"""Knowledge base per course: transcripts (lecture) + reading texts (supplement).

One Markdown file per course::

    knowledge/<slug>.md

Each lecture/supplement appends one section (deduplicated by ``item_id``).
The file doubles as the knowledge base for quiz solving: when an
assessment is solved with ``--llm``, the collected text is injected into
the LLM prompt so answers are grounded in the actual course material
instead of the model's general knowledge.
"""

import html
import re
from pathlib import Path
from urllib.parse import urljoin
from loguru import logger

COURSERA_ORIGIN = "https://www.coursera.org"
SECTION_SEPARATOR = "\n\n---\n\n"

# Small stopword list (EN + ID) for the extractive summarizer.
STOPWORDS = frozenset(
    "the and for are was with this that from have will your our their there "
    "what when where which then been about more these those also but not all can "
    "yang dan untuk dengan dari pada dalam akan ini itu adalah atau karena tidak "
    "kita anda mereka juga saat bisa dapat".split()
)

# Filler / non-informative phrases dropped from summaries.
FILLER_PHRASES = (
    "hello", "welcome", "subscribe", "jangan lupa", "don't forget",
    "thank you", "terima kasih", "stay tuned", "like and subscribe",
    "comment below", "follow us",
)


def sanitize_filename(name: str, max_len: int = 80) -> str:
    """Make a menu name safe to use as a file name (keeps it readable)."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name or "").strip()
    cleaned = re.sub(r"\s+", "-", cleaned)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    if not cleaned:
        cleaned = "item"
    return cleaned[:max_len]


def srt_to_text(srt: str) -> str:
    """Strip SRT/VTT timestamps + counters, return flowing plain text."""
    lines = []
    for raw in (srt or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.upper() == "WEBVTT":
            continue
        if line.isdigit():
            continue
        if "-->" in line:
            continue
        if line.startswith(("NOTE ", "STYLE", "REGION")):
            continue
        # VTT cue settings after timestamp are already skipped above.
        if lines and lines[-1] == line:
            continue  # dedupe consecutive repeats
        lines.append(line)
    text = " ".join(lines)
    return re.sub(r"\s+", " ", text).strip()


def cml_to_markdown(cml: str) -> str:
    """Convert Coursera CML (XML-ish) supplement content to Markdown."""
    if not cml:
        return ""
    out = cml

    # Headings: <heading level="1">..</heading>
    def _heading(m):
        level = m.group(1)
        try:
            hashes = "#" * min(int(level), 6)
        except ValueError:
            hashes = "##"
        return f"\n\n{hashes} {m.group(2).strip()}\n\n"

    out = re.sub(r'<heading\s+level="(\d+)">(.*?)</heading>', _heading, out, flags=re.DOTALL)
    # Paragraphs
    out = re.sub(r"<text>(.*?)</text>", lambda m: f"\n\n{m.group(1).strip()}\n\n", out, flags=re.DOTALL)
    # Links: <a href="..">label</a>
    out = re.sub(
        r'<a\s+href="([^"]+)">(.*?)</a>',
        lambda m: f"[{m.group(2).strip()}]({m.group(1).strip()})",
        out,
        flags=re.DOTALL,
    )
    # List items
    out = re.sub(r"<li>(.*?)</li>", lambda m: f"\n- {m.group(1).strip()}", out, flags=re.DOTALL)
    # Table cells/rows -> pipe rows
    out = re.sub(r"</tr\s*>", "\n", out)
    out = re.sub(r"</td\s*>", " | ", out)
    out = re.sub(r"<table[^>]*>", "\n\n", out)
    out = re.sub(r"</table\s*>", "\n\n", out)
    # Code / images
    out = re.sub(r"<pre>(.*?)</pre>", lambda m: f"\n\n```\n{m.group(1).strip()}\n```\n\n", out, flags=re.DOTALL)
    out = re.sub(r"<code>(.*?)</code>", lambda m: f"`{m.group(1).strip()}`", out, flags=re.DOTALL)
    out = re.sub(r'<img[^>]*src="([^"]+)"[^>]*>', lambda m: f"\n\n![]({m.group(1).strip()})\n\n", out)
    # Drop any remaining tags
    out = re.sub(r"<[^>]+>", "", out)
    out = html.unescape(out)
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def fetch_lecture_transcript(session, base_url: str, course_id: str, item_id: str,
                              lang: str = "en") -> str:
    """Download a lecture's subtitle track and return it as plain text."""
    r = session.get(
        base_url + f"onDemandLectureVideos.v1/{course_id}~{item_id}",
        params={"includes": "video"},
        timeout=60,
    )
    if r.status_code != 200:
        return ""
    try:
        videos = r.json().get("linked", {}).get("onDemandVideos.v1", [])
    except ValueError:
        return ""
    if not videos:
        return ""
    subtitles = videos[0].get("subtitles", {}) or {}
    # Preferred language -> English -> any available track.
    track = subtitles.get(lang) or subtitles.get("en")
    if not track and subtitles:
        track = next(iter(subtitles.values()))
    if not track:
        return ""
    sub = session.get(urljoin(COURSERA_ORIGIN, track), timeout=60)
    if sub.status_code != 200 or not sub.text.strip():
        return ""
    return srt_to_text(sub.text)


