import html
import json
import os
import re
import time

import requests

from .types import QUESTION_TYPE_MAP, MODEL_MAP, deep_blank_model, WHITELISTED_QUESTION_TYPES
from skipera.config import GRAPHQL_URL
from .queries import (GET_STATE_QUERY, SAVE_RESPONSES_QUERY, SUBMIT_DRAFT_QUERY,
                      INITIATE_ATTEMPT_QUERY)
from loguru import logger
from skipera.llm.connector import build_connector


INPUT_CHARS_PER_TOKEN = 4
OVERHEAD_CHARS = 1500      # system prompt + instruksi + wrapper
MIN_KNOWLEDGE_CHARS = 1500


# Short type codes for LLM payload (hemat token)
_SHORT_TYPE_MAP = {
    "Submission_MultipleChoiceQuestion": "MC",
    "Submission_MultipleChoiceReflectQuestion": "MC",
    "Submission_CheckboxQuestion": "MS",
    "Submission_CheckboxReflectQuestion": "MS",
    "Submission_PlainTextQuestion": "TXT",
    "Submission_TextReflectQuestion": "TXT",
    "Submission_NumericQuestion": "TXT",
    "Submission_MathQuestion": "TXT",
    "Submission_RegexQuestion": "TXT",
    "Submission_TextExactMatchQuestion": "TXT",
}


def _clean_text(raw, limit):
    """Bersihkan HTML/CML, collapse whitespace, truncate ke limit."""
    if not raw:
        return ""
    text = str(raw)
    # Coba cml_to_markdown jika ada HTML
    if '<' in text:
        try:
            from skipera.knowledge import cml_to_markdown
            text = cml_to_markdown(text)
        except Exception:
            # Fallback: regex strip tags
            text = re.sub(r'<[^>]+>', ' ', text)
    text = html.unescape(text)
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) > limit:
        text = text[:limit] + '…'
    return text


# Leaf key per response_field untuk text-type questions (reshaping jawaban teks).
_TEXT_LEAF = {
    "plainTextResponse": "plainText",
    "textReflectResponse": "answer",
    "textExactMatchResponse": "answer",
    "mathResponse": "answer",
    "regexResponse": "answer",
    "numericResponse": "answer",
}

# Response-field keys yang membawa jawaban teks (untuk lenient fallback).
_TEXT_FIELDS = (
    "plainTextResponse",
    "textReflectResponse",
    "textExactMatchResponse",
    "mathResponse",
    "regexResponse",
    "numericResponse",
    "richTextResponse",
)


_RICHTEXT_DTD = "assess/1"


def _cml_wrap(text):
    """Escape XML lalu bungkus teks sebagai CML ``<co-content><text>...</text>``."""
    safe = html.escape(str(text if text is not None else ""), quote=False)
    return f"<co-content><text>{safe}</text></co-content>"


def _cml_unwrap(text):
    """Ambil teks asli dari CML (strip tag ``<co-content><text>`` + unescape XML)."""
    if not isinstance(text, str):
        return ""
    if "<co-content>" not in text:
        return text  # teks plain (kandidat 3/4) — jangan diubah
    match = re.search(r"<co-content>\s*<text>(.*?)</text>\s*</co-content>", text, re.DOTALL)
    if match:
        return html.unescape(match.group(1))
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def _richtext_candidates(text):
    """Kandidat bentuk ``richTextResponse.richText`` (inner), primer (1) -> fallback (4).

    Server menolak ``{"richText": {"value": ...}}`` (value hanya ada di
    SubmissionHtmlContent, bukan CmlContent), jadi kandidat 1 (cmlValue + dtdId)
    dipakai sebagai kanonis dan sisanya sebagai fallback berurutan.
    """
    plain = str(text if text is not None else "")
    wrapped = _cml_wrap(plain)
    return [
        {"richText": {"cmlValue": wrapped, "dtdId": _RICHTEXT_DTD}},
        {"richText": {"value": wrapped, "dtdId": _RICHTEXT_DTD}},
        {"richText": plain},
        {"richText": {"cmlValue": plain}},
    ]


def _extract_text(value):
    """Pull a plain string out of any text-leaf shape (max one level deep)."""
    if isinstance(value, str):
        return _cml_unwrap(value)
    if isinstance(value, dict):
        for key in ("plainText", "answer", "text", "cmlValue", "value",
                    "richText", "htmlWithMetadata"):
            v = value.get(key)
            if isinstance(v, str):
                return _cml_unwrap(v)
            if isinstance(v, dict):
                for k2 in ("value", "text", "answer", "plainText", "cmlValue"):
                    v2 = v.get(k2)
                    if isinstance(v2, str):
                        return _cml_unwrap(v2)
        return ""
    if isinstance(value, list):
        return " ".join(str(x) for x in value if isinstance(x, str))
    return str(value) if value is not None else ""


