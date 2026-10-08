import requests

from loguru import logger


def list_gemini_models(api_key: str) -> list:
    """Return model ids that support generateContent for a Gemini API key."""
    from google import genai

    client = genai.Client(api_key=api_key)
    models = []
    for model in client.models.list():
        name = (getattr(model, "name", "") or "").replace("models/", "")
        actions = (
            getattr(model, "supported_actions", None)
            or getattr(model, "supported_generation_methods", None)
            or []
        )
        if name and (not actions or "generateContent" in actions):
            models.append(name)
    return sorted(models)


def list_openai_models(base_url: str, api_key: str = "") -> list:
    """Return model ids from an OpenAI-compatible /models endpoint."""
    from urllib.parse import urlsplit, urlunsplit

    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    url = f"{base_url.rstrip('/')}/models"
    try:
        response = requests.get(url, headers=headers, timeout=30)
    except requests.exceptions.ConnectionError:
        parts = urlsplit(url)
        if parts.hostname in (None, "127.0.0.1", "localhost"):
            raise
        netloc = f"127.0.0.1:{parts.port}" if parts.port else "127.0.0.1"
        alt = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
        logger.warning(f"Tidak bisa konek ke {url}; mencoba {alt} ...")
        response = requests.get(alt, headers=headers, timeout=30)

    response.raise_for_status()
    payload = response.json()
    return sorted(item["id"] for item in payload.get("data", []) if item.get("id"))


def list_models(settings: dict) -> list:
    """List available models for the resolved provider."""
    if settings.get("provider") == "gemini":
        return list_gemini_models(settings.get("api_key", ""))
    return list_openai_models(settings.get("base_url", ""), settings.get("api_key", ""))


def ensure_model_available(settings: dict) -> list:
    """
    Best-effort availability check. Returns the available models (or [] on any
    error) and logs a warning if the configured model is not among them.
    """
    provider = settings.get("provider")
    model = settings.get("model")
    if not model:
        return []
    try:
        available = list_models(settings)
    except Exception as exc:
        logger.debug(f"Could not list models for availability check: {exc}")
        return []

    if available and model not in available:
        logger.warning(
            f"Model '{model}' tidak ada di daftar model {provider} yang tersedia. "
            "Jalankan dengan --list-models untuk melihat pilihan."
        )
    return available
