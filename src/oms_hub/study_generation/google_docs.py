from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from oms_hub.config import Settings
from oms_hub.study_generation.native_quiz import validate_native_quiz_url
from oms_hub.study_generation.repository import GenerationRepository


@dataclass(frozen=True, slots=True)
class CourseDocumentRef:
    id: str
    title: str
    subject_key: str


@dataclass(frozen=True, slots=True)
class ExamTabRef:
    document_id: str
    tab_id: str
    exam_number: int


class GoogleDocsAuthenticationError(RuntimeError):
    pass


class GoogleDocsGateway:
    def __init__(
        self,
        repository: GenerationRepository,
        docs: Any,
        drive: Any,
        settings: Settings,
    ):
        self.repository = repository
        self.docs = docs
        self.drive = drive
        self.settings = settings

    def ensure_course_document(self, subject: str) -> CourseDocumentRef:
        subject_key = _subject_key(subject)
        title = f"{subject} Lecture Quizzes"
        stored = self.repository.course_document(subject_key)
        if stored is not None:
            try:
                self.docs.documents().get(documentId=stored.document_id).execute()
                return CourseDocumentRef(stored.document_id, stored.title, subject_key)
            except Exception:  # noqa: BLE001 - deleted/inaccessible document is recreated
                stored = None
        created = (
            self.docs.documents()
            .create(body={"title": title})
            .execute()
        )
        document_id = str(created["documentId"])
        self.repository.save_course_document(
            subject,
            subject_key,
            document_id,
            title,
        )
        return CourseDocumentRef(document_id, title, subject_key)

    def ensure_exam_tab(
        self,
        document: CourseDocumentRef,
        exam_number: int,
    ) -> ExamTabRef:
        stored = self.repository.exam_tab(document.subject_key, exam_number)
        if stored is not None:
            current = (
                self.docs.documents()
                .get(documentId=document.id, includeTabsContent=True)
                .execute()
            )
            if stored.tab_id in _all_tab_ids(current):
                return ExamTabRef(document.id, stored.tab_id, exam_number)
        title = f"Exam {exam_number}"
        response = (
            self.docs.documents()
            .batchUpdate(
                documentId=document.id,
                body={
                    "requests": [
                        {"addDocumentTab": {"tabProperties": {"title": title}}}
                    ]
                },
            )
            .execute()
        )
        try:
            tab_id = str(
                response["replies"][0]["addDocumentTab"]["tabProperties"]["tabId"]
            )
        except (KeyError, IndexError, TypeError) as error:
            raise RuntimeError("Google Docs did not return the new exam tab") from error
        self.repository.save_exam_tab(document.subject_key, exam_number, tab_id)
        return ExamTabRef(document.id, tab_id, exam_number)

    def sync_quiz_link(
        self,
        tab: ExamTabRef,
        lecture_number: int,
        url: str,
    ) -> None:
        trusted_url = validate_native_quiz_url(url, self.settings)
        marker = f"oms-study-hub-quiz-lecture-{lecture_number}"
        label = f"Lecture {lecture_number} Quiz"
        line = f"{label}\n"
        document = (
            self.docs.documents()
            .get(documentId=tab.document_id, includeTabsContent=True)
            .execute()
        )
        all_named = _tab_named_ranges(document, tab.tab_id)
        named = all_named.get(marker, {}).get("namedRanges", [])
        requests: list[dict[str, Any]] = []
        insertion_index: int
        if named:
            segment = named[0]["range"]
            insertion_index = int(segment["startIndex"])
            requests.extend(
                [
                    {"deleteNamedRange": {"name": marker}},
                    {"deleteContentRange": {"range": segment}},
                ]
            )
        else:
            later_indexes = [
                int(item["namedRanges"][0]["range"]["startIndex"])
                for name, item in all_named.items()
                if name.startswith("oms-study-hub-quiz-lecture-")
                and _lecture_number(name) > lecture_number
                and item.get("namedRanges")
            ]
            insertion_index = (
                min(later_indexes)
                if later_indexes
                else _tab_end_index(document, tab.tab_id)
            )
        requests.extend(
            [
                {
                    "insertText": {
                        "location": {
                            "index": insertion_index,
                            "tabId": tab.tab_id,
                        },
                        "text": line,
                    }
                },
                {
                    "updateTextStyle": {
                        "range": {
                            "tabId": tab.tab_id,
                            "startIndex": insertion_index,
                            "endIndex": insertion_index + len(label),
                        },
                        "textStyle": {"link": {"url": trusted_url}},
                        "fields": "link",
                    }
                },
                {
                    "createNamedRange": {
                        "name": marker,
                        "range": {
                            "tabId": tab.tab_id,
                            "startIndex": insertion_index,
                            "endIndex": insertion_index + len(line),
                        },
                    }
                },
            ]
        )
        (
            self.docs.documents()
            .batchUpdate(
                documentId=tab.document_id,
                body={"requests": requests},
            )
            .execute()
        )


