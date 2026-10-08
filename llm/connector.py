import json

import requests
from loguru import logger

import config
from config import SYSTEM_PROMPT

ANSWER_INSTRUCTIONS = (
    "Return JSON only with shape {\"responses\":[...]}. "
    "Each item must include question_id and question_response. "
    "For question_response, use exactly the key and shape requested in "
    "answer_format for that question."
)


def _extract_json(raw_text: str) -> dict:
    """Parse a JSON response, tolerating markdown code fences and extra prose."""
    cleaned = (raw_text or "").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Last resort: grab the first {...} block.
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end > start:
            return json.loads(cleaned[start:end + 1])
        raise


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

    def get_response(self, questions: dict) -> dict:
        logger.debug(f"Making an API request to Gemini ({self.model})...")
        response = self.client.models.generate_content(
            model=self.model,
            contents=f"{ANSWER_INSTRUCTIONS}\n\n{json.dumps(questions)}",
            config=self._types.GenerateContentConfig(
                system_instruction=self.system_prompt,
                response_mime_type="application/json",
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

    def _chat_completions_url(self) -> str:
        # Allow either ".../v1" or a full ".../v1/chat/completions" base URL.
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def _request(self, questions: dict, use_json_mode: bool) -> requests.Response:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user",
                 "content": f"{ANSWER_INSTRUCTIONS}\n\n{json.dumps(questions)}"},
            ],
            "temperature": 0.2,
            "stream": False,
        }
        if use_json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        return requests.post(
            self._chat_completions_url(),
            json=payload,
            headers=headers,
            timeout=self.timeout,
        )

    def get_response(self, questions: dict) -> dict:
        logger.debug(
            f"Making an API request to {self.base_url} "
            f"(model={self.model or 'default'})..."
        )

        response = self._request(questions, use_json_mode=True)
        # Many local routers reject response_format; retry once without it.
        if response.status_code in (400, 404, 415, 422):
            logger.debug(
                f"Endpoint rejected JSON mode ({response.status_code}); "
                "retrying without response_format..."
            )
            response = self._request(questions, use_json_mode=False)

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
