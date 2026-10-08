# Skipera V2
<img width="96" height="96" alt="image" src="https://github.com/user-attachments/assets/8cf9428c-ef58-45b2-8fff-184ae353a890" />

Module to facilitate skipping Coursera (https://www.coursera.org/) videos and assessments.

> **V2 highlights:** dynamic LLM providers (Gemini, OpenRouter, local OpenAI-compatible
> routers), model listing + availability checks, per-item completion summary, and
> read-only `--diagnose` for unsupported item types (Role Play / Dialogue / etc.).

## Why?
Skipera assists in automatically skip irrelevant MOOC courses which are made mandatory by universities.
Many of such courses are allotted directly by the university as credit fillers and are not in the interest of the student. The progress of the completion of these courses is tracked by the university and credits are allotted.

## How?
Skipera makes use of the Coursera web API and completes the videos + reading materials.
Graded assessments are completed with the assistance of an LLM API.

## Installation

### Conda (recommended)

```bash
conda env create -f environment.yml
conda activate skipera
playwright install chromium   # only needed for the --capture (Role Play/Dialogue) mode
```

### pip

```bash
pip install -e .
```

Or install the published package:

```bash
pip install skipera
```

The project uses a **flat layout** — `main.py`, `config.py`, `report.py` and the
`assessment/`, `llm/`, `watcher/` packages live directly in the repository root.

## Configuration

On first run, skipera creates a config file at `~/.skipera/config.json`.

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

To find your cookies, use the **Application → Cookies → `https://www.coursera.org`** steps described in the tutorial below (step 2).

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

> **Research note:** "Role Play" and "Dialogue" are not their own types — both are
> `typeName: "coach"` (Coursera Coach) with a `customDisplayTypenameOverride` of
> `Role Play` / `Dialogue`. They are AI chat activities served by Coursera's coach
> service (route `/learn/<slug>/coach/<itemId>/<slug>`) behind feature flags
> (`isGradedRolePlaysEnabled`, `enableRolePlayVoiceGA`), and are not reachable via
> the public REST endpoints skipera uses, so they stay manual.

> **Can the manual items be automated?** Mostly no, and usually unnecessary:
> - `discussionPrompt` and `coach` (Role Play / Dialogue) are **not graded** in
>   typical courses — completing them is optional, so automating them rarely matters.
> - `gradedProgramming` **is graded** (e.g. ~70% of the HPC course), but it needs
>   running code inside an external lab workspace (Jupyter / RStudio / VS Code / Linux),
>   so it cannot be automated generically.
> - `peerGraded` / `peerReview` and `gradedLti` similarly require human/lab work.
>
> Skipera now distinguishes manual items: `MANUAL*` = counts toward your grade,
> `MANUAL` = optional (not graded). It derives this from the course's
> `passableLessonElements` weights.

## LLM Support

If you wish to solve graded assignments automatically, add your Perplexity or Gemini API key to the config file and use the `--llm` flag:

```bash
skipera introduction-psychology --llm
```

### Any OpenAI-compatible endpoint (dynamic providers)

Skipera can talk to any OpenAI-compatible `/chat/completions` endpoint: OpenRouter, a local router (9Router, LiteLLM, one-api), Ollama, LM Studio, vLLM, etc. Configure it once in `.env` or `~/.skipera/config.json`:

```bash
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

### Choosing a model (and checking availability)

List every model the resolved provider actually offers, then pick one. The current
model is marked with `*`:

```bash
skipera --list-models --provider gemini
skipera --list-models --base-url http://127.0.0.1:8045/v1
skipera <slug> --llm --provider gemini --model gemini-2.5-flash
```

On a real run skipera checks the chosen model against the provider's model list and
warns if it is not available.

Note that an average 10 question assignment consumes ~5000 input tokens.

## Summary report

Every run writes a per-item summary so you can see exactly what was completed, skipped or still needs manual action:

```bash
skipera introduction-psychology --llm --summary-dir ./reports
```

This produces `skipera_summary_<slug>.json` and `skipera_summary_<slug>.md`, and prints the same table to the console. Items that need manual work (labs, peer reviews, interactive widgets, etc.) are collected in `skipera_manual_items.json`.

## Debugging unsupported items

Some activities (interactive dialog / role-play widgets, programming labs) are not auto-solvable yet. Inspect the course read-only without changing anything on your account:

```bash
skipera introduction-psychology --diagnose
```

This prints the type distribution, flags dialog/role-play candidates, and writes `skipera_all_items.json` with every item's raw `contentSummary` and `customDisplayTypenameOverride`. Add `--dump-items` to a normal run to dump the same payloads.

Currently, only the single-choice and multiple-choice objective questions are supported in this mode. Note that you might
not always achieve passing marks due to the LLM hallucinating sometimes.

## Browser-assisted capture (Playwright) for Role Play / Dialogue / discussion

These items are rendered by a JS SPA and talk to endpoints that only exist for a
logged-in session, so they cannot be driven by plain HTTP. Instead of blindly
guessing DOM selectors, Skipera ships a **local Playwright recorder**: it opens a
real browser (persistent profile), you do the item **once** by hand, and it records
every `/api` + GraphQL request. From that capture the exact endpoint can be turned
into a stable HTTP client (no browser needed afterwards).

```bash
# 1. one-time install
pip install playwright && playwright install chromium

# 2. record a Role Play / Dialogue / discussion item
skipera --capture "https://www.coursera.org/learn/<slug>/coach/<itemId>/<slug>" --capture-out capture.json
#    (login in the opened browser if asked, finish the item, then press Enter)
```

`capture.json` lists the `method`, `url` and `post_data` of every relevant request.
Send it over and the endpoints get wired into Skipera as direct API calls.

---

# Dokumentasi (Bahasa Indonesia)

Skipera V2 adalah sebuah *tool* (modul) yang dirancang untuk memfasilitasi pengguna dalam melewati (skip) materi video dan otomatis menyelesaikan penilaian (*assessment*) pada platform Coursera.

## Mengapa Menggunakan Skipera?
Banyak program studi atau universitas yang mewajibkan mahasiswanya untuk menyelesaikan kursus MOOC (*Massive Open Online Course*) tertentu sebagai syarat kredit (SKS). Terkadang kursus ini dianggap kurang relevan dengan minat atau fokus mahasiswa tersebut. Skipera membantu mempercepat proses penyelesaian kursus sehingga progres tetap tercatat oleh universitas.

## Cara Kerja
1. **Penyelesaian Materi Otomatis:** Skipera memanfaatkan *Coursera Web API* untuk menandai video dan materi bahan bacaan sebagai selesai diakses.
2. **Bantuan AI (LLM) untuk Ujian/Kuis:** Penilaian atau format *graded assessments* diselesaikan dengan mengandalkan integrasi *Large Language Model* (LLM) API, seperti Gemini, OpenRouter, atau router lokal (9Router, Ollama, LM Studio, dll).

## Tutorial Instalasi dan Penggunaan

### 1. Menyiapkan Lingkungan Python (Conda Environment)
Buka terminal dan pastikan Anda telah berada di dalam direktori proyek ini.

```bash
# 1. Membuat conda environment baru bernama 'skipera' (sekaligus install semua dependensi)
conda env create -f environment.yml

# 2. Mengaktifkan conda environment
conda activate skipera
```

### 2. Konfigurasi Autentikasi: Mendapatkan Cookie (CAUTH)
Agar program ini dapat berinteraksi dengan akun Coursera Anda (menandai kemajuan atas nama Anda), Anda harus memiliki akses cookie otentikasi.

1. Buka *browser* (seperti Google Chrome atau Microsoft Edge).
2. Akses situs web [Coursera](https://www.coursera.org/) dan pastikan Anda sudah **Login**.
3. Lakukan **Inspect Element** (klik kanan sembarang pada halaman > **Inspect**, atau tekan tombol **F12**).
4. Di panel Inspect Element, navigasi ke tab **Application**.
5. Pada menu navigasi di sebelah kiri, rentangkan menu **Cookies**, kemudian pilih **`https://www.coursera.org`**.
6. Cari *Name* **`CAUTH`** dan salin *Value*-nya, lalu isikan pada file `.env` atau `~/.skipera/config.json`.

> **Tips Otomatis:** Pada beberapa kasus, Skipera dapat mendeteksi Cookie secara otomatis pada *browser* yang sedang login akun Coursera. Cukup tutup *browser* Chrome (jika di Windows) dan jalankan *script*.

### 3. Menjalankan Script Proyek
Setelah *environment* aktif dan Anda telah login ke Coursera:

```bash
# Format perintah
skipera <judul_course_atau_slug>

# Contoh
skipera introduction-psychology
```

### 4. Mode Lanjutan dengan Dukungan LLM
Untuk menyelesaikan tugas/kuis secara otomatis, tambahkan flag `--llm`:

```bash
skipera <slug> --llm
```

#### 4a. Menggunakan Model Lokal / Proxy (dinamis)
Skipera mendukung **semua endpoint yang kompatibel dengan OpenAI** (`/chat/completions`): 9router, OpenRouter, Ollama, LM Studio, vLLM, LiteLLM, one-api, dll. Konfigurasi cukup sekali di file `.env`:

```bash
LLM_PROVIDER=openai            # gemini | openai | openrouter | 9router | local | perplexity
LLM_BASE_URL=http://127.0.0.1:8045/v1
LLM_API_KEY=                   # biasanya kosong untuk router lokal
LLM_MODEL=gemini-3.8-flash-tiered
```

Atau ganti tanpa mengedit file, langsung dari terminal:

```bash
# Pakai proxy lokal
skipera <slug> --llm --base-url http://127.0.0.1:8045/v1 --model gemini-3.8-flash-tiered

# Pakai OpenRouter
skipera <slug> --llm --provider openrouter --model google/gemini-2.0-flash-001 --api-key sk-or-...

# Balik ke Gemini resmi
skipera <slug> --llm --provider gemini
```

> **Tips:** proxy lokal (Antigravity/9router) umumnya hanya *bind* ke `127.0.0.1`. Gunakan `--base-url http://127.0.0.1:8045/v1`. Kalau tetap memakai IP LAN (mis. `192.168.x.x`) dan koneksinya ditolak, Skipera otomatis mencoba `127.0.0.1` sebagai fallback.

#### 4b. Ringkasan (Summary) Hasil Course
Setiap kali dijalankan, Skipera membuat ringkasan per-item: mana video yang di-*skip*, materi yang dibaca, kuis yang lulus, dan item yang butuh tindakan manual.

```bash
skipera <slug> --llm --summary-dir ./reports
```

Menghasilkan `skipera_summary_<slug>.json` dan `skipera_summary_<slug>.md`. Item yang butuh aksi manual dikumpulkan di `skipera_manual_items.json`.

#### 4c. Memilih Model & Cek Ketersediaan
Gunakan `--list-models` untuk melihat model yang benar-benar tersedia (model yang sedang dipakai ditandai `*`):

```bash
skipera --list-models --provider gemini
skipera --list-models --base-url http://127.0.0.1:8045/v1
skipera <slug> --llm --provider gemini --model gemini-2.5-flash
```

Saat menjalankan `--llm`, Skipera memvalidasi model yang dipilih dan memberi peringatan bila model tersebut tidak tersedia.

#### 4d. Tipe Item Coursera & Statusnya

| `typeName` | Tampilan | Perlakuan |
| --- | --- | --- |
| `lecture` | video | di-skip bila boleh, atau ditonton |
| `supplement` | bacaan | ditandai selesai |
| `ungradedAssignment` | kuis latihan | dijawab dengan `--llm` |
| `staffGraded` | kuis bernilai | dijawab dengan `--llm` |
| `ungradedWidget` / `ungradedLti` | widget / lab eksternal | dicoba auto-complete |
| `discussionPrompt` | diskusi | **manual** — posting di forum |
| `coach` | **Role Play / Dialogue** | **manual** — percakapan AI Coursera Coach |
| `gradedProgramming` | tugas pemrograman | **manual** — jalankan kode di lab |
| `gradedLti` | tool eksternal bernilai | **manual** |
| `peerGraded` / `peerReview` / `phasedPeer` | penilaian sejawat | **manual** |

> **Hasil riset:** "Role Play" dan "Dialogue" bukan tipe tersendiri — keduanya bertipe `coach` (fitur Coursera Coach) dengan `customDisplayTypenameOverride` `Role Play` / `Dialogue`. Aktivitas ini berupa percakapan AI di rute `/learn/<slug>/coach/<itemId>/<slug>`, di balik feature flag (`isGradedRolePlaysEnabled`, `enableRolePlayVoiceGA`), dan tidak dapat diakses lewat endpoint REST publik yang dipakai Skipera — sehingga tetap **manual**. Tugas pemrograman (`gradedProgramming`) juga manual karena harus menjalankan/menyimpan kode di environment lab.

> **Apakah item manual bisa diotomatiskan?** Umumnya tidak, dan sering tidak perlu:
> - `discussionPrompt` dan `coach` (Role Play / Dialogue) **tidak dinilai** di course umum — mengerjakannya opsional, jadi otomatisasi nyaris tak berdampak.
> - `gradedProgramming` **dinilai** (contoh ~70% nilai course HPC), tapi butuh menjalankan kode di lab eksternal (Jupyter / RStudio / VS Code / Linux) sehingga tidak bisa digeneralisasi.
> - `peerGraded` / `peerReview` dan `gradedLti` juga butuh pekerjaan manusia/lab.
>
> Skipera kini membedakan item manual: **`MANUAL*`** = ikut menghitung nilai, **`MANUAL`** = opsional (tidak dinilai), diambil dari bobot `passableLessonElements` course. Gunakan `--diagnose` untuk melihat bobot tiap item.

#### 4f. Mode Playwright (rekam API Role Play / Dialogue / diskusi)
Item coach/discussion dirender oleh SPA dan endpoint-nya hanya muncul saat sudah login, jadi tidak bisa lewat HTTP biasa. Daripada menebak selector DOM, Skipera menyediakan **perekam Playwright lokal**: membuka browser asli (profil persisten), Anda kerjakan itemnya **sekali** secara manual, dan semua request `/api` + GraphQL direkam. Dari rekaman itu, endpoint-nya bisa diubah menjadi pemanggilan API langsung yang stabil.

```bash
# install sekali
pip install playwright && playwright install chromium

# rekam satu item Role Play / Dialogue / diskusi
skipera --capture "https://www.coursera.org/learn/<slug>/coach/<itemId>/<slug>" --capture-out capture.json
#   (login di browser yang terbuka bila diminta, selesaikan itemnya, lalu tekan Enter)
```

File `capture.json` berisi `method`, `url`, dan `post_data` tiap request. Kirim isinya agar endpoint tersebut dipasang sebagai pemanggilan API langsung di Skipera.

#### 4e. Men-debug Item yang Belum Didukung
Untuk melihat tipe setiap item **tanpa mengubah apa pun** di akun Anda:

```bash
skipera <slug> --diagnose
```

Perintah ini mencetak distribusi tipe item (termasuk `raw -> override`), menandai kandidat dialog/role play, dan menulis `skipera_all_items.json`. Tambahkan `--dump-items` pada run biasa untuk menyimpan payload yang sama.

> **Catatan Mode Kuis (LLM):** AI kadang-kadang bisa memberikan halusinasi (jawaban tak akurat). Anda mungkin mengalami kondisi tidak langsung lulus karena nilai tak cukup, sehingga butuh retake atau pengecekan mandiri. Selain itu per tugas rata-rata menghabiskan ~5000 input tokens.

## Lisensi

MIT License — Copyright (c) 2026 derispewss. Lihat file [LICENSE](LICENSE) untuk teks lengkapnya.