class OAuthGoogleDocsGateway:
    def __init__(
        self,
        repository: GenerationRepository,
        connection: Any,
        settings: Settings,
    ):
        self.repository = repository
        self.connection = connection
        self.settings = settings
        self._gateway: GoogleDocsGateway | None = None

    def ensure_course_document(self, subject: str) -> CourseDocumentRef:
        return cast(
            CourseDocumentRef,
            self._authenticated(
                lambda: self._current().ensure_course_document(subject)
            ),
        )

    def ensure_exam_tab(
        self,
        document: CourseDocumentRef,
        exam_number: int,
    ) -> ExamTabRef:
        return cast(
            ExamTabRef,
            self._authenticated(
                lambda: self._current().ensure_exam_tab(document, exam_number)
            ),
        )

    def sync_quiz_link(
        self,
        tab: ExamTabRef,
        lecture_number: int,
        url: str,
    ) -> None:
        self._authenticated(
            lambda: self._current().sync_quiz_link(
                tab,
                lecture_number,
                url,
            )
        )

    def _authenticated(self, action: Callable[[], Any]) -> Any:
        try:
            return action()
        except Exception as error:
            if _is_docs_auth_error(error):
                self._gateway = None
                raise GoogleDocsAuthenticationError(
                    "Google Docs authorization expired; reconnect Google in Settings."
                ) from error
            raise

    def _current(self) -> GoogleDocsGateway:
        if self._gateway is None:
            from googleapiclient.discovery import build  # type: ignore[import-untyped]

            credentials = self.connection.oauth_credentials()
            self._gateway = GoogleDocsGateway(
                self.repository,
                build(
                    "docs",
                    "v1",
                    credentials=credentials,
                    cache_discovery=False,
                ),
                build(
                    "drive",
                    "v3",
                    credentials=credentials,
                    cache_discovery=False,
                ),
                self.settings,
            )
        return self._gateway


def _subject_key(subject: str) -> str:
    return " ".join(subject.casefold().split())


def _is_docs_auth_error(error: Exception) -> bool:
    message = str(error).casefold()
    return any(
        phrase in message
        for phrase in (
            "invalid_grant",
            "invalid credentials",
            "unauthorized",
            "authentication",
            "credentials expired",
            "token has been expired",
        )
    )


def _tab_named_ranges(document: dict[str, Any], tab_id: str) -> dict[str, Any]:
    for tab in document.get("tabs", []):
        if str(tab.get("tabProperties", {}).get("tabId")) == tab_id:
            return dict(tab.get("documentTab", {}).get("namedRanges", {}))
    return dict(document.get("namedRanges", {}))


def _all_tab_ids(document: dict[str, Any]) -> set[str]:
    result: set[str] = set()

    def visit(tabs: list[dict[str, Any]]) -> None:
        for tab in tabs:
            value = tab.get("tabProperties", {}).get("tabId")
            if value is not None:
                result.add(str(value))
            visit(tab.get("childTabs", []))

    visit(document.get("tabs", []))
    return result


def _lecture_number(name: str) -> int:
    try:
        return int(name.rsplit("-", 1)[-1])
    except ValueError:
        return 2**31


def _tab_end_index(document: dict[str, Any], tab_id: str) -> int:
    for tab in document.get("tabs", []):
        if str(tab.get("tabProperties", {}).get("tabId")) != tab_id:
            continue
        content = tab.get("documentTab", {}).get("body", {}).get("content", [])
        if content:
            return max(1, int(content[-1].get("endIndex", 1)) - 1)
    content = document.get("body", {}).get("content", [])
    if content:
        return max(1, int(content[-1].get("endIndex", 1)) - 1)
    return 1
