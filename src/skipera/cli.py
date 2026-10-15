import json
import os
import sys
from pathlib import Path

import click
import requests
from skipera.config import (fetch_browser_cookies, CONFIG_FILE, DEFAULT_CONFIG, BASE_URL,
                    HEADERS, COOKIES, resolve_llm_settings)
from loguru import logger
from skipera.assessment.solver import GradedSolver
from skipera.knowledge import (default_knowledge_dir, fetch_lecture_transcript,
                       fetch_supplement_markdown, has_knowledge,
                       load_knowledge_context, save_knowledge)
from skipera.llm.models import list_models as list_available_models, ensure_model_available
from skipera.report import CourseReport
from skipera.watcher.watch import Watcher


# Item types requiring a manual step because skipera cannot complete them yet.
MANUAL_ITEM_TYPES = {
    "peerGraded",
    "peerReview",
    "phasedPeer",
    "appItem",
    "gradedProgramming",
    "gradedLti",
    "staffGraded",
}

# Keywords that hint at the interactive dialog / role-play activities.
INTERACTIVE_KEYWORDS = ("dialog", "role", "conversation", "scenario", "widget", "coach")


class Skipera(object):
    def __init__(self, course: str, llm: bool, llm_settings: dict = None,
                 summary_dir: str = "", dump_items: bool = False, diagnose: bool = False,
                 knowledge_dir: str = "", no_knowledge: bool = False,
                 knowledge_lang: str = "en", max_context_chars: int = 4000,
                 input_token_budget: int = 5000,
                 refresh_knowledge: bool = False, short_prompt: bool = False):
        self.user_id = None
        self.course_id = None
        self.base_url = BASE_URL
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.session.cookies.update(COOKIES)
        self.course = course
        self.llm = llm
        self.llm_settings = llm_settings or resolve_llm_settings()
        self.summary_dir = summary_dir
        self.dump_items = dump_items
        self.diagnose = diagnose
        self.knowledge_dir = Path(knowledge_dir) if knowledge_dir else default_knowledge_dir()
        self.no_knowledge = no_knowledge
        self.knowledge_lang = knowledge_lang or "en"
        self.max_context_chars = max_context_chars
        self.input_token_budget = input_token_budget
        self.refresh_knowledge = refresh_knowledge
        self.short_prompt = short_prompt
        self._knowledge_order = 0
        self._completion_probe_state = {}
        self.report = CourseReport(course)
        self.manual_items = []
        self.all_items = []
        self.graded_weights = {}
        self.progress_map = {}
        if not self.get_userid():
            self.refresh_cookies()
            if not self.get_userid():
                logger.error("Cookies are invalid. Log into Coursera in your browser, close it, and retry.")
                raise SystemExit

    def refresh_cookies(self):
        logger.warning("Session expired — re-fetching cookies from browser...")
        cookies = fetch_browser_cookies()
        if not cookies:
            return
        self.session.cookies.clear()
        self.session.cookies.update(cookies)
        cfg = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else DEFAULT_CONFIG.copy()
        cfg["cookies"] = cookies
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

    def get_userid(self) -> bool:
        r = self.session.get(self.base_url + "adminUserPermissions.v1?q=my").json()
        try:
            self.user_id = r["elements"][0]["id"]
            logger.info("User ID: " + self.user_id)
        except KeyError:
            if r.get("errorCode"):
                logger.error("Error Encountered: " + r["errorCode"])
            return False
        return True

    def get_course(self) -> None:
        r = self.session.get(self.base_url + f"onDemandCourseMaterials.v2/", params={
            "q": "slug",
            "slug": self.course,
            "includes": "modules,lessons,passableItemGroups,passableItemGroupChoices,passableLessonElements,items,"
                        "tracks,gradePolicy,gradingParameters,embeddedContentMapping",
            "fields": "moduleIds,onDemandCourseMaterialModules.v1(name,slug,description,timeCommitment,lessonIds,"
                      "optional,learningObjectives),onDemandCourseMaterialLessons.v1(name,slug,timeCommitment,"
                      "elementIds,optional,trackId),onDemandCourseMaterialPassableItemGroups.v1(requiredPassedCount,"
                      "passableItemGroupChoiceIds,trackId),onDemandCourseMaterialPassableItemGroupChoices.v1(name,"
                      "description,itemIds),onDemandCourseMaterialPassableLessonElements.v1(gradingWeight,"
                      "isRequiredForPassing),onDemandCourseMaterialItems.v2(name,originalName,slug,timeCommitment,"
                      "contentSummary,isLocked,lockableByItem,itemLockedReasonCode,trackId,lockedStatus,itemLockSummary,"
                      "customDisplayTypenameOverride),onDemandCourseMaterialTracks.v1(passablesCount),"
                      "onDemandGradingParameters.v1(gradedAssignmentGroups),"
                      "contentAtomRelations.v1(embeddedContentSourceCourseId,subContainerId)",
            "showLockedItems": True
        })

        if r.status_code != 200:
            logger.error("Please check if you are enrolled in the course!")
            raise SystemExit

        r = r.json()

        self.course_id = r["elements"][0]["id"]
        course_name = r["elements"][0].get("name", "")
        self.report.course_id = self.course_id
        self.report.course_name = course_name
        self._index_graded_items(r)
        self._load_progress_map()

        logger.info("Course ID: " + self.course_id)
        logger.info("Number of Modules: " + str(len(r["linked"]["onDemandCourseMaterialModules.v1"])))
        logger.debug("Processing items..")

        if self.diagnose:
            self._diagnose_items(r["linked"]["onDemandCourseMaterialItems.v2"])
            return

        for item in r["linked"]["onDemandCourseMaterialItems.v2"]:
            self.all_items.append(item)
            raw_type = item["contentSummary"]["typeName"]
            display_type = self._type_name(item)  # honors customDisplayTypenameOverride

            # Server checklist wins: Completed items are skipped entirely.
            # (Quizzes keep their grade flow so <100% scores still get retried.)
            if raw_type not in {"ungradedAssignment", "staffGraded"} and self._is_completed(item["id"]):
                logger.info(f"Sudah checklist di Coursera: '{item['name']}' — dilewati.")
                self.report.add(item["name"], display_type, "already", "sudah Completed di server",
                                url=self._item_url(item))
                continue

            if raw_type == "lecture":
                logger.info(item["name"])
                status = self.watch_item(item, self.get_video_metadata(item["id"]))
                self.report.add(item["name"], raw_type, status, url=self._item_url(item))
                self._save_lecture_knowledge(item)
            elif raw_type == "supplement":
                status = self.read_item(item["id"], raw_type, item["name"])
                self.report.add(item["name"], raw_type, status, url=self._item_url(item))
                self._save_supplement_knowledge(item)
            elif raw_type == "ungradedAssignment":
                self._handle_assessment(item, display_type, "practice/ungraded assessment")
            elif raw_type == "staffGraded":
                self._handle_assessment(item, display_type, "graded assessment")
            elif raw_type in {"ungradedWidget", "ungradedLti"}:
                if self._complete_widget(item["id"]):
                    logger.info(f"Marked as complete (widget progress): {item['name']} ({raw_type})")
                    self.report.add(item["name"], display_type, "passed", "auto-complete via endpoint",
                                    url=self._item_url(item))
                elif self._try_reverse_completion(item["id"], raw_type, item["name"]):
                    logger.info(f"Marked as complete via reversed endpoint: {item['name']} ({raw_type})")
                    self.report.add(item["name"], display_type, "passed", "auto-complete via endpoint",
                                    url=self._item_url(item))
                else:
                    self._record_manual(item, display_type, reason="widget eksternal")
            elif raw_type == "coach":
                self._handle_coach(item, display_type)
            elif raw_type in {"discussionPrompt", "gradedDiscussionPrompt"}:
                logger.info(f"Discussion prompt: '{item['name']}' (perlu posting di forum).")
                self._record_manual(item, display_type, reason="posting di forum diskusi")
            elif raw_type in MANUAL_ITEM_TYPES:
                self._record_manual(item, display_type, reason=self._manual_reason(raw_type))
            else:
                if self._looks_interactive(item, raw_type):
                    logger.info(
                        f"Interactive item detected: '{item['name']}' ({display_type}); "
                        "attempting auto-complete probe..."
                    )
                    if self._try_reverse_completion(item["id"], raw_type, item["name"]):
                        logger.info(f"Marked as complete: {item['name']} ({display_type})")
                        self.report.add(item["name"], display_type, "passed", "auto-complete via endpoint",
                                        url=self._item_url(item))
                    else:
                        self._record_manual(item, display_type, reason="interaktif/coach")
                else:
                    logger.debug(f"Skipping unsupported item type: {raw_type} ({item['name']})")
                    self.report.add(item["name"], display_type, "manual", "tipe tidak didukung",
                                    url=self._item_url(item))

        self._finish()

    def _handle_coach(self, item: dict, display_type: str) -> None:
        """Coursera Coach activities (Role Play / Dialogue) need live AI interaction."""
        label = item.get("customDisplayTypenameOverride") or "Coach"
        logger.warning(
            f"Coursera Coach: '{item['name']}' ({label}). Aktivitas Role Play/Dialogue "
            "berbasis AI; belum bisa otomatis."
        )
        self._record_manual(
            item, f"coach ({label})",
            reason="Coursera Coach (Role Play/Dialogue) — perlu interaksi AI")

    def _knowledge_enabled(self) -> bool:
        return not self.no_knowledge

    def _next_knowledge_order(self) -> int:
        self._knowledge_order += 1
        return self._knowledge_order

    def _save_lecture_knowledge(self, item: dict) -> None:
        if not self._knowledge_enabled():
            return
        if not self.refresh_knowledge and has_knowledge(self.course, self.knowledge_dir, item["id"]):
            return
        try:
            text = fetch_lecture_transcript(
                self.session, self.base_url, self.course_id, item["id"],
                lang=self.knowledge_lang)
            if not text:
                logger.debug(f"Tidak ada transkrip untuk '{item['name']}'")
                return
            save_knowledge(self.course, self.knowledge_dir,
                           self._next_knowledge_order(), item,
                           "lecture", text, url=self._item_url(item),
                           overwrite=self.refresh_knowledge)
        except Exception as exc:
            logger.debug(f"Gagal menyimpan knowledge '{item['name']}': {exc}")

    def _save_supplement_knowledge(self, item: dict) -> None:
        if not self._knowledge_enabled():
            return
        if not self.refresh_knowledge and has_knowledge(self.course, self.knowledge_dir, item["id"]):
            return
        try:
            text = fetch_supplement_markdown(
                self.session, self.base_url, self.course_id, item["id"])
            if not text:
                logger.debug(f"Tidak ada konten bacaan untuk '{item['name']}'")
                return
            save_knowledge(self.course, self.knowledge_dir,
                           self._next_knowledge_order(), item,
                           "supplement", text, url=self._item_url(item),
                           overwrite=self.refresh_knowledge)
        except Exception as exc:
            logger.debug(f"Gagal menyimpan knowledge '{item['name']}': {exc}")

    def _knowledge_context(self, questions=None) -> str:
        if not self._knowledge_enabled() or not self.llm:
            return ""
        try:
            return load_knowledge_context(self.knowledge_dir, self.course,
                                          self.max_context_chars, query=questions)
        except Exception as exc:
            logger.debug(f"Gagal memuat knowledge context: {exc}")
            return ""

    @staticmethod
    def _manual_reason(raw_type: str) -> str:
        return {
            "gradedProgramming": "programming assignment / lab (jalankan kode)",
            "programming": "programming / lab (jalankan kode)",
            "notebook": "notebook / lab (jalankan kode)",
            "gradedLti": "lab eksternal (LTI) – perlu dijalankan manual",
            "peerGraded": "peer review – perlu dinilai manual",
            "peerReview": "peer review – perlu dinilai manual",
            "phasedPeer": "peer review bertahap",
            "gradedPeer": "peer review – perlu dinilai manual",
            "teammateReview": "review rekan tim",
            "appItem": "aplikasi eksternal",
            "exam": "ujian – perlu dikerjakan manual",
            "wiseFlow": "ujian terproktor (WiseFlow)",
        }.get(raw_type, "tidak auto-solvable")

    def _index_graded_items(self, materials: dict) -> None:
        """Map item id -> point weight for items that count toward the course grade."""
        self.graded_weights = {}
        linked = materials.get("linked", {}) or {}
        for element in linked.get("onDemandCourseMaterialPassableLessonElements.v1", []) or []:
            element_id = element.get("id", "")
            if element_id.startswith("item~"):
                item_id = element_id.split("~", 1)[1]
                weight = element.get("gradingWeight") or 0
                if weight:
                    self.graded_weights[item_id] = weight
        if self.graded_weights:
            logger.debug(f"Indexed {len(self.graded_weights)} graded items.")

    def _load_progress_map(self) -> None:
        """Fetch the server checklist: item_id -> progressState (Completed/...)."""
        self.progress_map = {}
        try:
            r = self.session.get(
                self.base_url + f"onDemandCoursesProgress.v1/{self.user_id}~{self.course_id}",
                timeout=30)
            if r.status_code != 200:
                return
            items = (r.json().get("elements") or [{}])[0].get("items", {})
            self.progress_map = {
                item_id: (entry or {}).get("progressState", "")
                for item_id, entry in items.items()
            }
            done = sum(1 for v in self.progress_map.values() if v == "Completed")
            logger.info(f"Checklist server: {done}/{len(self.progress_map)} item sudah Completed.")
        except Exception as exc:
            logger.debug(f"Gagal memuat checklist server: {exc}")

    def _is_completed(self, item_id: str) -> bool:
        return self.progress_map.get(item_id) == "Completed"

    def _diagnose_items(self, items: list) -> None:
        """Read-only: list every item and its type so unsupported ones can be studied."""
        by_type = {}
        rows = []
        for item in items:
            raw_type = item["contentSummary"]["typeName"]
            override = item.get("customDisplayTypenameOverride")
            display = f"{raw_type} -> {override}" if override else raw_type
            locked = item.get("isLocked")
            rows.append((display, item["id"], item["name"], bool(override), bool(locked)))
            by_type.setdefault(display, 0)
            by_type[display] += 1

        logger.info("Type distribution (raw[ -> display override]):")
        for type_name, count in sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0])):
            logger.info(f"  {count:>3}  {type_name}")

        logger.info("Items (type | graded pts | locked | name):")
        for type_name, item_id, name, has_override, locked in rows:
            lock = "LOCKED " if locked else ""
            pts = self.graded_weights.get(item_id)
            ptxt = f"{pts} pts | " if pts else ""
            logger.info(f"  {lock}{type_name} | {ptxt}{name}  ({item_id})")

        if self.graded_weights:
            total = sum(self.graded_weights.values())
            graded_types = {}
            for item in items:
                if item["id"] in self.graded_weights:
                    t = item["contentSummary"]["typeName"]
                    graded_types[t] = graded_types.get(t, 0) + 1
            logger.info(f"Graded items: {len(self.graded_weights)} ({total} pts) -> {graded_types}")
            logger.info("  (item yang dinilai inilah yang wajib dikerjakan untuk lulus)")

        interactive = [row for row in rows if self._looks_interactive(
            {"name": row[2], "slug": ""}, row[0])]
        if interactive:
            logger.info("Interactive/dialog/role-play candidates:")
            for type_name, item_id, name, _, _ in interactive:
                logger.info(f"  - {name} ({type_name})")

        self.all_items = items
        path = self._dump_json("skipera_all_items.json", items)
        logger.info(f"Full item payloads written to {path}")

    def _type_name(self, item: dict) -> str:
        override = item.get("customDisplayTypenameOverride")
        return override or item["contentSummary"]["typeName"]

    def _item_url(self, item: dict) -> str:
        """Canonical Coursera URL: /learn/<slug>/<route>/<itemId>/<itemSlug>."""
        raw_type = item["contentSummary"]["typeName"]
        route = "coach" if raw_type == "coach" else raw_type
        slug = item.get("slug", "")
        return f"https://www.coursera.org/learn/{self.course}/{route}/{item['id']}/{slug}"

    def _handle_assessment(self, item: dict, type_name: str, label: str) -> None:
        if not self.llm:
            logger.info(f"Skipping {label} (run with --llm to attempt it).")
            weight = self.graded_weights.get(item["id"], 0)
            detail = "jalankan dengan --llm" + (f" ({weight} pts)" if weight else "")
            self.report.add(item["name"], type_name, "no_llm", detail, url=self._item_url(item))
            return

        logger.info(f"Attempting to solve {label}..")
        context = self._knowledge_context()
        if self.short_prompt:
            os.environ["SKIPERA_SHORT_PROMPT"] = "1"
        else:
            os.environ.pop("SKIPERA_SHORT_PROMPT", None)
        solver = GradedSolver(self.session, self.course_id, item["id"],
                              self.llm_settings, knowledge_context=context,
                              knowledge_dir=self.knowledge_dir,
                              course_slug=self.course,
                              max_chars=self.max_context_chars,
                              input_token_budget=self.input_token_budget)
        status = solver.solve()
        weight = self.graded_weights.get(item["id"], 0)
        detail = solver.last_grade
        if solver.last_error and status == "error":
            detail = (detail + " " if detail else "") + solver.last_error
        if weight and status not in {"passed"}:
            detail = (detail + " " if detail else "") + f"({weight} pts)"
        if status == "passed" and solver.preexisting_perfect:
            status = "already"
            logger.info(f"Sudah 100% sebelumnya: '{item['name']}' ({detail}) — dilewati.")
        self.report.add(item["name"], type_name, status, detail, url=self._item_url(item))
        if status in {"failed", "no_llm", "error", "no_attempts"}:
            self.manual_items.append(item)

    def _record_manual(self, item: dict, type_name: str, reason: str = "tidak auto-solvable") -> None:
        weight = self.graded_weights.get(item["id"], 0)
        if weight:
            status = "manual_graded"
            detail = f"{reason} — DINILAI {weight} pts"
        else:
            status = "manual"
            detail = f"{reason} — opsional/tidak dinilai"
        logger.warning(f"Manual action required for '{item['name']}' ({type_name}). {detail}.")
        self.report.add(item["name"], type_name, status, detail, url=self._item_url(item))
        self.manual_items.append(item)

    def _looks_interactive(self, item: dict, type_name: str) -> bool:
        haystack = f"{type_name} {item.get('name', '')} {item.get('slug', '')}".lower()
        return any(keyword in haystack for keyword in INTERACTIVE_KEYWORDS)

    def _output_dir(self) -> Path:
        if self.summary_dir:
            return Path(self.summary_dir)
        safe = (self.course or "course").replace("/", "_")
        return Path.cwd() / "reports" / safe

    def _finish(self) -> None:
        summary = self.report.render()
        print("\n" + summary + "\n")

        paths = self.report.save(str(self._output_dir()))
        logger.info(f"Summary (JSON)    : {paths['json']}")
        logger.info(f"Summary (Markdown): {paths['markdown']}")
        logger.info(f"Resume checklist  : {paths['resume']}")
        if self.report.resume_entries():
            logger.warning(
                f"{len(self.report.resume_entries())} item perlu di-resume "
                f"(lihat {paths['resume']})."
            )

        if self.dump_items:
            self._dump_json("skipera_all_items.json", self.all_items)

        if self.manual_items:
            path = self._dump_json("skipera_manual_items.json", self.manual_items)
            logger.warning(
                f"{len(self.manual_items)} item butuh tindakan manual. "
                f"Detail payload ditulis ke {path}"
            )

    def _dump_json(self, filename: str, payload) -> str:
        out_dir = self._output_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / filename
        path.write_text(json.dumps(payload, indent=2, default=str))
        return str(path)

    def get_video_metadata(self, item_id: str) -> dict:
        r = self.session.get(self.base_url + f"onDemandLectureVideos.v1/{self.course_id}~{item_id}", params={
            "includes": "video",
            "fields": "disableSkippingForward,startMs,endMs"
        }).json()

        return {"can_skip": not r["elements"][0]["disableSkippingForward"],
                "tracking_id": r["linked"]["onDemandVideos.v1"][0]["id"]}

    def watch_item(self, item: dict, metadata: dict) -> str:
        if not metadata["can_skip"] and self._already_watched(item, metadata):
            logger.info(f"Sudah ditonton sebelumnya: '{item['name']}' — dilewati.")
            return "already"
        watcher = Watcher(self.session, item, metadata, self.user_id, self.course, self.course_id)
        return watcher.watch_item()

    def _already_watched(self, item: dict, metadata: dict) -> bool:
        """True when the server shows this video as fully viewed."""
        try:
            progress_id = f"{self.user_id}~{self.course_id}~{metadata['tracking_id']}"
            r = self.session.get(self.base_url + f"onDemandVideoProgresses.v1/{progress_id}",
                                 timeout=30)
            if r.status_code != 200:
                return False
            viewed = (r.json().get("elements") or [{}])[0].get("viewedUpTo", 0) or 0
            return viewed >= (item.get("timeCommitment") or 0) > 0
        except (ValueError, KeyError, TypeError, AttributeError):
            return False
        except Exception as exc:
            logger.debug(f"Gagal cek progres video '{item.get('name', '')}': {exc}")
            return False

    def read_item(self, item_id: str, item_type: str = "supplement", item_name: str = "") -> str:
        r = self.session.post(self.base_url + "onDemandSupplementCompletions.v1", json={
            "courseId": self.course_id,
            "itemId": item_id,
            "userId": int(self.user_id)
        })
        if 200 <= r.status_code < 300:
            try:
                payload = r.json()
                if not payload.get("errorCode"):
                    return "read"
            except ValueError:
                # Non-JSON but successful status; assume completion was accepted.
                return "read"

        label = f" ({item_name})" if item_name else ""
        logger.debug(
            f"Couldn't mark item as complete{label}; type={item_type}, "
            f"status={r.status_code}, body={r.text[:160]}"
        )
        return "error"

    def _is_completion_success(self, response: requests.Response) -> bool:
        if not (200 <= response.status_code < 300):
            return False

        content_type = response.headers.get("Content-Type", "").lower()
        body = response.text.strip()

        # Coursera answers unknown routes with HTTP 200 + an HTML "API Route
        # Does Not Exist" page. Never mistake that for a completion.
        if "text/html" in content_type or body.startswith("<"):
            return False

        if not body:
            return True  # 200/204 with empty body is a valid ack

        try:
            payload = response.json()
        except ValueError:
            return True

        if payload.get("errorCode"):
            return False

        message = str(payload.get("message", "")).lower()
        if "wrong content type" in message:
            return False

        return True

    def _completion_candidates(self, item_id: str, item_type: str) -> list:
        base_payload = {
            "courseId": self.course_id,
            "itemId": item_id,
            "userId": int(self.user_id),
        }

        progress_id = f"{self.user_id}~{self.course_id}~{item_id}"

        if item_type == "ungradedWidget":
            return [
                ("POST", "onDemandWidgetCompletions.v1", base_payload),
                ("POST", "onDemandUngradedWidgetCompletions.v1", base_payload),
                ("POST", "onDemandWidgetProgresses.v1", {
                    **base_payload,
                    "widgetProgressId": progress_id,
                }),
                ("PUT", f"onDemandWidgetProgresses.v1/{progress_id}", {
                    **base_payload,
                    "id": progress_id,
                    "widgetProgressId": progress_id,
                }),
            ]

        if item_type == "ungradedLti":
            return [
                ("POST", "onDemandLtiCompletions.v1", base_payload),
                ("POST", "onDemandUngradedLtiCompletions.v1", base_payload),
                ("POST", "onDemandExternalToolCompletions.v1", base_payload),
            ]

        # Generic fallbacks for interactive items whose type we do not know yet
        # (e.g. dialog / role-play widgets). A wrong endpoint simply 404s.
        return [
            ("POST", "onDemandWidgetCompletions.v1", base_payload),
            ("POST", "onDemandItemCompletions.v1", base_payload),
            ("POST", "onDemandUngradedItemCompletions.v1", base_payload),
        ]

    def _widget_session_id(self, item_id: str) -> str:
        """Fetch the widget session id required for progress updates."""
        wid = f"{self.user_id}~{self.course_id}~{item_id}"
        try:
            r = self.session.get(
                self.base_url + f"onDemandWidgetSessions.v1/{wid}",
                params={"fields": "session,sessionId"}, timeout=30)
            if r.status_code != 200:
                return ""
            return (r.json().get("elements") or [{}])[0].get("sessionId", "") or ""
        except (ValueError, KeyError, TypeError, AttributeError):
            return ""
        except Exception as exc:
            logger.debug(f"Gagal mengambil widget session '{item_id}': {exc}")
            return ""

    def _complete_widget(self, item_id: str) -> bool:
        """Simulate 'Mark as completed': PUT widget progress as Completed."""
        session_id = self._widget_session_id(item_id)
        if not session_id:
            return False
        wid = f"{self.user_id}~{self.course_id}~{item_id}"
        try:
            r = self.session.put(
                self.base_url + f"onDemandWidgetProgress.v1/{wid}",
                json={"widgetProgressId": wid, "sessionId": session_id,
                      "progressState": "Completed"},
                timeout=30)
        except Exception as exc:
            logger.debug(f"Gagal complete widget '{item_id}': {exc}")
            return False
        return self._is_completion_success(r)

    def _try_reverse_completion(self, item_id: str, item_type: str, item_name: str) -> bool:
        cached = self._completion_probe_state.get(item_type)
        if cached is False:
            return False

        if isinstance(cached, tuple):
            method, endpoint, template = cached
            payload = template(item_id) if callable(template) else template
            response = self.session.request(method, self.base_url + endpoint, json=payload)
            if self._is_completion_success(response):
                return True

        attempts = []
        for method, endpoint, payload in self._completion_candidates(item_id, item_type):
            response = self.session.request(method, self.base_url + endpoint, json=payload)
            attempts.append((method, endpoint, response.status_code, response.text[:120]))

            if self._is_completion_success(response):
                # Cache endpoint shape per item type to avoid probing repeatedly.
                self._completion_probe_state[item_type] = (
                    method,
                    endpoint,
                    lambda next_item_id, m=method, e=endpoint, t=item_type: self._completion_candidates(next_item_id, t)[
                        [c[1] for c in self._completion_candidates(next_item_id, t)].index(e)
                    ][2],
                )
                logger.debug(f"Discovered completion endpoint for {item_type}: {method} {endpoint}")
                return True

        self._completion_probe_state[item_type] = False
        compact = "; ".join([f"{m} {e} => {s}" for m, e, s, _ in attempts])
        logger.debug(
            f"Reverse completion probe failed for '{item_name}' ({item_type}). Tried: {compact}"
        )
        return False


