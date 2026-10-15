import json
import time

import requests
from loguru import logger

import skipera.config as config
from skipera.config import SYSTEM_PROMPT

ANSWER_INSTRUCTIONS = (
    "Return JSON only with shape {\"responses\":[...]}. "
    "Each item must include \"question_id\" matching the original question id exactly and "
    "\"question_response\" using the short type code mapping: "
    "MC->{\"multipleChoiceResponse\":{\"chosen\":\"O1\"}}; "
    "MS->{\"checkboxResponse\":{\"chosen\":[\"O1\",\"O2\"]}}; "
    "TXT->{\"plainTextResponse\":{\"plainText\":\"...\"}} for all plain text variants "
    "(Numeric/Math/Regex/TextExactMatch/TextReflect); "
    "RichText->{\"richTextResponse\":{\"richText\":{\"value\":\"...\"}}}. "
    "Do NOT include any answer_format field or additional keys."
)

CONTEXT_INSTRUCTIONS = (
    "You are given a summarized knowledge base of relevant video transcript "
    "excerpts and reading excerpts. Treat it as the COURSE MATERIAL and your "
    "primary source: if it contains information that answers the question, "
    "base your answer on it (paraphrase implicitly, do not invent citations). "
    "Never fabricate; if the material does not cover the answer, fall back to "
    "general knowledge only as a last resort."
)


def _build_user_content(questions: dict, context: str = "") -> str:
    if context and context.strip():
        return (
            f"{CONTEXT_INSTRUCTIONS}\n\n"
            f"--- COURSE MATERIAL START ---\n{context.strip()}\n--- COURSE MATERIAL END ---\n\n"
            "If COURSE MATERIAL covers the answer, you MUST base your answer on it.\n\n"
            f"{ANSWER_INSTRUCTIONS}\n\n{json.dumps(questions)}"
        )
    return f"{ANSWER_INSTRUCTIONS}\n\n{json.dumps(questions)}"


def _extract_json(raw_text: str) -> dict:
    """Parse a JSON response, tolerating markdown fences, prose, and trailing noise.

    Strategy: extract fenced block if present, then decode the first complete
    JSON value with ``raw_decode`` (ignores trailing garbage instead of raising
    "Extra data"). Prefers the object containing a ``responses`` key when the
    model emits several JSON blocks (thinking traces, retries, echoes).
    """
    cleaned = (raw_text or "").strip()
    if not cleaned:
        raise ValueError("Empty LLM response (no JSON to parse)")

    # 1. Prefer fenced code block content (```json ... ``` or ``` ... ```).
    if "```" in cleaned:
        try:
            parts = cleaned.split("```")
            # Odd-indexed parts are inside fences.
            candidates = [p.strip() for i, p in enumerate(parts) if i % 2 == 1 and p.strip()]
            for cand in candidates:
                if cand.startswith("json"):
                    cand = cand[4:].strip()
                try:
                    return json.loads(cand)
                except json.JSONDecodeError:
                    continue
            # Fall through: use the longest fenced block as cleaned text.
            if candidates:
                longest = max(candidates, key=len)
                if longest.startswith("json"):
                    longest = longest[4:].strip()
                cleaned = longest
        except (IndexError, ValueError):
            pass
        else:
            cleaned = cleaned.strip().strip("`").strip()
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].strip()

    # 2. Direct parse (pure JSON).
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()

    def _scan_candidates(text: str):
        """Yield (obj) for each balanced {...}/[...] block, preferring 'responses'."""
        found = []
        idx, n = 0, len(text)
        while idx < n:
            nxt_obj = text.find("{", idx)
            nxt_arr = text.find("[", idx)
            if nxt_obj == -1 and nxt_arr == -1:
                break
            start = nxt_obj
            if nxt_arr != -1 and (nxt_obj == -1 or nxt_arr < nxt_obj):
                start = nxt_arr
            try:
                obj, end = decoder.raw_decode(text, start)
            except json.JSONDecodeError:
                idx = start + 1
                continue
            found.append(obj)
            idx = end
        # Prefer blocks carrying the expected payload.
        found.sort(key=lambda o: 0 if isinstance(o, dict) and "responses" in o else 1)
        return found

    for obj in _scan_candidates(cleaned):
        if isinstance(obj, dict):
            return obj

    # 3. Nothing decoded: raise with a short preview for debuggability.
    preview = cleaned[:300].replace("\n", " ")
    raise ValueError(f"Could not parse LLM JSON (preview: {preview})")