def _sanitize_response(response_field: str, value):
    """Coerce a response value into a non-nullable, correctly-shaped payload.

    - checkboxResponse: ``chosen`` -> list ([] bila None/kosong)
    - multipleChoiceResponse: ``chosen`` -> str ("" bila None/list kosong)
    - text leaves (plainText/answer/...): -> "" bila None
    - richTextResponse: bentuk kanonis ``{"richText": {"cmlValue": CML, "dtdId": "assess/1"}}``
      (kandidat primer; fallback lewat ``_richtext_candidates`` di save_responses)
    Returns a new dict, never mutates the input.
    """
    if response_field == "checkboxResponse":
        chosen = value.get("chosen") if isinstance(value, dict) else value
        if chosen is None:
            return {"chosen": []}
        if isinstance(chosen, str):
            return {"chosen": [chosen] if chosen else []}
        if isinstance(chosen, list):
            return {"chosen": [c for c in chosen if c]}
        return {"chosen": []}
    if response_field == "multipleChoiceResponse":
        chosen = value.get("chosen") if isinstance(value, dict) else value
        if chosen is None:
            return {"chosen": ""}
        if isinstance(chosen, list):
            return {"chosen": chosen[0] if chosen else ""}
        return {"chosen": chosen if isinstance(chosen, str) else ""}
    if response_field == "richTextResponse":
        text = _extract_text(value)
        return _richtext_candidates(text)[0]  # kanonis: cmlValue + dtdId
    # Text leaves: normalize key to the canonical leaf, value to a string.
    leaf = _TEXT_LEAF.get(response_field, "answer")
    return {leaf: _extract_text(value)}


