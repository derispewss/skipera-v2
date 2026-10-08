import json
import os
import sys
from pathlib import Path

from loguru import logger

CONFIG_DIR = Path.home() / ".skipera"
CONFIG_FILE = CONFIG_DIR / "config.json"


def _read_dotenv() -> dict:
    """Best-effort .env reader for local runs without requiring python-dotenv."""
    env_values = {}
    candidates = [Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"]

    for env_file in candidates:
        if not env_file.exists():
            continue

        for raw_line in env_file.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in env_values:
                env_values[key] = value

    return env_values


DOTENV_VALUES = _read_dotenv()

DEFAULT_CONFIG = {
    "cookies": {},
    # Generic / dynamic LLM settings (OpenAI-compatible endpoints)
    "llm_provider": "",
    "llm_base_url": "",
    "llm_api_key": "",
    "llm_model": "",
    # Legacy provider-specific settings
    "perplexity_api_key": "",
    "gemini_api_key": "",
    "perplexity_model": "sonar-pro",
    "gemini_model": "gemini-3-flash-preview",
}

# Well-known OpenAI-compatible endpoints, so a provider can be selected by name.
PROVIDER_PRESETS = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "perplexity": "https://api.perplexity.ai",
    "local": "http://192.168.18.11:8045/v1",
    "9router": "http://192.168.18.11:8045/v1",
}


def fetch_browser_cookies() -> dict:
    try:
        import browser_cookie3
    except ImportError:
        logger.error("browser-cookie3 not installed. Run: pip install browser-cookie3")
        return {}

    browsers = [
        ("Chrome", "chrome"),
        ("Chromium", "chromium"),
        ("Brave", "brave"),
        ("Firefox", "firefox"),
        ("Edge", "edge"),
        ("Opera", "opera"),
        ("Vivaldi", "vivaldi"),
    ]

    for name, fn_name in browsers:
        browser_fn = getattr(browser_cookie3, fn_name, None)
        if browser_fn is None:
            continue

        try:
            cj = browser_fn(domain_name=".coursera.org")
            cookies = {c.name: c.value for c in cj}
            if "CAUTH" in cookies:
                logger.success(f"Fetched Coursera cookies from {name}")
                return cookies
        except Exception:
            continue

    logger.warning("Could not find Coursera cookies in supported browsers. Make sure you're logged into Coursera and fully close the browser before retrying.")
    return {}


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2))

    config = json.loads(CONFIG_FILE.read_text())

    if not config.get("cookies"):
        env_cauth = os.getenv("COURSERA_CAUTH", "") or os.getenv("CAUTH", "")
        if not env_cauth:
            env_cauth = DOTENV_VALUES.get("COURSERA_CAUTH", "") or DOTENV_VALUES.get("CAUTH", "")

        if env_cauth:
            config["cookies"] = {"CAUTH": env_cauth}
            CONFIG_FILE.write_text(json.dumps(config, indent=2))
            logger.info(f"Cookies loaded from environment and saved to {CONFIG_FILE}")
            return config

        logger.info("No cookies in config — attempting to fetch from browser...")
        cookies = fetch_browser_cookies()
        if cookies:
            config["cookies"] = cookies
            CONFIG_FILE.write_text(json.dumps(config, indent=2))
            logger.info(f"Cookies saved to {CONFIG_FILE}")
        else:
            logger.error(f"No cookies found. Log into Coursera in your browser and retry, or manually edit {CONFIG_FILE}")
            sys.exit(1)

    return config


_config = load_config()

# URLs (constant, not user-configurable)
BASE_URL = "https://www.coursera.org/api/"
GRAPHQL_URL = "https://www.coursera.org/graphql-gateway"
PERPLEXITY_API_URL = "https://api.perplexity.ai/chat/completions"

# User-configurable
COOKIES = _config["cookies"]
PERPLEXITY_API_KEY = _config.get("perplexity_api_key", "")
GEMINI_API_KEY = _config.get("gemini_api_key", "")
if not GEMINI_API_KEY:
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
if not GEMINI_API_KEY:
    GEMINI_API_KEY = os.getenv("GOOGLE_API_KEY", "")