CONTEXT_SETTINGS = dict(help_option_names=["-h", "--help"], max_content_width=100)


@logger.catch
@click.command(context_settings=CONTEXT_SETTINGS)
@click.argument("slug", required=False)
@click.option("--llm", is_flag=True, help="Solve graded/ungraded assignments with an LLM.")
@click.option("-p", "--provider", default="",
              help="LLM provider: gemini, openai, openrouter, 9router, local, perplexity.")
@click.option("--base-url", default="",
              help="OpenAI-compatible base URL, e.g. http://127.0.0.1:8045/v1")
@click.option("-m", "--model", default="", help="Model name to use (see --list-models).")
@click.option("--api-key", default="", help="API key for the endpoint (optional for local routers).")
@click.option("--list-models", "show_models", is_flag=True,
              help="List available models for the resolved provider and exit.")
@click.option("--summary-dir", default="",
              help="Directory for summary/report files (default: reports/<slug>).")
@click.option("--knowledge-dir", default="",
              help="Directory holding the <slug>.md knowledge file (default: knowledge/).")
@click.option("--no-knowledge", is_flag=True, help="Disable knowledge base collection.")
@click.option("--knowledge-lang", default="en", show_default=True,
              help="Subtitle language for lecture transcripts.")
@click.option("--input-token-budget", default=5000, show_default=True, type=int,
              help="Target total token INPUT per request (soal + konteks). Konteks knowledge otomatis memakai sisa anggaran setelah payload soal.")