class GradedSolver(object):
    def __init__(self, session: requests.Session, course_id: str, item_id: str,
                 llm_settings: dict = None, knowledge_context: str = "",
                 knowledge_dir=None, course_slug: str = "", max_chars: int = 4000,
                 input_token_budget: int = 5000):
        self.session: requests.Session = session
        self.course_id: str = course_id
        self.item_id: str = item_id
        self.llm_settings = llm_settings
        self.knowledge_context = knowledge_context or ""
        self.knowledge_dir = knowledge_dir
        self.course_slug = course_slug
        self.max_chars = max_chars
        self.input_token_budget = input_token_budget
        self.attempt_id = None
        self.draft_id = None
        self.discarded_questions = []
        self.question_specs = {}
        self.last_error = ""
        self.last_grade = ""          # e.g. "90% (9/10)"
        self.preexisting_perfect = False
        self.last_questions = {}
        self.last_submitted = []
        # Short-id maps (hemat token): full partId <-> "Q1".. / full optionId <-> "O1"..
        self.short_map = {}   # full_qid -> short_qid
        self.full_map = {}    # short_qid -> full_qid
        self.opt_map = {}     # short_qid -> {short_opt: full_opt_id}
        self._last_full_map = {}
        self._wrong_detail = {}  # full_qid -> {prompt, options, prev_full} untuk rebuild note
        self._best_ratio = 0.0   # rasio tertinggi (stall cap, tahan reshuffle maximum)
        self._stall_counter = 0  # banyak attempt tanpa perbaikan best

    def solve(self) -> str:
        """Returns one of: passed, failed, no_attempts, no_llm, error.

        Target is 100%: attempts repeat while the grade is below perfect
        and the course still grants attempts.
        """
        state = self.get_state()
        if not state:
            return "error"
        percent, earned, maximum = self.grade_info(state)
        if maximum and earned >= maximum:
            self.preexisting_perfect = True
            logger.info(f"Sudah 100% ({self.last_grade}) — dilewati.")
            return "passed"

        retry_note = ""
        wrong_qids = None  # None = semua soal, [] = tak ada rincian
        # Stall cap: hentikan loop bila skor tak membaik N attempt berturut-turut.
        earned_ratio = (earned / maximum) if maximum else 0.0
        self._best_ratio = earned_ratio
        self._stall_counter = 0
        while True:
            state = self.get_state()
            if not state:
                return "error"

            if state["allowedAction"] == "RESUME_DRAFT":
                logger.info("Resuming in-progress attempt...")
                status = self._solve_current_attempt(retry_note, only_qids=wrong_qids)
                if status == "no_llm":
                    return "no_llm"
                if status == "error":
                    return "error"

            elif state["allowedAction"] == "START_NEW_ATTEMPT":
                remaining = state["attempts"]["attemptsRemaining"]
                if remaining == 0:
                    logger.error("No more attempts can be made!")
                    return "no_attempts"

                rem_label = remaining if remaining is not None else "tak terbatas"
                logger.info(f"Starting a new attempt ({rem_label} remaining before this run)...")
                if not self.initiate_attempt():
                    logger.error("Could not start an attempt. Please file an issue.")
                    return "error"

                status = self._solve_current_attempt(retry_note, only_qids=wrong_qids)
                if status == "no_llm":
                    return "no_llm"
                if status == "error":
                    return "error"

            else:
                logger.error("Something went wrong! Please file an issue.")
                return "error"

            state = self.get_state()
            if not state:
                return "error"
            percent, earned, maximum = self.grade_info(state)
            remaining_raw = state["attempts"]["attemptsRemaining"]
            rem_label = remaining_raw if remaining_raw is not None else "tak terbatas"
            logger.info(f"Nilai: {self.last_grade} | sisa attempt: {rem_label}")

            if maximum and earned >= maximum:
                return "passed"

            # Stall cap: pakai rasio earned/maximum agar tahan reshuffle.
            ratio = (earned / maximum) if maximum else 0.0
            if ratio > self._best_ratio + 1e-9:
                self._best_ratio = ratio
                self._stall_counter = 0
            else:
                self._stall_counter += 1
            if self._stall_counter >= 3:
                logger.error(
                    f"Skor stagnan di {self.last_grade} selama {self._stall_counter} attempt — berhenti, "
                    "perlu di-resume manual via view-feedback.")
                return "failed"

            if remaining_raw is not None and remaining_raw == 0:
                logger.error(
                    f"Berhenti di {self.last_grade} (attempt habis). "
                    "Soal yang salah perlu dikerjakan manual via view-feedback.")
                return "failed"

            if state["allowedAction"] != "START_NEW_ATTEMPT":
                logger.error(
                    f"Berhenti di {self.last_grade} (attempt habis — action={state['allowedAction']}). "
                    "Soal yang salah perlu dikerjakan manual via view-feedback.")
                return "failed"

            # Hitung soal salah untuk retry hemat token
            wrong_qids = self._wrong_qids(state)
            if wrong_qids and len(wrong_qids) < len(self.question_specs):
                logger.info(f"Retry hanya {len(wrong_qids)} soal salah dari {len(self.question_specs)} total")
            else:
                wrong_qids = None  # Fallback: semua soal
            retry_note = self._build_retry_note(state, wrong_qids)
            logger.warning(f"Belum 100% ({self.last_grade}). Mengerjakan ulang...")

    def _wrong_qids(self, state: dict) -> list:
        """Return list full_qid yang score<max (untuk retry hemat token)."""
        scores = self.feedback_scores(state)
        if not scores:
            return []
        wrong = []
        for qid in self.question_specs:
            suffix = qid.split("~")[-1]
            hit = scores.get(suffix)
            if hit is None:
                continue
            score, maximum = hit
            if maximum and score < maximum:
                wrong.append(qid)
        return wrong

    def grade_info(self, state: dict) -> tuple:
        """Returns (percent, earned, maximum) and caches a printable grade."""
        earned, maximum = 0, 0
        state = state or {}
        try:
            fb = (state.get("feedback") or {}).get("outcome") or {}
            if fb.get("maxScore"):
                maximum = fb["maxScore"]
                earned = fb.get("highestScore", fb.get("latestScore", 0)) or 0
        except (AttributeError, TypeError):
            pass
        if not maximum:
            try:
                earned = float((state.get("outcome") or {}).get("earnedGrade", 0) or 0)
            except (AttributeError, TypeError, ValueError):
                earned = 0
            maximum = 1
        percent = (earned / maximum * 100) if maximum else 0
        try:
            self.last_grade = f"{percent:.0f}% ({earned:g}/{maximum:g})"
        except (TypeError, ValueError):
            self.last_grade = f"{percent:.0f}%"
        return percent, earned, maximum

    def feedback_scores(self, state: dict) -> dict:
        """Map draft question_id -> (score, maxScore) from view-feedback data."""
        scores = {}
        try:
            parts = (state.get("feedback") or {}).get("parts") or []
        except AttributeError:
            return scores
        for part in parts:
            fb = (part.get("feedback") or {}).get("outcome") or {}
            if "score" not in fb:
                continue
            full_id = part.get("partId", "")
            suffix = full_id.split("~")[-1]
            scores[suffix] = (fb.get("score", 0), fb.get("maxScore", 0))
        return scores

    def _build_retry_note(self, state: dict, wrong_qids=None) -> str:
        """Describe the previous attempt's wrong questions for the next LLM call (kompak).

        Selain string, simpan ``self._wrong_detail`` (keyed by full_qid) dengan
        prompt clean + options (full_opt_id, val) + prev_full, agar note bisa
        di-rebuild dengan penomoran Q/O BARU bila soal berubah antar attempt.
        """
        if wrong_qids is None:
            wrong_qids = self._wrong_qids(state)
        if not wrong_qids and not self.last_submitted:
            return ""
        lines = [f"Hasil attempt sebelumnya: {self.last_grade}."]
        self._wrong_detail = {}
        if wrong_qids:
            lines.append(f"{len(wrong_qids)} soal masih salah — perbaiki:")
            prev = {a.get("question_id"): a for a in self.last_submitted}
            for full_qid in wrong_qids:
                short_qid = self.short_map.get(full_qid, full_qid)
                q = self.last_questions.get(short_qid, {})
                prompt_clean = _clean_text(str(q.get("prompt", "")), 150)
                options = q.get("options", [])
                # Detail stabil keyed by full id (opt -> full_opt_id)
                detail = {
                    "prompt": prompt_clean,
                    "options": [],
                    "prev_full": [],
                }
                for o in options[:4]:
                    short_opt = o.get("id", "")
                    full_opt = self.opt_map.get(short_qid, {}).get(short_opt, short_opt)
                    detail["options"].append((full_opt, str(o.get("v", ""))[:40]))
                self._wrong_detail[full_qid] = detail

                lines.append(f"- {short_qid}: {prompt_clean}")
                if options:
                    opt_text = ", ".join(
                        f"{o.get('id')}={str(o.get('v', ''))[:40]}" for o in options[:4])
                    lines.append(f"  opsi: {opt_text}")
                if full_qid in prev:
                    ans = prev[full_qid]
                    qr = ans.get("question_response", {})
                    chosen = qr.get("multipleChoiceResponse", {}).get("chosen") or \
                             qr.get("checkboxResponse", {}).get("chosen") or []
                    if isinstance(chosen, str):
                        chosen = [chosen]
                    # Simpan FULL opt id (stabil lintas reshuffle) untuk rebuild
                    self._wrong_detail[full_qid]["prev_full"] = list(chosen)
                    # Translate full -> short untuk tampilan
                    opt_reverse = {}
                    for so, fo in (self.opt_map.get(short_qid, {}).items()):
                        opt_reverse[fo] = so
                    short_chosen = [opt_reverse.get(c, c) for c in chosen]
                    lines.append(f"  dulu: {short_chosen}")
        else:
            lines.append("Tidak ada rincian soal yang salah; jawab ulang semuanya dengan teliti.")
        return "\n".join(lines) + "\n\nIMPORTANT: Do NOT repeat the same wrong answer. Think differently and verify against course material."

    def _build_sibling_appendix(self, subset_shorts: set) -> str:
        """Context appendix: soal benar yang TIDAK dikirim ke LLM + jawabannya.

        Menutup lubang korelasi: LLM butuh isi studi kasus + jawaban Q-benarnya
        agar uraian konsisten. Prompt ≤200 chars, teks ≤300, MC/checkbox tampil
        sebagai short opt + nilai ≤60 chars.
        """
        prev = {a.get("question_id"): a for a in self.last_submitted}
        lines = []
        for short_qid in self.last_questions:
            full_qid = self.full_map.get(short_qid, short_qid)
            if short_qid in subset_shorts or full_qid in subset_shorts:
                continue
            q = self.last_questions.get(short_qid, {})
            prompt = _clean_text(str(q.get("prompt", "")), 200)
            ans = prev.get(full_qid)
            ans_text = "belum dijawab"
            if ans:
                qr = ans.get("question_response", {}) or {}
                if "multipleChoiceResponse" in qr:
                    chosen_full = qr["multipleChoiceResponse"].get("chosen")
                    shown = self._short_opt(short_qid, chosen_full)
                    opt_val = self._opt_value(short_qid, shown)[:60]
                    ans_text = f"{shown} ({opt_val})" if opt_val else shown
                elif "checkboxResponse" in qr:
                    chosen_list = qr["checkboxResponse"].get("chosen") or []
                    items = []
                    for c in chosen_list:
                        so = self._short_opt(short_qid, c)
                        ov = self._opt_value(short_qid, so)[:60]
                        items.append(f"{so} ({ov})" if ov else so)
                    ans_text = ", ".join(items) or "(kosong)"
                else:
                    for key, val in qr.items():
                        ans_text = _extract_text(val)[:300]
                        break
                    ans_text = ans_text or "(kosong)"
            lines.append(f"- {short_qid}: {prompt} | jawabanmu: {ans_text}")
        if not lines:
            return ""
        return "SOAL LAIN & JAWABANMU (untuk konsistensi):\n" + "\n".join(lines)

    def _short_opt(self, short_qid, full_opt_id) -> str:
        """Full option id -> short id (untuk ditampilkan ke LLM)."""
        for so, fo in (self.opt_map.get(short_qid, {}) or {}).items():
            if fo == full_opt_id:
                return so
        return str(full_opt_id or "")

    def _opt_value(self, short_qid, short_opt) -> str:
        """Cari nilai clean opsi dari last_questions."""
        opts = (self.last_questions.get(short_qid, {}) or {}).get("options", []) or []
        for o in opts:
            if o.get("id") == short_opt:
                return str(o.get("v", ""))
        return ""

    def _rebuild_retry_note(self, ask_wrong) -> str:
        """Rebuild retry note hanya untuk qid yang masih ada, dengan penomoran Q/O BARU."""
        if not ask_wrong:
            return ""
        lines = [f"Hasil attempt sebelumnya: {self.last_grade}."]
        lines.append(f"{len(ask_wrong)} soal masih salah — perbaiki:")
        for full_qid in ask_wrong:
            short_qid = self.short_map.get(full_qid, full_qid)
            detail = self._wrong_detail.get(full_qid, {})
            prompt_clean = detail.get("prompt", "")
            if not prompt_clean:
                q = self.last_questions.get(short_qid, {})
                prompt_clean = _clean_text(str(q.get("prompt", "")), 150)
            lines.append(f"- {short_qid}: {prompt_clean}")
            # options: full_opt_id -> short id via opt_map BARU
            opt_reverse = {fo: so for so, fo in self.opt_map.get(short_qid, {}).items()}
            stored_opts = detail.get("options", [])
            if stored_opts:
                opt_text = ", ".join(
                    f"{opt_reverse.get(fo, fo)}={str(val)[:40]}"
                    for fo, val in stored_opts[:4])
                lines.append(f"  opsi: {opt_text}")
            prev_full = detail.get("prev_full", [])
            if prev_full:
                short_chosen = [opt_reverse.get(c, c) for c in prev_full]
                lines.append(f"  dulu: {short_chosen}")
        return "\n".join(lines) + "\n\nIMPORTANT: Do NOT repeat the same wrong answer. Think differently and verify against course material."

    def _solve_current_attempt(self, retry_note: str = "", only_qids=None) -> str:
        try:
            connector = build_connector(self.llm_settings)
        except Exception as exc:
            logger.error(f"Skipping graded assessment: could not build LLM connector ({exc}).")
            return "no_llm"

        # A. Snapshot attempt SEBELUM retrieve_questions() me-reset spec/maps.
        prev_qids = set(self.question_specs)          # kosong saat attempt pertama
        prev_answered = {a.get("question_id"): a for a in self.last_submitted}

        questions = self.retrieve_questions()
        self.last_questions = questions
        self._last_full_map = dict(self.full_map)
        new_qids = set(self.question_specs)

        # B. Tentukan subset soal untuk LLM + apakah reuse jawaban lama.
        #    Hanya relevan saat only_qids diberikan (retry). only_qids is None
        #    -> perilaku lama persis (semua soal, tanpa reuse).
        reuse_qids = set()      # full_qid correct lama yang masih ada -> reuse
        effective_only = None   # None => mode full (semua soal ke LLM)
        if only_qids is not None:
            only_set = set(only_qids)
            new_order = list(self.question_specs)  # urutan draft BARU
            still_wrong = [q for q in new_order if q in only_set]
            brand_new = [q for q in new_order if q not in prev_qids]
            ask = still_wrong + brand_new  # dedup otomatis (still_wrong ⊆ prev)
            if brand_new:
                logger.info(
                    f"Soal berubah antar attempt (bank acak): {len(brand_new)} soal baru "
                    "ikut dikirim ke LLM")
            if not still_wrong and brand_new:
                logger.warning(
                    "Soal attempt baru semuanya berbeda — retry_note lama dibuang, mode full")
                retry_note = ""
            if not ask or len(ask) >= len(new_qids):
                effective_only = None  # fallback / semua soal
            else:
                effective_only = set(ask)
            # Reuse: correct lama yang STILL ada (∈ new_qids) dan bukan yang salah
            reuse_qids = {q for q in prev_answered
                          if q in new_qids and q not in only_qids}
            # C. Jika soal/urutan berubah, rebuild retry_note dengan penomoran Q/O BARU.
            #    Selalu rebuild saat still_wrong non-empty: idempoten bila soal & urutan
            #    sama, tapi memperbaiki penomoran bila ada reshuffle / subset berubah.
            if still_wrong:
                retry_note = self._rebuild_retry_note(still_wrong)

        # Filter subset untuk LLM
        subset_shorts = None
        if effective_only is not None and len(effective_only) < len(questions):
            subset = {}
            for short_qid, payload in questions.items():
                full_qid = self.full_map.get(short_qid, short_qid)
                if full_qid in effective_only:
                    subset[short_qid] = payload
            if subset:
                questions = subset
                subset_shorts = set(subset)
                logger.debug(f"LLM hanya menerima {len(questions)} soal")

        # Prefer knowledge filtered by the retrieved questions (summarized +
        # relevance-ranked); fall back to the prebuilt context when unavailable.
        context = self.knowledge_context
        if self.knowledge_dir and self.course_slug:
            try:
                from skipera.knowledge import load_knowledge_context
                q_chars = len(json.dumps(questions, ensure_ascii=False))
                budget_chars = self.input_token_budget * INPUT_CHARS_PER_TOKEN
                remaining = budget_chars - q_chars - OVERHEAD_CHARS
                kb = max(MIN_KNOWLEDGE_CHARS, min(self.max_chars, remaining))
                logger.debug(
                    f"Budget konteks: total={self.input_token_budget} tok, soal={q_chars} char, "
                    f"konteks={kb} char"
                )
                relevant = load_knowledge_context(
                    self.knowledge_dir, self.course_slug, kb, query=questions)
                if relevant:
                    context = relevant
            except Exception as exc:
                logger.debug(f"Gagal memuat knowledge relevan: {exc}")
        if retry_note:
            context = (context + "\n\n" if context else "") + "PREVIOUS ATTEMPT FEEDBACK:\n" + retry_note
        # Sibling appendix: jawaban soal benar untuk konsistensi (hemat, hanya subset).
        if subset_shorts is not None:
            appendix = self._build_sibling_appendix(subset_shorts)
            if appendix:
                context = (context + "\n\n" if context else "") + appendix
        try:
            retry_temp = 0.35 if retry_note else 0.0
            answers = connector.get_response(questions, context=context, temperature=retry_temp)
        except Exception as exc:
            self.last_error = str(exc)
            logger.error(f"Could not get answers from LLM: {exc}")
            return "error"

        # Merge: reuse jawaban lama yang correct DAN masih ada + jawaban baru LLM.
        # Entri basi (qid ∉ new_qids) tidak ikut merged. save_responses iterasi
        # question_specs BARU, jadi soal tanpa jawaban otomatis jadi blank.
        answers_list = answers.get("responses", answers) if isinstance(answers, dict) else answers
        if reuse_qids and self.last_submitted:
            merged = []
            for qid in reuse_qids:
                merged.append(prev_answered[qid])
            for ans in (answers_list if isinstance(answers_list, list) else [answers_list]):
                if isinstance(ans, dict):
                    merged.append(ans)
            answers_list = merged

        if not self.save_responses(answers_list):
            logger.error("Could not save responses. Please file an issue.")
            return "error"

        if not self.submit_draft():
            logger.error("Could not submit the assignment. Please file an issue.")
            return "error"

        logger.debug("Waiting 3 seconds for grading..")
        time.sleep(3)  # delay for grading process
        return "passed" if self.get_grade() else "failed"

    def get_state(self) -> dict:
        """
        Retrieves the current state of the assessment.

        Returns an empty dict when Coursera answers with the failure variant
        (e.g. the item is locked or not yet available) instead of raising.
        """
        res = self.session.post(url=GRAPHQL_URL, params={
            "opname": "QueryState"
        }, json={
            "operationName": "QueryState",
            "variables": {
                "courseId": self.course_id,
                "itemId": self.item_id
            },
            "query": GET_STATE_QUERY
        }).json()

        data = res.get("data") or {}
        state = (data.get("SubmissionState") or {}).get("queryState")
        if isinstance(state, dict) and "allowedAction" in state:
            return state

        # Failure variant (or null): surface the reason instead of crashing.
        reasons = res.get("errors") or []
        if isinstance(state, dict):
            reasons = reasons or state.get("errors") or []
        reason = str(reasons)[:300] if reasons else \
            "state tidak tersedia (item mungkin terkunci/belum tersedia)"
        self.last_error = f"QueryState gagal: {reason}"
        logger.error(self.last_error)
        return {}

    def initiate_attempt(self) -> bool:
        """
        Initiates a new attempt for the assessment.
        """
        res = self.session.post(url=GRAPHQL_URL, params={
            "opname": "Submission_StartAttempt"
        }, json={
            "operationName": "Submission_StartAttempt",
            "variables": {
                "courseId": self.course_id,
                "itemId": self.item_id
            },
            "query": INITIATE_ATTEMPT_QUERY
        })

        if "Submission_StartAttemptSuccess" in res.text:
            return True
        return False

    def retrieve_questions(self) -> dict:
        """
        Retrieves the questions for the particular attempt
        which are to be sent to the LLM Connector.
        """
        state = self.get_state()
        draft = state["attempts"]["inProgressAttempt"]

        self.draft_id = draft["id"]
        self.attempt_id = draft["draft"]["id"]
        questions = draft["draft"]["parts"]
        questions_formatted = {}
        self.discarded_questions = []
        self.question_specs = {}
        self.short_map = {}
        self.full_map = {}
        self.opt_map = {}

        q_counter = 0
        for question in questions:
            typename = question["__typename"]
            if typename not in QUESTION_TYPE_MAP:  # discard unknown question types
                logger.debug(
                    f"Q?: {typename} — dibuang (tipe tidak dikenal)")
                continue

            full_qid = question["partId"]
            self.question_specs[full_qid] = {
                "typename": typename,
                "response_field": QUESTION_TYPE_MAP[typename][0],
                "question_type": QUESTION_TYPE_MAP[typename][1],
            }

            if typename not in WHITELISTED_QUESTION_TYPES:
                logger.debug(f"{full_qid}: {typename} — dibuang (belum didukung)")
                self.discarded_questions.append({
                    "questionId": full_qid,
                    "questionType": QUESTION_TYPE_MAP[typename][1],
                    "questionResponse": {
                        QUESTION_TYPE_MAP[typename][0]:
                        deep_blank_model(MODEL_MAP[typename])
                    }
                })
                continue

            # Assign short ID
            q_counter += 1
            short_qid = f"Q{q_counter}"
            self.short_map[full_qid] = short_qid
            self.full_map[short_qid] = full_qid
            logger.debug(f"{short_qid}: {typename} — whitelisted")

            questions_formatted[short_qid] = self._format_question_for_llm(question, short_qid)

        if self.discarded_questions:
            types = sorted({q["questionType"] for q in self.discarded_questions})
            logger.warning(
                f"Melewati {len(self.discarded_questions)} soal bertipe belum didukung: "
                f"{', '.join(types)}. Soal ini akan dikirim kosong."
            )

        return questions_formatted

    def _format_question_for_llm(self, question: dict, short_qid: str) -> dict:
        # Token-tight mode: cap prompt/option length when SKIPERA_SHORT_PROMPT=1.
        short = os.getenv("SKIPERA_SHORT_PROMPT") == "1"
        prompt_cap = 400 if short else 800
        opt_cap = 100 if short else 200
        typename = question["__typename"]
        schema = question.get("questionSchema", {})
        raw_prompt = schema.get("prompt", {}).get("cmlValue") or schema.get("prompt", {}).get("value") or ""
        payload = {
            "question_id": short_qid,
            "type": _SHORT_TYPE_MAP.get(typename, "TXT"),
            "prompt": _clean_text(raw_prompt, prompt_cap),
        }

        if "options" in schema:
            opt_map = {}
            o_counter = 0
            options_list = []
            for option in schema["options"]:
                o_counter += 1
                short_opt = f"O{o_counter}"
                full_opt_id = option.get("optionId", "")
                opt_map[short_opt] = full_opt_id
                raw_value = option.get("display", {}).get("cmlValue") or option.get("display", {}).get("value") or ""
                options_list.append({
                    "id": short_opt,
                    "v": _clean_text(raw_value, opt_cap),
                })
            self.opt_map[short_qid] = opt_map
            payload["options"] = options_list

        # answer_format dihapus — dijelaskan sekali di ANSWER_INSTRUCTIONS connector

        return payload

    def _blank_response_payload(self, question_id: str) -> dict:
        spec = self.question_specs[question_id]
        blank = _sanitize_response(
            spec["response_field"], deep_blank_model(MODEL_MAP[spec["typename"]]))
        return {
            "questionId": question_id,
            "questionType": spec["question_type"],
            "questionResponse": {
                spec["response_field"]: blank
            }
        }

    def _translate_answer(self, question_id, answer):
        """Translate short opt ids -> full opt ids sebelum coerce.

        ``question_id`` is the full partId; opt_map is keyed by the short id.
        """
        if not isinstance(answer, dict):
            return answer
        short_qid = self.short_map.get(question_id, question_id)
        opt_reverse = {so: fo for so, fo in self.opt_map.get(short_qid, {}).items()}
        if not opt_reverse:
            return answer
        qr = answer.get("question_response", {})
        if not isinstance(qr, dict):
            return answer
        opt_reverse = {so: fo for so, fo in self.opt_map.get(short_qid, {}).items()}
        # multipleChoiceResponse.chosen
        mc = qr.get("multipleChoiceResponse", {})
        if isinstance(mc, dict) and "chosen" in mc:
            chosen = mc["chosen"]
            if isinstance(chosen, str) and chosen in opt_reverse:
                mc["chosen"] = opt_reverse[chosen]
        # checkboxResponse.chosen
        cb = qr.get("checkboxResponse", {})
        if isinstance(cb, dict) and "chosen" in cb:
            chosen = cb["chosen"]
            if isinstance(chosen, list):
                cb["chosen"] = [opt_reverse.get(c, c) for c in chosen]
        return answer

    def _coerce_llm_answer(self, question_id: str, answer: dict) -> dict:
        spec = self.question_specs[question_id]
        response_field = spec["response_field"]

        # Translate short -> full sebelum proses
        answer = self._translate_answer(question_id, answer)

        raw_response = answer.get("question_response", {})
        if not isinstance(raw_response, dict):
            raw_response = {}

        value = None
        if response_field in raw_response:
            value = raw_response[response_field]
        elif response_field in _TEXT_LEAF or response_field == "richTextResponse":
            # Lenient: ambil nilai teks dari field/leaf alternatif mana pun.
            for tf in _TEXT_FIELDS:
                if tf in raw_response:
                    value = raw_response[tf]
                    break
            if value is None:
                for lk in ("plainText", "answer", "text", "value", "cmlValue",
                           "richText"):
                    if lk in raw_response:
                        value = raw_response[lk]
                        break
        if value is not None:
            return {
                "questionId": question_id,
                "questionType": spec["question_type"],
                "questionResponse": {
                    response_field: _sanitize_response(response_field, value)
                }
            }

        # Backward compatibility for the old connector schema.
        if answer.get("type") in {"Single", "Multi"} and "option_id" in answer:
            if answer["type"] == "Single":
                candidate = {
                    "multipleChoiceResponse": {
                        "chosen": answer["option_id"][0] if answer["option_id"] else ""
                    }
                }
            else:
                candidate = {
                    "checkboxResponse": {
                        "chosen": answer["option_id"]
                    }
                }

            if response_field in candidate:
                return {
                    "questionId": question_id,
                    "questionType": spec["question_type"],
                    "questionResponse": {
                        response_field: _sanitize_response(
                            response_field, candidate[response_field])
                    }
                }

        return self._blank_response_payload(question_id)

    def save_responses(self, answers: dict) -> bool:
        """
        Saves the responses for the assessment to the draft.
        """
        if isinstance(answers, dict):
            if "responses" in answers:
                answers = answers.get("responses", [])
            elif answers.get("question_id"):
                answers = [answers]  # single response object, not wrapped
            else:
                logger.debug(f"Format jawaban LLM tak dikenal: {str(answers)[:200]}")
                answers = []
        elif isinstance(answers, list):
            pass  # sudah list
        else:
            answers = []

        answers_by_id = {}
        for answer in answers or []:
            if not isinstance(answer, dict):
                logger.debug(f"Item jawaban bukan object, dilewati: {str(answer)[:200]}")
                continue
            qid = answer.get("question_id")
            if not qid:
                continue
            # Normalize short -> full
            if qid in self.full_map:
                qid = self.full_map[qid]
            elif qid not in self.question_specs:
                # Coba reverse lookup
                for full, short in self.short_map.items():
                    if short == qid:
                        qid = full
                        break
            answers_by_id[qid] = answer

        answer_responses = []
        for question_id in self.question_specs:
            answer = answers_by_id.get(question_id)
            if not answer:
                answer_responses.append(self._blank_response_payload(question_id))
                continue

            answer_responses.append(self._coerce_llm_answer(question_id, answer))

        self.last_submitted = [
            {"question_id": r["questionId"], "question_response": r["questionResponse"]}
            for r in answer_responses
        ]

        res = self.session.post(url=GRAPHQL_URL, params={
            "opname": "Submission_SaveResponses"
        }, json={
            "operationName": "Submission_SaveResponses",
            "variables": {
                "input": {
                    "courseId": self.course_id,
                    "itemId": self.item_id,
                    "attemptId": self.draft_id,
                    "questionResponses": answer_responses
                }
            },
            "query": SAVE_RESPONSES_QUERY
        })

        if "Submission_SaveResponsesSuccess" in res.text:
            return True

        # Fallback: coba kandidat 2..4 bila server tolak bentuk richText kanonis.
        # Hanya mengganti bagian richTextResponse soal bertipe RICH_TEXT; soal lain tetap.
        def _richtext_hint(errors):
            blob = str(errors or "").lower()
            return ("bad_user_input" in blob
                    and any(k in blob for k in ("richtext", "/value", "cmlvalue", "cml")))

        richtext_qids = [qid for qid, spec in self.question_specs.items()
                         if spec.get("response_field") == "richTextResponse"]
        try:
            body = res.json()
            errors = (
                body.get("data", {})
                .get("Submission_SaveResponses", {})
                .get("errors", [])
            ) or body.get("errors", [])
        except ValueError:
            logger.error(f"SaveResponses gagal (HTTP {res.status_code}): {res.text[:300]}")
            return False

        if richtext_qids and _richtext_hint(errors):
            # Ekstrak teks per soal richText dari payload kanonis (untuk rebuild kandidat).
            texts = {}
            for r in answer_responses:
                qr = r.get("questionResponse", {}) or {}
                if "richTextResponse" in qr:
                    texts[r["questionId"]] = _extract_text(qr["richTextResponse"])
            for n in (2, 3, 4):
                swapped = []
                for r in answer_responses:
                    qr = r.get("questionResponse", {}) or {}
                    if "richTextResponse" in qr:
                        inner = _richtext_candidates(texts.get(r["questionId"], ""))[n - 1]
                        r2 = dict(r)
                        r2["questionResponse"] = {"richTextResponse": inner}
                        swapped.append(r2)
                    else:
                        swapped.append(r)
                retry = self.session.post(url=GRAPHQL_URL, params={
                    "opname": "Submission_SaveResponses"
                }, json={
                    "operationName": "Submission_SaveResponses",
                    "variables": {
                        "input": {
                            "courseId": self.course_id,
                            "itemId": self.item_id,
                            "attemptId": self.draft_id,
                            "questionResponses": swapped
                        }
                    },
                    "query": SAVE_RESPONSES_QUERY
                })
                if "Submission_SaveResponsesSuccess" in retry.text:
                    logger.success(f"server menerima bentuk richText kandidat-{n}")
                    # last_submitted = bentuk yang diterima server
                    self.last_submitted = [
                        {"question_id": r["questionId"], "question_response": r["questionResponse"]}
                        for r in swapped
                    ]
                    return True
                # total percobaan save <= 4 (1 kanonis + 3 fallback); lanjut ke kandidat berikutnya
            try:
                retry_body = retry.json()
                retry_errors = (
                    retry_body.get("data", {})
                    .get("Submission_SaveResponses", {})
                    .get("errors", [])
                ) or retry_body.get("errors", [])
            except ValueError:
                retry_errors = []
            all_errors = list(errors or []) + list(retry_errors or [])
            logger.error(
                f"SaveResponses ditolak server (semua kandidat richText gagal): "
                f"{str(all_errors)[:2000]}")
            logger.debug(answer_responses)
            logger.debug(body)
            return False

        logger.error(f"SaveResponses ditolak server: {str(errors)[:2000]}")
        logger.debug(answer_responses)
        logger.debug(body)
        return False

    def submit_draft(self) -> bool:
        """
        Submits the draft for evaluation after the submission is saved.
        """
        res = self.session.post(url=GRAPHQL_URL, params={
            "opname": "Submission_SubmitLatestDraft"
        }, json={
            "operationName": "Submission_SubmitLatestDraft",
            "query": SUBMIT_DRAFT_QUERY,
            "variables": {
                "input": {
                    "courseId": self.course_id,
                    "itemId": self.item_id,
                    "submissionId": self.attempt_id
                }
            }
        })

        if "Submission_SubmitLatestDraftSuccess" in res.text:
            return True
        return False

    def get_grade(self) -> bool:
        """
        Retrieves the outcome for the submitted assignment.
        """
        res = self.session.post(url=GRAPHQL_URL, params={
            "opname": "QueryState"
        }, json={
            "operationName": "QueryState",
            "query": GET_STATE_QUERY,
            "variables": {
                "courseId": self.course_id,
                "itemId": self.item_id
            }
        }).json()

        state = ((res.get("data") or {}).get("SubmissionState") or {}).get("queryState") or {}

        outcome = state.get("outcome")

        if outcome is not None:
            logger.debug(f"Achieved {outcome['earnedGrade']} grade. Passed? {outcome['isPassed']}")
        else:
            logger.debug("Outcome is None - check upstream logic")
            return False

        return outcome['isPassed']