def fetch_supplement_markdown(session, base_url: str, course_id: str, item_id: str) -> str:
    """Download a reading's CML asset and return it as Markdown."""
    r = session.get(
        base_url + f"onDemandSupplements.v1/{course_id}~{item_id}",
        params={"includes": "asset"},
        timeout=60,
    )
    if r.status_code != 200:
        return ""
    try:
        assets = r.json().get("linked", {}).get("openCourseAssets.v1", [])
    except ValueError:
        return ""
    for asset in assets:
        definition = asset.get("definition", {}) or {}
        value = definition.get("value", "")
        if value:
            return cml_to_markdown(value)
    return ""


def default_knowledge_dir() -> Path:
    return Path.cwd() / "knowledge"


def knowledge_path(knowledge_dir: str | Path, course_slug: str) -> Path:
    safe = sanitize_filename(course_slug, max_len=100).lower() or "course"
    base = Path(knowledge_dir) if knowledge_dir else default_knowledge_dir()
    return base / f"{safe}.md"


def _marker(item_id: str) -> str:
    return f"<!-- skipera:item_id={item_id} -->"


def _build_section(order: int, item: dict, kind: str, text: str, url: str = "") -> str:
    lines = [
        f"## {order:03d} — {item.get('name', '')} ({kind})",
        _marker(item.get("id", "")),
    ]
    if url:
        lines.append(f"URL: {url}")
    lines += ["", text.strip()]
    return "\n".join(lines)


def _replace_section(content: str, item_id: str, section: str) -> str:
    """Swap the section carrying ``item_id`` with a fresh one."""
    chunks = content.split(SECTION_SEPARATOR)
    marker = _marker(item_id)
    for i, chunk in enumerate(chunks):
        if marker in chunk:
            chunks[i] = section
            break
    else:
        chunks.append(section)
    return SECTION_SEPARATOR.join(chunks)


# --- Extractive summarization (no LLM, no extra deps) ---


def _tokenize(text: str) -> list:
    """Lowercase alphanumeric words (len > 2, stopwords removed)."""
    words = re.findall(r"[a-zA-Z0-9]+", (text or "").lower())
    return [w for w in words if len(w) > 2 and w not in STOPWORDS]


def _split_sections(content: str) -> list:
    """Split knowledge content into non-empty sections."""
    return [chunk.strip() for chunk in content.split(SECTION_SEPARATOR)
            if chunk.strip()]


def _truncate_leading(section: str, limit: int) -> str:
    """Keep the head of a section (heading + first lines) up to limit."""
    if len(section) <= limit:
        return section
    cut = section[:limit]
    nl = cut.rfind("\n")
    if nl > len(cut) * 0.5:
        cut = cut[:nl]
    return cut.strip()