if not GEMINI_API_KEY:
    GEMINI_API_KEY = DOTENV_VALUES.get("GEMINI_API_KEY", "")
if not GEMINI_API_KEY:
    GEMINI_API_KEY = DOTENV_VALUES.get("GOOGLE_API_KEY", "")
PERPLEXITY_MODEL = _config.get("perplexity_model", "sonar-pro")
GEMINI_MODEL = (
    os.getenv("GEMINI_MODEL", "")
    or DOTENV_VALUES.get("GEMINI_MODEL", "")
    or _config.get("gemini_model", "")
    or "gemini-3-flash-preview"
)

# Generic LLM settings, resolved from env > .env > config.json.
LLM_PROVIDER = (
    os.getenv("LLM_PROVIDER", "")
    or DOTENV_VALUES.get("LLM_PROVIDER", "")
    or _config.get("llm_provider", "")
).strip().lower()
LLM_BASE_URL = (
    os.getenv("LLM_BASE_URL", "")
    or DOTENV_VALUES.get("LLM_BASE_URL", "")
    or _config.get("llm_base_url", "")
).strip().rstrip("/")
LLM_API_KEY = (
    os.getenv("LLM_API_KEY", "")
    or DOTENV_VALUES.get("LLM_API_KEY", "")
    or _config.get("llm_api_key", "")
    or os.getenv("OPENROUTER_API_KEY", "")
    or DOTENV_VALUES.get("OPENROUTER_API_KEY", "")
).strip()
LLM_MODEL = (
    os.getenv("LLM_MODEL", "")
    or DOTENV_VALUES.get("LLM_MODEL", "")
    or _config.get("llm_model", "")
).strip()

SYSTEM_PROMPT = (
    "Answer all provided assessment questions and return strict JSON only. "
    "Use each question's answer_format exactly as the response shape. "
    "Be concise and deterministic. Ignore HTML noise in prompts/options."
)


def provider_implies_openai(base_url: str) -> bool:
    """Gemini is the only non-OpenAI provider; anything else with a URL is OpenAI-compatible."""
    if not base_url:
        return False
    return "generativelanguage.googleapis.com" not in base_url


def resolve_llm_settings(provider: str = "", base_url: str = "", model: str = "",
                         api_key: str = "") -> dict:
    """
    Merge CLI overrides with environment/config so the connector can be chosen
    dynamically at runtime.

    Priority for the endpoint:
      1. explicit base_url argument (CLI --base-url)
      2. named provider preset (CLI/env --provider openrouter|local|9router|...)
      3. LLM_BASE_URL from env/.env/config
    """
    provider = (provider or "").strip().lower()
    base_url = (base_url or "").strip().rstrip("/")
    model = (model or "").strip()
    api_key = (api_key or LLM_API_KEY or "").strip()

    if base_url:
        resolved_provider = provider or LLM_PROVIDER or ("openai" if provider_implies_openai(base_url) else "gemini")
        resolved_base = base_url
    elif provider:
        resolved_provider = provider
        resolved_base = PROVIDER_PRESETS.get(provider, LLM_BASE_URL)
    else:
        resolved_provider = LLM_PROVIDER
        resolved_base = LLM_BASE_URL
        if not resolved_provider:
            resolved_provider = "openai" if provider_implies_openai(resolved_base) else "gemini"

    if resolved_provider == "gemini":
        api_key = api_key or GEMINI_API_KEY
        model = model or GEMINI_MODEL
        resolved_base = ""
    else:
        model = model or LLM_MODEL
        if resolved_provider == "perplexity":
            api_key = api_key or PERPLEXITY_API_KEY
            model = model or PERPLEXITY_MODEL
        if not resolved_base and resolved_provider == "openai":
            resolved_base = PROVIDER_PRESETS["openai"]

    return {
        "provider": resolved_provider,
        "base_url": resolved_base,
        "api_key": api_key,
        "model": model,
    }

HEADERS = {
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
    'x-coursera-application': 'ondemand',
    'x-coursera-version': '3bfd497de04ae0fef167b747fd85a6fbc8fb55df',
    'x-requested-with': 'XMLHttpRequest',
}
