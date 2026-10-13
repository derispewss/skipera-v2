import time

import requests

from .types import QUESTION_TYPE_MAP, MODEL_MAP, deep_blank_model, WHITELISTED_QUESTION_TYPES
from skipera.config import GRAPHQL_URL
from .queries import (GET_STATE_QUERY, SAVE_RESPONSES_QUERY, SUBMIT_DRAFT_QUERY,
                      INITIATE_ATTEMPT_QUERY)
from loguru import logger
from skipera.llm.connector import build_connector


class GradedSolver(object):
    def __init__(self, session: requests.Session, course_id: str, item_id: str,
                 llm_settings: dict = None, knowledge_context: str = "",
                 knowledge_dir=None, course_slug: str = "", max_chars: int = 8000):
        self.session: requests.Session = session
        self.course_id: str = course_id
        self.item_id: str = item_id
        self.llm_settings = llm_settings
        self.knowledge_context = knowledge_context or ""
        self.knowledge_dir = knowledge_dir
        self.course_slug = course_slug
        self.max_chars = max_chars
        self.attempt_id = None
        self.draft_id = None
        self.discarded_questions = []
        self.question_specs = {}
        self.last_error = ""
        self.last_grade = ""          # e.g. "90% (9/10)"
        self.preexisting_perfect = False
        self.last_questions = {}
        self.last_submitted = []

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
        while True:
            state = self.get_state()
            if not state:
                return "error"

            if state["allowedAction"] == "RESUME_DRAFT":
                logger.info("Resuming in-progress attempt...")
                status = self._solve_current_attempt(retry_note)
                if status == "no_llm":
                    return "no_llm"
                if status == "error":
                    return "error"

            elif state["allowedAction"] == "START_NEW_ATTEMPT":
                remaining = state["attempts"]["attemptsRemaining"]
                if remaining == 0:
                    logger.error("No more attempts can be made!")
                    return "no_attempts"

                logger.info(f"Starting a new attempt ({remaining} remaining before this run)...")
                if not self.initiate_attempt():
                    logger.error("Could not start an attempt. Please file an issue.")
                    return "error"

                status = self._solve_current_attempt(retry_note)
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
            remaining = state["attempts"]["attemptsRemaining"]
            logger.info(f"Nilai: {self.last_grade} | sisa attempt: {remaining}")

            if maximum and earned >= maximum:
                return "passed"

            if state["allowedAction"] != "START_NEW_ATTEMPT" or remaining == 0:
                logger.error(
                    f"Berhenti di {self.last_grade} (attempt habis). "
                    "Soal yang salah perlu dikerjakan manual via view-feedback.")
                return "failed"

            retry_note = self._build_retry_note(state)
            logger.warning(f"Belum 100% ({self.last_grade}). Mengerjakan ulang...")

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

    def _build_retry_note(self, state: dict) -> str:
        """Describe the previous attempt's wrong questions for the next LLM call."""
        scores = self.feedback_scores(state)
        if not scores and not self.last_submitted:
            return ""
        # question_id (draft form) -> suffix used by feedback parts
        wrong = []
        for qid in self.question_specs:
            suffix = qid.split("~")[-1]
            hit = scores.get(suffix)
            if hit is None:
                # Fallback: positional match when counts align.
                continue
            score, maximum = hit
            if maximum and score < maximum:
                wrong.append(qid)
        lines = [f"Hasil attempt sebelumnya: {self.last_grade}."]
        if wrong:
            lines.append(f"{len(wrong)} soal masih salah — perbaiki:")
            prev = {a.get("question_id"): a for a in self.last_submitted}
            for qid in wrong:
                q = self.last_questions.get(qid, {})
                prompt = str(q.get("prompt", ""))[:300]
                lines.append(f"- {qid}: {prompt}")
                options = q.get("options", [])
                if options:
                    opt_text = ", ".join(
                        f"{o.get('option_id')[:8]}={str(o.get('value', ''))[:60]}"
                        for o in options[:8])
                    lines.append(f"  opsi: {opt_text}")
                if qid in prev:
                    lines.append(f"  jawabanmu sebelumnya: {prev[qid]}")
        else:
            lines.append("Tidak ada rincian soal yang salah; jawab ulang semuanya dengan teliti.")
        return "\n".join(lines)

    def _solve_current_attempt(self, retry_note: str = "") -> str:
        try:
            connector = build_connector(self.llm_settings)
        except Exception as exc:
            logger.error(f"Skipping graded assessment: could not build LLM connector ({exc}).")
            return "no_llm"

        questions = self.retrieve_questions()
        self.last_questions = questions
        # Prefer knowledge filtered by the retrieved questions (summarized +
        # relevance-ranked); fall back to the prebuilt context when unavailable.
        context = self.knowledge_context
        if self.knowledge_dir and self.course_slug:
            try:
                from skipera.knowledge import load_knowledge_context
                relevant = load_knowledge_context(
                    self.knowledge_dir, self.course_slug, self.max_chars, query=questions)
                if relevant:
                    context = relevant
            except Exception as exc:
                logger.debug(f"Gagal memuat knowledge relevan: {exc}")
        if retry_note:
            context = (context + "\n\n" if context else "") + "PREVIOUS ATTEMPT FEEDBACK:\n" + retry_note
        try:
            answers = connector.get_response(questions, context=context)
        except Exception as exc:
            self.last_error = str(exc)
            logger.error(f"Could not get answers from LLM: {exc}")
            return "error"

        if not self.save_responses(answers.get("responses", answers)):
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

        for question in questions:
            typename = question["__typename"]
            if typename not in QUESTION_TYPE_MAP:  # discard unknown question types
                continue

            self.question_specs[question["partId"]] = {
                "typename": typename,
                "response_field": QUESTION_TYPE_MAP[typename][0],
                "question_type": QUESTION_TYPE_MAP[typename][1],
            }

            if typename not in WHITELISTED_QUESTION_TYPES:
                self.discarded_questions.append({
                    "questionId": question["partId"],
                    "questionType": QUESTION_TYPE_MAP[typename][1],
                    "questionResponse": {
                        QUESTION_TYPE_MAP[typename][0]:
                        deep_blank_model(MODEL_MAP[typename])
                    }
                })
                continue

            questions_formatted[question["partId"]] = self._format_question_for_llm(question)

        if self.discarded_questions:
            types = sorted({q["questionType"] for q in self.discarded_questions})
            logger.warning(
                f"Melewati {len(self.discarded_questions)} soal bertipe belum didukung: "
                f"{', '.join(types)}. Soal ini akan dikirim kosong."
            )

        return questions_formatted

    def _format_question_for_llm(self, question: dict) -> dict:
        typename = question["__typename"]
        schema = question.get("questionSchema", {})
        payload = {
            "question_id": question["partId"],
            "question_type": QUESTION_TYPE_MAP[typename][1],
            "prompt": schema.get("prompt", {}).get("cmlValue") or schema.get("prompt", {}).get("value") or "",
        }

        if "options" in schema:
            payload["options"] = [
                {
                    "option_id": option.get("optionId", ""),
                    "value": option.get("display", {}).get("cmlValue") or option.get("display", {}).get("value") or "",
                }
                for option in schema["options"]
            ]

        if typename in {"Submission_MultipleChoiceQuestion", "Submission_MultipleChoiceReflectQuestion"}:
            payload["answer_format"] = {
                "multipleChoiceResponse": {
                    "chosen": "single option_id string"
                }
            }
        elif typename in {"Submission_CheckboxQuestion", "Submission_CheckboxReflectQuestion"}:
            payload["answer_format"] = {
                "checkboxResponse": {
                    "chosen": ["option_id", "..."]
                }
            }
        elif typename == "Submission_PlainTextQuestion":
            payload["answer_format"] = {
                "plainTextResponse": {
                    "plainText": "short plain answer"
                }
            }
        else:
            payload["answer_format"] = {
                QUESTION_TYPE_MAP[typename][0]: {
                    "answer": "short answer"
                }
            }

        return payload

    def _blank_response_payload(self, question_id: str) -> dict:
        spec = self.question_specs[question_id]
        return {
            "questionId": question_id,
            "questionType": spec["question_type"],
            "questionResponse": {
                spec["response_field"]: deep_blank_model(MODEL_MAP[spec["typename"]])
            }
        }

    def _coerce_llm_answer(self, question_id: str, answer: dict) -> dict:
        spec = self.question_specs[question_id]
        response_field = spec["response_field"]

        raw_response = answer.get("question_response", {})
        if response_field in raw_response:
            return {
                "questionId": question_id,
                "questionType": spec["question_type"],
                "questionResponse": {
                    response_field: raw_response[response_field]
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
                        response_field: candidate[response_field]
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

        answers_by_id = {}
        for answer in answers or []:
            if not isinstance(answer, dict):
                logger.debug(f"Item jawaban bukan object, dilewati: {str(answer)[:200]}")
                continue
            question_id = answer.get("question_id")
            if question_id:
                answers_by_id[question_id] = answer

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

        try:
            body = res.json()
            errors = (
                body.get("data", {})
                .get("Submission_SaveResponses", {})
                .get("errors", [])
            ) or body.get("errors", [])
            logger.error(f"SaveResponses ditolak server: {str(errors)[:300]}")
            logger.debug(answer_responses)
            logger.debug(body)
        except ValueError:
            logger.error(f"SaveResponses gagal (HTTP {res.status_code}): {res.text[:300]}")
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