def summarize_text(text: str, budget: int = 1500) -> str:
    """Extractive summary: keep headings/bullets, top sentences till budget.

    Scores each sentence by word-frequency mass, a position bonus for the
    first 20% of the text, and a bonus for numbers / definition-style
    sentences. Duplicate (>0.85 token overlap) and filler sentences are
    dropped. Surviving sentences are returned in original order.
    """
    if not text or not text.strip():
        return ""
    text = text.strip()
    if len(text) <= budget:
        return text

    lines = text.splitlines()
    tokens = _tokenize(text)
    if not tokens:
        return text  # nothing scorable — keep as-is

    freq: dict = {}
    for token in tokens:
        freq[token] = freq.get(token, 0) + 1

    # Units: (orig_line_index, content). Headings/bullets are priority.
    priority: list = []
    sentences: list = []
    pending: list = []
    pending_start = 0

    def flush_pending():
        nonlocal pending, pending_start
        start = pending_start
        buffered = re.sub(r"\s+", " ", " ".join(pending)).strip()
        pending, pending_start = [], 0
        if not buffered:
            return
        for sent in re.split(r"[.!?\n]+", buffered):
            sent = re.sub(r"\s+", " ", sent).strip()
            if sent:
                sentences.append((start, sent))

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            flush_pending()
            priority.append((i, line))
        elif stripped.startswith(("-", "*", "+")) or re.match(r"^\d+[.)]", stripped):
            flush_pending()
            priority.append((i, line))
        else:
            if not pending:
                pending_start = i
            pending.append(line)
    flush_pending()

    def sentence_score(unit) -> float:
        idx, sent = unit
        sent_tokens = _tokenize(sent)
        if not sent_tokens:
            return 0.0
        score = float(sum(freq.get(t, 0) for t in sent_tokens))
        if (idx / max(len(lines) - 1, 1)) < 0.2:
            score *= 1.2  # position bonus for the opening 20%
        lower = sent.lower()
        if re.search(r"\d", sent):
            score += 0.5
        if ":" in sent or "adalah" in lower or " is " in lower or "means" in lower:
            score += 1.0
        return score

    def overlap_similarity(a: str, b: str) -> float:
        ta, tb = set(_tokenize(a)), set(_tokenize(b))
        if not ta or not tb:
            return 0.0
        return len(ta & tb) / min(len(ta), len(tb))

    ranked = sorted(sentences, key=sentence_score, reverse=True)
    picked: list = []
    accepted: list = []
    used = sum(len(c) for _, c in priority) + len(priority)
    for idx, sent in ranked:
        lower = sent.lower()
        if any(f in lower for f in FILLER_PHRASES):
            continue
        if any(overlap_similarity(sent, prev) > 0.85 for prev in accepted):
            continue
        if used + len(sent) + 1 > budget:
            continue
        accepted.append(sent)
        picked.append((idx, sent))
        used += len(sent) + 1

    ordered = sorted(priority + picked, key=lambda unit: unit[0])
    if not ordered:
        return text[:budget].strip()
    return "\n".join(content for _, content in ordered).strip()


def summarize_section(section_text: str, budget: int = 1500) -> str:
    """Summarize a section body, preserving its header (heading/marker/URL)."""
    if not section_text or not section_text.strip():
        return ""
    lines = section_text.strip().splitlines()
    header_count = 3 if len(lines) > 2 and lines[2].startswith("URL:") else 2
    header = "\n".join(lines[:header_count])
    body = "\n".join(lines[header_count:]).strip()
    summarized = summarize_text(body, budget)
    if not summarized:
        return header
    return header + "\n" + summarized


def _write_summary_section(base: Path, course_slug: str, order: int,
                           item: dict, kind: str, text: str, url: str = "") -> None:
    """Write/refresh one summarized section inside ``<slug>.summary.md``.

    When the summary file does not exist yet it is seeded from every
    section already present in the full knowledge file (backward compat
    with runs that predate summaries).
    """
    summary_file = summary_path(base, course_slug)
    section = summarize_section(_build_section(order, item, kind, text, url))

    if summary_file.exists():
        content = summary_file.read_text(encoding="utf-8")
        content = _replace_section(content, item.get("id", ""), section)
        summary_file.write_text(content, encoding="utf-8")
        return

    body = section
    try:
        raw = knowledge_path(base, course_slug).read_text(encoding="utf-8")
        raw = re.sub(r"\A# Knowledge:.*?\n", "", raw, count=1)
        existing = _split_sections(raw)
        if existing:
            body = SECTION_SEPARATOR.join(summarize_section(s) for s in existing)
    except OSError:
        pass
    summary_file.write_text(
        f"# Knowledge summary: {course_slug}\n" + SECTION_SEPARATOR + body + "\n",
        encoding="utf-8")


def _query_to_text(query) -> str:
    """Flatten a questions dict (or raw string) into plain text."""
    if query is None:
        return ""
    if isinstance(query, str):
        return query
    if isinstance(query, dict):
        parts = []
        for value in query.values():
            if isinstance(value, dict):
                parts.append(str(value.get("prompt", "")))
                for opt in value.get("options", []) or []:
                    if isinstance(opt, dict):
                        parts.append(str(opt.get("value", "")))
                    else:
                        parts.append(str(opt))
            else:
                parts.append(str(value))
        return " ".join(parts)
    return str(query)


def summary_path(knowledge_dir: str | Path, course_slug: str) -> Path:
    """Path to the per-course summary file (``knowledge/<slug>.summary.md``)."""
    full = knowledge_path(knowledge_dir, course_slug)
    return full.with_name(full.stem + ".summary.md")


