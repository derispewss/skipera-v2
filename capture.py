"""
Local Playwright recorder to reverse-engineer Coursera's UI-only features
(Coursera Coach "Role Play" / "Dialogue", discussion prompts, programming labs).

Why: those items are rendered by a JavaScript SPA and talk to endpoints that are
not discoverable without a logged-in session. Instead of guessing DOM selectors,
open a real (persistent profile) browser, do the item once, and record every
`/api` + graphql request. From the captured calls we can build a robust HTTP
client — no brittle selectors.

Install:
    pip install playwright && playwright install chromium

Usage:
    skipera --capture https://www.coursera.org/learn/<slug>/coach/<itemId>/<slug>
"""
import json
import time
from pathlib import Path

from loguru import logger

DEFAULT_PROFILE = Path.home() / ".skipera" / "browser-profile"
CAPTURE_HINTS = ("/api/", "/graphql", "graphql-gateway")


def _record(request, records: list) -> None:
    url = request.url
    if not any(hint in url for hint in CAPTURE_HINTS):
        return
    entry = {"method": request.method, "url": url}
    try:
        if request.method in ("POST", "PUT", "PATCH"):
            entry["post_data"] = request.post_data
    except Exception:
        pass
    records.append(entry)


def run_capture(url: str, out_path: str = "skipera_capture.json",
                profile_dir: str = "", timeout_s: int = 900) -> str:
    """
    Open a headed browser with a persistent profile, record /api + graphql
    requests until the user presses Enter, then save them to out_path.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error(
            "Playwright belum terpasang. Jalankan:\n"
            "  pip install playwright && playwright install chromium"
        )
        raise SystemExit(1)

    profile = Path(profile_dir) if profile_dir else DEFAULT_PROFILE
    profile.mkdir(parents=True, exist_ok=True)
    records: list = []

    logger.info(f"Profil browser: {profile} (login Coursera di sini kalau diminta)")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(profile), headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.on("request", lambda req: _record(req, records))
        page.goto(url, wait_until="domcontentloaded")

        logger.info(
            "Browser terbuka. Login bila perlu, lalu kerjakan SATU item "
            "(Role Play / Dialogue / discussion) secara manual."
        )
        logger.info("Setelah selesai, kembali ke terminal ini dan tekan Enter untuk menyimpan hasil rekaman.")
        try:
            input()
        except EOFError:
            logger.warning(f"Non-interaktif: merekam selama {timeout_s}s...")
            time.sleep(timeout_s)

        Path(out_path).write_text(json.dumps(records, indent=2))
        context.close()

    logger.success(f"Terekam {len(records)} request -> {out_path}")
    logger.info("Kirim isi file ini agar pemanggilan API-nya bisa dibuat (tanpa Playwright).")
    return out_path
