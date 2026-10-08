import json
from collections import Counter
from datetime import datetime
from pathlib import Path

# Plain-text status markers (no emojis) used across the summary output.
STATUS_LABELS = {
    "skipped": "SKIPPED",
    "watched": "WATCHED",
    "read": "READ",
    "passed": "PASSED",
    "failed": "FAILED",
    "no_attempts": "NO_ATTEMPTS",
    "no_llm": "NO_LLM",
    "manual": "MANUAL",
    "error": "ERROR",
    "already": "ALREADY_DONE",
}

# Human-readable one-liners for the summary footer.
STATUS_NOTES = {
    "skipped": "video dilewati karena skippable",
    "watched": "video ditonton sampai selesai",
    "read": "materi bacaan ditandai selesai",
    "passed": "assessment dijawab & lulus",
    "failed": "assessment belum lulus",
    "no_attempts": "tidak ada sisa attempt",
    "no_llm": "butuh konfigurasi LLM",
    "manual": "perlu tindakan manual (belum didukung)",
    "error": "gagal diproses",
    "already": "sudah selesai sebelumnya",
}


class CourseReport(object):
    """Collects per-item outcomes and renders a course completion summary."""

    def __init__(self, course: str, course_id: str = "", course_name: str = ""):
        self.course = course
        self.course_id = course_id
        self.course_name = course_name
        self.started_at = datetime.now()
        self.entries = []

    def add(self, name: str, item_type: str, status: str, detail: str = "") -> None:
        self.entries.append({
            "name": name,
            "type": item_type,
            "status": status,
            "detail": detail,
        })

    def counts(self) -> Counter:
        return Counter(entry["status"] for entry in self.entries)

    def render(self) -> str:
        lines = []
        lines.append("=" * 70)
        title = self.course_name or self.course
        lines.append(f"RINGKASAN COURSE: {title}")
        if self.course_id:
            lines.append(f"Course ID      : {self.course_id}")
        lines.append(f"Waktu          : {self.started_at:%Y-%m-%d %H:%M:%S}")
        lines.append("=" * 70)

        if not self.entries:
            lines.append("(tidak ada item yang diproses)")
        else:
            for entry in self.entries:
                label = STATUS_LABELS.get(entry["status"], entry["status"].upper())
                detail = f"  -- {entry['detail']}" if entry["detail"] else ""
                lines.append(
                    f"[{label:12}] {entry['name']} ({entry['type']}){detail}"
                )

        lines.append("-" * 70)
        counts = self.counts()
        if counts:
            tally = ", ".join(
                f"{STATUS_LABELS.get(status, status.upper())}={count}"
                for status, count in sorted(counts.items())
            )
            lines.append(f"Total: {len(self.entries)} item | {tally}")
        lines.append("=" * 70)
        return "\n".join(lines)

    def save(self, directory: str = "") -> dict:
        out_dir = Path(directory) if directory else Path.cwd()
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = f"skipera_summary_{self.course.replace('/', '_')}"

        payload = {
            "course": self.course,
            "course_name": self.course_name,
            "course_id": self.course_id,
            "generated_at": self.started_at.isoformat(),
            "counts": dict(self.counts()),
            "entries": self.entries,
        }

        json_path = out_dir / f"{stem}.json"
        md_path = out_dir / f"{stem}.md"
        json_path.write_text(json.dumps(payload, indent=2))
        md_path.write_text(self._render_markdown())

        return {"json": str(json_path), "markdown": str(md_path)}

    def _render_markdown(self) -> str:
        title = self.course_name or self.course
        lines = [f"# Ringkasan Course: {title}", ""]
        lines.append(f"- Course slug: `{self.course}`")
        if self.course_id:
            lines.append(f"- Course ID: `{self.course_id}`")
        lines.append(f"- Waktu: {self.started_at:%Y-%m-%d %H:%M:%S}")
        lines.append("")
        lines.append("| Status | Item | Tipe | Keterangan |")
        lines.append("| --- | --- | --- | --- |")
        for entry in self.entries:
            label = STATUS_LABELS.get(entry["status"], entry["status"].upper())
            lines.append(
                f"| {label} | {entry['name']} | {entry['type']} | {entry['detail']} |"
            )
        lines.append("")
        counts = self.counts()
        if counts:
            lines.append("## Rekap")
            for status, count in sorted(counts.items()):
                note = STATUS_NOTES.get(status, "")
                lines.append(f"- {STATUS_LABELS.get(status, status.upper())}: {count} ({note})")
        lines.append("")
        return "\n".join(lines)