def has_knowledge(course_slug: str, knowledge_dir: str | Path, item_id: str) -> bool:
    """True when the course file already holds a section for ``item_id``."""
    path = knowledge_path(knowledge_dir, course_slug)
    if not path.is_file():
        return False
    try:
        return _marker(item_id) in path.read_text(encoding="utf-8")
    except OSError:
        return False


def save_knowledge(course_slug: str, knowledge_dir: str | Path, order: int,
                   item: dict, kind: str, text: str, url: str = "",
                   overwrite: bool = False) -> str | None:
    """Append one section to ``knowledge/<slug>.md`` (skips existing items)."""
    if not text or not text.strip():
        return None
    base = Path(knowledge_dir) if knowledge_dir else default_knowledge_dir()
    base.mkdir(parents=True, exist_ok=True)
    path = knowledge_path(base, course_slug)
    section = _build_section(order, item, kind, text, url)
    marker = _marker(item.get("id", ""))

    if path.exists():
        content = path.read_text(encoding="utf-8")
        if marker in content:
            if not overwrite:
                return str(path)  # cached — do not refetch/rewrite
            content = _replace_section(content, item.get("id", ""), section)
            path.write_text(content, encoding="utf-8")
            logger.info(f"Knowledge diperbarui: {path} ({item.get('name', '')})")
            _write_summary_section(base, course_slug, order, item, kind, text, url)
            return str(path)
        content = content.rstrip("\n") + SECTION_SEPARATOR + section + "\n"
        path.write_text(content, encoding="utf-8")
    else:
        path.write_text(f"# Knowledge: {course_slug}\n" + SECTION_SEPARATOR + section + "\n",
                        encoding="utf-8")
    logger.info(f"Knowledge tersimpan: {path} (+{len(text)} karakter)")
    _write_summary_section(base, course_slug, order, item, kind, text, url)
    return str(path)


def load_knowledge_context(knowledge_dir: str | Path, course_slug: str,
                           max_chars: int = 8000, query=None) -> str:
    """Read the summarized knowledge, keeping within ``max_chars``.

    The ``.summary.md`` file wins; when it is missing, the full file is
    summarized on the fly per section (backward compat). With a query
    (questions dict or string), sections rank by relevance; without one,
    the most recent sections win (tail semantics like before).
    """
    summary_file = summary_path(knowledge_dir, course_slug)
    full_file = knowledge_path(knowledge_dir, course_slug)

    content = ""
    source_path = full_file
    source_label = "ringkasan"
    if summary_file.is_file():
        source_path = summary_file
        try:
            content = summary_file.read_text(encoding="utf-8")
        except OSError:
            content = ""
    if not content and full_file.is_file():
        source_label = "ringkasan on-the-fly"
        try:
            raw = full_file.read_text(encoding="utf-8")
            raw = re.sub(r"\A# Knowledge:.*?\n", "", raw, count=1)
            content = SECTION_SEPARATOR.join(
                summarize_section(s) for s in _split_sections(raw)
            )
        except OSError:
            content = ""
    if not content:
        return ""

    content = re.sub(r"\A# Knowledge( summary)?:.*?\n", "", content, count=1).strip()
    sections = _split_sections(content)
    if not sections:
        return ""

    query_tokens = set(_tokenize(_query_to_text(query))) if query else set()

    selected = []
    if query_tokens:
        scored = []
        for sec in sections:
            sec_tokens = _tokenize(sec)
            if not sec_tokens:
                continue
            overlap = len(query_tokens & set(sec_tokens))
            score = overlap / ((len(sec_tokens) + 1) ** 0.5)
            scored.append((score, sec))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        used = 0
        for score, sec in scored:
            if score <= 0:
                continue
            if used + len(sec) > max_chars:
                if not selected:
                    selected.append(_truncate_leading(sec, max_chars))
                elif max_chars - used > 0:
                    selected.append(_truncate_leading(sec, max_chars - used))
                break
            selected.append(sec)
            used += len(sec)
    else:
        used = 0
        for i in range(len(sections) - 1, -1, -1):
            sec = sections[i]
            if used + len(sec) > max_chars:
                if not selected:
                    selected.insert(0, _truncate_leading(sec, max_chars))
                elif max_chars - used > 0:
                    selected.insert(0, _truncate_leading(sec, max_chars - used))
                break
            selected.insert(0, sec)
            used += len(sec)

    result = SECTION_SEPARATOR.join(selected).strip()
    logger.info(
        f"Knowledge context: {source_path} ({len(result)} karakter, "
        f"dari {len(selected)} section, {source_label})"
    )
    return result
