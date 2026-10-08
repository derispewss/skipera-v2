import time

import requests

from .types import QUESTION_TYPE_MAP, MODEL_MAP, deep_blank_model, WHITELISTED_QUESTION_TYPES
from config import GRAPHQL_URL
from .queries import (GET_STATE_QUERY, SAVE_RESPONSES_QUERY, SUBMIT_DRAFT_QUERY,
                      INITIATE_ATTEMPT_QUERY)
from loguru import logger
from llm.connector import build_connector


class GradedSolver(object):
    def __init__(self, session: requests.Session, course_id: str, item_id: str,
                 llm_settings: dict = None):
        self.session: requests.Session = session
        self.course_id: str = course_id
        self.item_id: str = item_id
        self.llm_settings = llm_settings
        self.attempt_id = None
        self.draft_id = None
        self.discarded_questions = []
        self.question_specs = {}
        self.last_error = ""

    def solve(self) -> str:
        """Returns one of: passed, failed, no_attempts, no_llm, error."""
        while True:
            state = self.get_state()

            if state["outcome"] is not None and state["outcome"]["isPassed"]:
                logger.debug("Already passed!")
                return "passed"

            if state["allowedAction"] == "RESUME_DRAFT":
                logger.info("Resuming in-progress attempt...")
                status = self._solve_current_attempt()
                if status == "passed":
                    return "passed"
                if status == "no_llm":
                    return "no_llm"

            elif state["allowedAction"] == "START_NEW_ATTEMPT":
                remaining = state["attempts"]["attemptsRemaining"]
                if remaining == 0:
                    logger.error("No more attempts can be made!")
                    return "no_attempts"

                logger.info(f"Starting a new attempt ({remaining} remaining before this run)...")
                if not self.initiate_attempt():
                    logger.error("Could not start an attempt. Please file an issue.")
                    return "error"

                status = self._solve_current_attempt()
                if status == "passed":
                    return "passed"
                if status == "no_llm":
                    return "no_llm"

            else:
                logger.error("Something went wrong! Please file an issue.")
                return "error"

            state = self.get_state()
            if state["outcome"] is not None and state["outcome"]["isPassed"]:
                return "passed"

            if state["allowedAction"] != "START_NEW_ATTEMPT" or state["attempts"]["attemptsRemaining"] == 0:
                logger.error("Sorry! Could not pass the assignment, maybe use a better model.")
                return "failed"

            logger.warning("Attempt not passed. Retrying with a new attempt...")

    def _solve_current_attempt(self) -> str:
        try:
            connector = build_connector(self.llm_settings)
        except Exception as exc:
            logger.error(f"Skipping graded assessment: could not build LLM connector ({exc}).")
            return "no_llm"

        questions = self.retrieve_questions()
        try:
            answers = connector.get_response(questions)
        except Exception as exc:
            self.last_error = str(exc)
            logger.error(f"Could not get answers from LLM: {exc}")
            return "error"

        if not self.save_responses(answers.get("responses", answers)):
            logger.error("Could not save responses. Please file an issue.")
            return "error"

        if not self.submit_draft():
            logger.error("Could not submit the assignment. Please file an issue.")
            return "error"

        logger.debug("Waiting 3 seconds for grading..")
        time.sleep(3)  # delay for grading process
        return "passed" if self.get_grade() else "failed"

    def get_state(self) -> dict:
        """
        Retrieves the current state of the assessment.
        """
        res = self.session.post(url=GRAPHQL_URL, params={
            "opname": "QueryState"
        }, json={
            "operationName": "QueryState",
            "variables": {
                "courseId": self.course_id,
                "itemId": self.item_id
            },
            "query": GET_STATE_QUERY
        }).json()

        return res["data"]["SubmissionState"]["queryState"]

    def initiate_attempt(self) -> bool:
        """
        Initiates a new attempt for the assessment.
        """
        res = self.session.post(url=GRAPHQL_URL, params={
            "opname": "Submission_StartAttempt"
        }, json={
            "operationName": "Submission_StartAttempt",
            "variables": {
                "courseId": self.course_id,
                "itemId": self.item_id
            },
            "query": INITIATE_ATTEMPT_QUERY
        })

        if "Submission_StartAttemptSuccess" in res.text:
            return True
        return False

    def retrieve_questions(self) -> dict:
        """
        Retrieves the questions for the particular attempt
        which are to be sent to the LLM Connector.
        """
        state = self.get_state()
        draft = state["attempts"]["inProgressAttempt"]

        self.draft_id = draft["id"]
        self.attempt_id = draft["draft"]["id"]
        questions = draft["draft"]["parts"]
        questions_formatted = {}
        self.discarded_questions = []
        self.question_specs = {}

        for question in questions:
            typename = question["__typename"]
            if typename not in QUESTION_TYPE_MAP:  # discard unknown question types
                continue

            self.question_specs[question["partId"]] = {
                "typename": typename,
                "response_field": QUESTION_TYPE_MAP[typename][0],
                "question_type": QUESTION_TYPE_MAP[typename][1],
            }

            if typename not in WHITELISTED_QUESTION_TYPES:
                self.discarded_questions.append({
                    "questionId": question["partId"],
                    "questionType": QUESTION_TYPE_MAP[typename][1],
                    "questionResponse": {
                        QUESTION_TYPE_MAP[typename][0]:
                        deep_blank_model(MODEL_MAP[typename])
                    }
                })
                continue

            questions_formatted[question["partId"]] = self._format_question_for_llm(question)

        if self.discarded_questions:
            types = sorted({q["questionType"] for q in self.discarded_questions})
            logger.warning(
                f"Melewati {len(self.discarded_questions)} soal bertipe belum didukung: "
                f"{', '.join(types)}. Soal ini akan dikirim kosong."
            )

        return questions_formatted

    def _format_question_for_llm(self, question: dict) -> dict:
        typename = question["__typename"]
        schema = question.get("questionSchema", {})
        payload = {
            "question_id": question["partId"],
            "question_type": QUESTION_TYPE_MAP[typename][1],
            "prompt": schema.get("prompt", {}).get("cmlValue") or schema.get("prompt", {}).get("value") or "",
        }

        if "options" in schema:
            payload["options"] = [
                {
                    "option_id": option.get("optionId", ""),
                    "value": option.get("display", {}).get("cmlValue") or option.get("display", {}).get("value") or "",
                }
                for option in schema["options"]
            ]

        if typename in {"Submission_MultipleChoiceQuestion", "Submission_MultipleChoiceReflectQuestion"}:
            payload["answer_format"] = {
                "multipleChoiceResponse": {
                    "chosen": "single option_id string"
                }
            }
        elif typename in {"Submission_CheckboxQuestion", "Submission_CheckboxReflectQuestion"}:
            payload["answer_format"] = {
                "checkboxResponse": {
                    "chosen": ["option_id", "..."]
                }
            }
        elif typename == "Submission_PlainTextQuestion":
            payload["answer_format"] = {
                "plainTextResponse": {
                    "plainText": "short plain answer"
                }
            }
        else:
            payload["answer_format"] = {
                QUESTION_TYPE_MAP[typename][0]: {
                    "answer": "short answer"
                }
            }

        return payload

    def _blank_response_payload(self, question_id: str) -> dict:
        spec = self.question_specs[question_id]
        return {
            "questionId": question_id,
            "questionType": spec["question_type"],
            "questionResponse": {
                spec["response_field"]: deep_blank_model(MODEL_MAP[spec["typename"]])
            }
        }

    def _coerce_llm_answer(self, question_id: str, answer: dict) -> dict:
        spec = self.question_specs[question_id]
        response_field = spec["response_field"]

        raw_response = answer.get("question_response", {})
        if response_field in raw_response:
            return {
                "questionId": question_id,
                "questionType": spec["question_type"],
                "questionResponse": {
                    response_field: raw_response[response_field]
                }
            }

        # Backward compatibility for the old connector schema.
        if answer.get("type") in {"Single", "Multi"} and "option_id" in answer:
            if answer["type"] == "Single":
                candidate = {
                    "multipleChoiceResponse": {
                        "chosen": answer["option_id"][0] if answer["option_id"] else ""
                    }
                }
            else:
                candidate = {
                    "checkboxResponse": {
                        "chosen": answer["option_id"]
                    }
                }

            if response_field in candidate:
                return {
                    "questionId": question_id,
                    "questionType": spec["question_type"],
                    "questionResponse": {
                        response_field: candidate[response_field]
                    }
                }

        return self._blank_response_payload(question_id)

    def save_responses(self, answers: dict) -> bool:
        """
        Saves the responses for the assessment to the draft.
        """
        if isinstance(answers, dict):
            answers = answers.get("responses", [])

        answers_by_id = {}
        for answer in answers or []:
            question_id = answer.get("question_id")
            if question_id:
                answers_by_id[question_id] = answer

        answer_responses = []
        for question_id in self.question_specs:
            answer = answers_by_id.get(question_id)
            if not answer:
                answer_responses.append(self._blank_response_payload(question_id))
                continue

            answer_responses.append(self._coerce_llm_answer(question_id, answer))

        res = self.session.post(url=GRAPHQL_URL, params={
            "opname": "Submission_SaveResponses"
        }, json={
            "operationName": "Submission_SaveResponses",
            "variables": {
                "input": {
                    "courseId": self.course_id,
                    "itemId": self.item_id,
                    "attemptId": self.draft_id,
                    "questionResponses": answer_responses
                }
            },
            "query": SAVE_RESPONSES_QUERY
        })

        if "Submission_SaveResponsesSuccess" in res.text:
            return True

        logger.debug(answer_responses)
        logger.debug(res.json())
        return False

    def submit_draft(self) -> bool:
        """
        Submits the draft for evaluation after the submission is saved.
        """
        res = self.session.post(url=GRAPHQL_URL, params={
            "opname": "Submission_SubmitLatestDraft"
        }, json={
            "operationName": "Submission_SubmitLatestDraft",
            "query": SUBMIT_DRAFT_QUERY,
            "variables": {
                "input": {
                    "courseId": self.course_id,
                    "itemId": self.item_id,
                    "submissionId": self.attempt_id
                }
            }
        })

        if "Submission_SubmitLatestDraftSuccess" in res.text:
            return True
        return False

    def get_grade(self) -> bool:
        """
        Retrieves the outcome for the submitted assignment.
        """
        res = self.session.post(url=GRAPHQL_URL, params={
            "opname": "QueryState"
        }, json={
            "operationName": "QueryState",
            "query": GET_STATE_QUERY,
            "variables": {
                "courseId": self.course_id,
                "itemId": self.item_id
            }
        }).json()

        outcome = res["data"]["SubmissionState"]["queryState"]["outcome"]

        if outcome is not None:
            logger.debug(f"Achieved {outcome['earnedGrade']} grade. Passed? {outcome['isPassed']}")
        else:
            logger.debug("Outcome is None - check upstream logic")
            return False

        return outcome['isPassed']
