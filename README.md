# Skipera V2
<img width="96" height="96" alt="image" src="https://github.com/user-attachments/assets/8cf9428c-ef58-45b2-8fff-184ae353a890" />

Module to facilitate skipping Coursera (https://www.coursera.org/) videos and assessments.

> **V2 highlights:** dynamic LLM providers (Gemini, OpenRouter, local OpenAI-compatible routers), model listing + availability checks, per-item completion summary, and read-only `--diagnose` for unsupported item types (Role Play / Dialogue / etc.).

## Why?
Skipera assists in automatically skipping irrelevant MOOC courses which are made mandatory by universities.
Many of such courses are allotted directly by the university as credit fillers and are not in the interest of the student. The progress of the completion of these courses is tracked by the university and credits are allotted.

## How?
Skipera makes use of the Coursera web API and completes the videos + reading materials.
Graded assessments are completed with the assistance of an LLM API.

## Installation

### Conda (recommended)

```bash
conda env create -f environment.yml
conda activate skipera
playwright install chromium   # fallback only; Google Chrome is used automatically if installed
```

### pip

```bash
pip install -e .
```

Or install the published package:

```bash
pip install skipera
```

The project uses a **`src` layout** — all code lives in the `skipera` package under `src/skipera/` (`cli.py`, `config.py`, `report.py`, `capture.py`, `knowledge.py`, plus the `assessment/`, `llm/`, `watcher/` subpackages).
A thin `main.py` shim stays in the repository root so `python main.py` keeps working; the recommended entry points are `skipera` and `python -m skipera`.

## Configuration

On first run, skipera creates a config file at `~/.skipera/config.json`.

### Environment file (`.env`)

The quickest way to configure everything is a `.env` file. A ready-to-use template is committed in the repo:

```bash
cp .env.example .env
```

Then open `.env` and fill in just two things:

- `CAUTH` — your Coursera session cookie (see below), and
- either `GEMINI_API_KEY` (Gemini), **or** the local AI block (`LLM_PROVIDER=local`, `LLM_BASE_URL=http://127.0.0.1:8045/v1`, `LLM_MODEL=...`).

That's it — `.env` is gitignored, `.env.example` is the shared template.

### Cookies (automatic)

If you're logged into Coursera in your browser (Chrome, Firefox, or Edge), skipera will automatically fetch the required cookies. Just run the command and it handles the rest. Expired cookies are also re-fetched automatically.

> **Note:** On Windows, Chrome must be closed for cookie fetching to work. On macOS, you may see a Keychain access prompt.

### Cookies (manual)

If automatic fetching doesn't work, you can manually add your cookies to the config file:

```json
{
  "cookies": {
    "CAUTH": "...",
    "CSRF3-Token": "...",
    "__204u": "..."
  }
}
```

To find your cookies:
1. Open your browser (Chrome, Firefox, or Edge)
2. Go to [Coursera](https://www.coursera.org/) and make sure you're **logged in**
3. Right-click anywhere on the page > **Inspect** (or press **F12**)
4. Navigate to the **Application** tab
5. In the left sidebar, expand **Cookies** and select **`https://www.coursera.org`**
6. Find the cookie named **`CAUTH`** and copy its **Value**

## Usage

```bash
skipera course-slug
```

Where `course-slug` is from the Coursera URL. For example, if the URL is `https://www.coursera.org/learn/introduction-psychology/home/module/2`, run:

```bash
skipera introduction-psychology
```

## Item types

Skipera classifies each course item by its `contentSummary.typeName`:

| `typeName` | Display / meaning | Behavior |
| --- | --- | --- |
| `lecture` | video | skipped when allowed, else watched |
| `supplement` | reading | marked complete |
| `ungradedAssignment` | practice quiz | solved with `--llm` |
| `staffGraded` | graded quiz | solved with `--llm` |
| `ungradedWidget` / `ungradedLti` | widget / external lab | auto-complete attempted |
| `discussionPrompt` | discussion prompt | **manual** — post in the forum (usually optional) |
| `coach` | **Role Play / Dialogue** | **manual** — live Coursera Coach AI conversation |
| `gradedProgramming` | programming assignment | **manual** — run code in the lab |
| `gradedLti` | graded external tool | **manual** |
| `peerGraded` / `peerReview` / `phasedPeer` | peer review | **manual** |

> **Research note:** "Role Play" and "Dialogue" are not separate types — both are `coach` with a `customDisplayTypenameOverride`. They are API‑driven by Coursera's coach service and cannot be accessed via the public REST endpoints Skipera uses, so they remain manual.

## LLM Support

If you wish to solve graded assignments automatically, add your API key to the config file and use the `--llm` flag:

```bash
skipera introduction-psychology --llm
```

### Any OpenAI‑compatible endpoint (dynamic providers)

Skipera can talk to any OpenAI‑compatible `/chat/completions` endpoint: OpenRouter, a local router (9Router, LiteLLM, one‑api), Ollama, LM Studio, vLLM, etc. Configure it once in `.env` or `~/.skipera/config.json`:

```ini
LLM_PROVIDER=openai            # gemini | openai | openrouter | 9router | local | perplexity
LLM_BASE_URL=http://127.0.0.1:8045/v1
LLM_API_KEY=                   # usually empty for a local router
LLM_MODEL=gemini-3.8-flash-tiered
```

Or override per run without editing files:

```bash
skipera introduction-psychology --llm --provider openrouter --model google/gemini-2.0-flash-001 --api-key sk-or-...
skipera introduction-psychology --llm --base-url http://127.0.0.1:8045/v1 --model gemini-3.8-flash-tiered
skipera introduction-psychology --llm --provider gemini   # fall back to the official Gemini SDK
```

> **Tip:** Local proxies (Antigravity/9router) typically bind only to `127.0.0.1`. Use `--base-url http://127.0.0.1:8045/v1`. If you're using a LAN IP (e.g. `192.168.x.x`) and the connection is refused, Skipera automatically retries on `127.0.0.1` as a fallback.

### Choosing a model (and checking availability)

List every model the resolved provider actually offers, then pick one. The current model is marked with `*`:

```bash
skipera --list-models --provider gemini
skipera --list-models --base-url http://127.0.0.1:8045/v1
skipera <slug> --llm --provider gemini --model gemini-2.5-flash
```

On a real run Skipera checks the chosen model against the provider's model list and warns if it is not available.

## Summary report

Every run writes a per‑item summary so you can see exactly what was completed, skipped or still needs manual action:

```bash
skipera introduction-psychology --llm --summary-dir ./reports
```

Files land in `--summary-dir`, or by default in `reports/<slug>/`. This produces:
- `skipera_summary_<slug>.json` / `.md` — full per‑item table.
- `skipera_resume_<slug>.md` — an actionable **resume checklist**: which items still need work vs. which are done.
- `skipera_manual_items.json` — raw payloads of items that need manual work.

The console output ends with two explicit sections:
- **RESUME — needs work**: items still pending (with a direct URL each), including graded (`MANUAL*`) and optional (`MANUAL`) ones.
- **SKIP — already done**: items already completed (skipped videos, readings, passed quizzes).

So the summary doubles as your to‑do list: work down `RESUME`, and the rest can be skipped on the next run.

## Course knowledge base (transcripts + readings)

Every run also saves each video transcript and reading into a single Markdown file named after the course:

```bash
skipera introduction-psychology
# -> knowledge/introduction-psychology.md (one section per menu item)
```

When quizzes are solved with `--llm`, the collected texts are injected into the LLM prompt as the knowledge base (most recent files first, capped by `--max-context-chars`, default 40000), so answers are grounded in the actual course material. Disable with `--no-knowledge`, re‑fetch with `--refresh-knowledge`, change subtitle language with `--knowledge-lang`.

## Debugging unsupported items

Some activities (interactive dialog / role‑play widgets, programming labs) are not auto‑solvable yet. Inspect the course read‑only without changing anything on your account:

```bash
skipera introduction-psychology --diagnose
```

This prints the type distribution, flags dialog/role‑play candidates, and writes `skipera_all_items.json` with every item's raw `contentSummary` and `customDisplayTypenameOverride`. Add `--dump-items` to a normal run to dump the same payloads.

Currently, only the single‑choice and multiple‑choice objective questions are supported in this mode. Note that you might not always achieve passing marks due to the LLM hallucinating sometimes.

### Grade target: 100% with view‑feedback retry

After each submission Skipera reads the grade (`highestScore/maxScore`) and attempts left from the same data behind the `view-feedback` page — no browser needed. Anything below 100% is retried while attempts remain, and the next attempt is told exactly which questions were wrong (per‑question `score/maxScore` from `feedback.parts`) plus your previous answers, so the LLM can fix them. Quizzes already at 100% are skipped (`ALREADY_DONE`) without touching your attempts. Fully‑watched videos are skipped the same way (server `viewedUpTo` check).

## Browser‑assisted capture (Playwright) for Role Play / Dialogue / discussion

These items are rendered by a JS SPA and talk to endpoints that only exist for logged‑in sessions, so they cannot be driven by plain HTTP. Instead of blindly guessing DOM selectors, Skipera ships a **local Playwright recorder**: it opens a real browser (persistent profile), you do the item **once** by hand, and it records every `/api` + GraphQL request. From that capture the exact endpoint can be turned into a stable HTTP client (no browser needed afterwards).

```bash
# 1. one‑time install (skipped if Google Chrome is already installed)
pip install playwright

# 2. record a Role Play / Dialogue / discussion item
skipera --capture "https://www.coursera.org/learn/<slug>/coach/<itemId>/<slug>" --capture-out capture.json
#    (login in the opened browser if asked, finish the item, then press Enter)
```

`capture.json` lists the `method`, `url` and `post_data` of every relevant request.
Send it over and the endpoints get wired into Skipera as direct API calls.

## License

MIT License — Copyright (c) 2026 derispewss. See the [LICENSE](LICENSE) file for the full text.