@click.option("--max-context-chars", default=12000, show_default=True, type=int,
              help="Ceiling maksimum karakter konteks knowledge; efektif dibatasi otomatis oleh --input-token-budget.")
@click.option("--refresh-knowledge", is_flag=True,
              help="Re-fetch transcripts/readings even if .md files exist.")
@click.option("--short-prompt", is_flag=True,
              help="Trim prompts to 400 chars and options to 100 chars; useful for token-tight runs.")
@click.option("--dump-items", is_flag=True, help="Dump every course item payload (debug).")
@click.option("--diagnose", is_flag=True, help="Read-only: list course item types and exit.")
@click.option("--capture", "capture_url", default="",
              help="Playwright recorder: open URL, record /api calls, and exit (for coach/discussion).")
@click.option("--capture-out", default="skipera_capture.json", help="Output file for --capture.")
@click.option("--log-level", type=click.Choice(["debug", "info", "warning", "error"]),
              default="info", show_default=True, help="Console log verbosity.")
@click.version_option(package_name="skipera", prog_name="skipera")
def main(slug: str, llm: bool, provider: str, base_url: str, model: str, api_key: str,
         show_models: bool, summary_dir: str, knowledge_dir: str, no_knowledge: bool,
         knowledge_lang: str, max_context_chars: int, input_token_budget: int,
         refresh_knowledge: bool, short_prompt: bool, dump_items: bool, diagnose: bool,
         capture_url: str, capture_out: str, log_level: str) -> None:
    """Skipera — skip mandatory Coursera videos, readings and assessments.

    \b
    Examples:
      skipera introduction-psychology
      skipera introduction-psychology --llm
      skipera <slug> --llm --provider openrouter --model google/gemini-2.0-flash-001 --api-key sk-or-...
      skipera <slug> --llm --base-url http://192.168.18.11:8045/v1 --model gemini-3.8-flash-tiered
      skipera --list-models --provider openrouter
      skipera <slug> --diagnose
      skipera --capture https://www.coursera.org/learn/<slug>/coach/<itemId>/<slug>
    """
    logger.remove()
    logger.add(sys.stderr, level=log_level.upper(), colorize=True)

    if capture_url:
        from skipera.capture import run_capture
        run_capture(capture_url, capture_out)
        return

    llm_settings = resolve_llm_settings(provider, base_url, model, api_key)

    if show_models:
        _print_models(llm_settings)
        return

    if not slug:
        raise click.UsageError("Missing argument 'SLUG'. Berikan course slug, atau pakai --list-models.")
    if not diagnose and not llm:
        logger.warning("Mode tanpa --llm: hanya video/materi yang akan diproses, kuis dilewati.")

    if llm:
        logger.info(
            f"LLM | provider={llm_settings['provider']} "
            f"| model={llm_settings['model'] or '(belum diset)'} "
            f"| endpoint={llm_settings['base_url'] or '(Gemini SDK)'}"
        )
        if llm_settings["provider"] != "gemini" and not llm_settings["model"]:
            raise click.UsageError(
                "Endpoint OpenAI-compatible wajib punya nama model. "
                "Pakai --model <nama> atau --list-models untuk melihat pilihan."
            )
        ensure_model_available(llm_settings)

    skipera = Skipera(slug, llm, llm_settings=llm_settings,
                      summary_dir=summary_dir, dump_items=dump_items, diagnose=diagnose,
                      knowledge_dir=knowledge_dir, no_knowledge=no_knowledge,
                      knowledge_lang=knowledge_lang, max_context_chars=max_context_chars,
                      input_token_budget=input_token_budget,
                      refresh_knowledge=refresh_knowledge, short_prompt=short_prompt)
    skipera.get_course()


def _print_models(settings: dict) -> None:
    endpoint = settings.get("base_url") or "(Gemini SDK)"
    try:
        models = list_available_models(settings)
    except Exception as exc:
        logger.error(f"Gagal mengambil daftar model dari {endpoint}: {exc}")
        raise SystemExit(1)

    click.echo(f"Provider : {settings['provider']}")
    click.echo(f"Endpoint : {endpoint}")
    click.echo(f"Total    : {len(models)} model")
    click.echo("")
    for name in models:
        marker = "  *" if name == settings.get("model") else "   "
        click.echo(f"{marker} {name}")


if __name__ == '__main__':
    main()