class GeminiConnector(object):
    """Google Gemini via the official SDK."""

    def __init__(self, api_key: str = "", model: str = "", system_prompt: str = SYSTEM_PROMPT):
        from google import genai
        from google.genai import types

        self._types = types
        self.api_key = api_key or config.GEMINI_API_KEY
        self.model = model or config.GEMINI_MODEL
        self.system_prompt = system_prompt
        self.client = genai.Client(api_key=self.api_key)

    def get_response(self, questions: dict, context: str = "", temperature: float = None) -> dict:
        logger.debug(f"Making an API request to Gemini ({self.model})...")
        response = self.client.models.generate_content(
            model=self.model,
            contents=_build_user_content(questions, context),
            config=self._types.GenerateContentConfig(
                system_instruction=self.system_prompt,
                response_mime_type="application/json",
                temperature=0.0 if temperature is None else temperature,
            ),
        )
        raw_text = response.candidates[0].content.parts[0].text
        return _extract_json(raw_text)


class OpenAICompatibleConnector(object):
    """
    Works with any OpenAI-compatible /chat/completions endpoint, including
    OpenRouter, 9Router, Ollama, LM Studio, vLLM, one-api, LiteLLM and
    Perplexity. The base URL must point at the API root (usually ends in /v1).
    """

    def __init__(self, base_url: str, api_key: str = "", model: str = "",
                 system_prompt: str = SYSTEM_PROMPT, timeout: int = 180):
        if not base_url:
            raise ValueError("OpenAI-compatible connector requires a base_url")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.system_prompt = system_prompt
        self.timeout = timeout
        self.temperature = 0.0

    def _chat_completions_url(self) -> str:
        # Allow either ".../v1" or a full ".../v1/chat/completions" base URL.
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    @staticmethod
    def _loopback_url(url: str) -> str:
        """Mirror a URL onto 127.0.0.1 keeping scheme/port/path."""
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(url)
        if parts.hostname in (None, "127.0.0.1", "localhost"):
            return ""
        netloc = f"127.0.0.1:{parts.port}" if parts.port else "127.0.0.1"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))

    def _post(self, payload: dict, headers: dict) -> requests.Response:
        url = self._chat_completions_url()
        try:
            return requests.post(url, json=payload, headers=headers, timeout=self.timeout)
        except requests.exceptions.ConnectionError:
            # Local routers often bind to loopback only, so the machine's own
            # LAN IP (e.g. 192.168.x.x) refuses the connection. Retry on 127.0.0.1.
            alt = self._loopback_url(url)
            if not alt:
                raise
            logger.warning(
                f"Tidak bisa konek ke {url}; mencoba {alt} "
                "(proxy lokal biasanya hanya listen di localhost)..."
            )
            return requests.post(alt, json=payload, headers=headers, timeout=self.timeout)

    def _request(self, questions: dict, use_json_mode: bool, context: str = "",
                 temperature: float = None) -> requests.Response:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user",
                 "content": _build_user_content(questions, context)},
            ],
            "temperature": self.temperature if temperature is None else temperature,
            "top_p": 0.95,
            "stream": False,
        }
        if use_json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        return self._post(payload, headers)

    @staticmethod
    def _is_transient(response: requests.Response) -> bool:
        """True for retryable failures: 429, 5xx, or an 'unavailable'/overloaded body."""
        if response.status_code == 429 or response.status_code >= 500:
            return True
        if response.status_code == 400:
            body = (response.text or "").lower()
            return any(k in body for k in
                       ("unavailable", "server_error", "overloaded"))
        return False

    def get_response(self, questions: dict, context: str = "", temperature: float = None) -> dict:
        logger.debug(
            f"Making an API request to {self.base_url} "
            f"(model={self.model or 'default'})..."
        )

        response = None
        for attempt in range(1, 4):
            response = self._request(questions, use_json_mode=True, context=context, temperature=temperature)
            # Many local routers reject response_format; retry once without it.
            if response.status_code in (400, 404, 415, 422):
                logger.debug(
                    f"Endpoint rejected JSON mode ({response.status_code}); "
                    "retrying without response_format..."
                )
                response = self._request(questions, use_json_mode=False, context=context, temperature=temperature)
            # Retry transient router failures (429 / 5xx / "unavailable"/overloaded);
            # non-transient errors fall through to the raise below unchanged.
            if self._is_transient(response) and attempt < 3:
                logger.warning(
                    f"LLM transient error ({response.status_code}); retry {attempt}/3..."
                )
                time.sleep(1.5 * attempt)
                continue
            break

        if response.status_code >= 400:
            raise RuntimeError(
                f"{response.status_code} {response.reason}: {response.text[:300]}"
            )

        data = response.json()
        try:
            raw_text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(f"Unexpected response shape: {str(data)[:300]}")

        return _extract_json(raw_text)


def build_connector(settings: dict = None):
    """Build the right connector from resolved settings (provider-aware)."""
    settings = settings or config.resolve_llm_settings()
    provider = settings.get("provider", "")

    if provider == "gemini":
        return GeminiConnector(
            api_key=settings.get("api_key", ""),
            model=settings.get("model", ""),
        )

    return OpenAICompatibleConnector(
        base_url=settings.get("base_url", ""),
        api_key=settings.get("api_key", ""),
        model=settings.get("model", ""),
    )
