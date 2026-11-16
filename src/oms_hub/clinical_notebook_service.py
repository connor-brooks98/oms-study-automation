"""Encrypted, explicit Clinical Notebook delivery with immutable per-file receipts."""

import hashlib
import json
import mimetypes
import secrets
import threading
from collections.abc import Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from oms_hub import clinical_notebook as api
from oms_hub.files.atomic import sha256_file
from oms_hub.files.pdf import validate_pdf
from oms_hub.security.secret_store import SecretStore
from oms_hub.study_generation.notebook_storage import EncryptedNotebookStorage, _restrict_owner_only


@dataclass
class Material:
    filename: str
    stream: BinaryIO
    role: str
    revision_id: int
    expected_sha256: str | None = None


class ClinicalNotebookService:
    def __init__(self, root: Path, secret_store: SecretStore):
        self.root, self.secrets = root.resolve(), secret_store
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="clinical-delivery")
        self.active: dict[tuple[str, str], Future[None]] = {}
        self.client_factory = api.http_client

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=True)

    def directory(self, owner: str) -> Path:
        return self.root / hashlib.sha256(owner.encode()).hexdigest()

    @contextmanager
    def state(self, owner: str, *, write: bool = False) -> Iterator[dict[str, Any]]:
        with self.lock:
            storage = EncryptedNotebookStorage(
                self.directory(owner) / "connection.enc", self.secrets
            )
            with storage.plaintext(writable=write) as path:
                data = json.loads(path.read_text()) if path.is_file() else {}
                yield data
                if write:
                    path.write_text(json.dumps(data), encoding="utf-8")

    def configure(self, owner: str, endpoint: str, token: str, label: str) -> None:
        endpoint = api.normalize_endpoint(endpoint)
        api.validate_token(token)
        label = label.strip()
        if not label or len(label) > 100:
            raise api.ClinicalNotebookError(
                "Name this Clinical Notebook installation (up to 100 characters)."
            )
        ident = hashlib.sha256(f"{endpoint}\0{token}".encode()).hexdigest()
        with self.state(owner, write=True) as state:
            state.setdefault("instance_id", secrets.token_hex(16))
            if state.get("current") != ident:
                state["destinations"] = {}
                state["notebooks"] = {}
            state.setdefault("connections", {})[ident] = {
                "id": ident,
                "endpoint": endpoint,
                "token": token,
                "label": label,
            }
            state["current"] = ident

    def disconnect(self, owner: str) -> None:
        with self.state(owner, write=True) as state:
            if any(key[0] == owner and not future.done() for key, future in self.active.items()):
                raise api.ClinicalNotebookError(
                    "Wait for the current delivery before disconnecting."
                )
            for connection in state.get("connections", {}).values():
                connection.pop("token", None)
            state.pop("current", None)
            state["destinations"] = {}
            state["notebooks"] = {}

    def connection(self, owner: str, ident: str | None = None) -> dict[str, Any]:
        with self.state(owner) as state:
            connection = state.get("connections", {}).get(ident or state.get("current"))
        if not connection or not connection.get("token"):
            raise api.ClinicalNotebookError("Connect Clinical Notebook in Settings first.")
        return dict(connection)

    def notebooks(self, owner: str) -> list[dict[str, str]]:
        connection = self.connection(owner)
        items: dict[str, str] = {}
        with self.client_factory() as client:
            remote = api.SourceAPI(client, connection["endpoint"], connection["token"])
            capabilities = remote.capabilities()
            cursor = ""
            for _ in range(25):
                page = remote.notebooks(cursor)
                for item in page.get("items", []):
                    if (
                        not isinstance(item.get("id"), str)
                        or not item["id"]
                        or len(item["id"]) > 200
                        or not isinstance(item.get("title"), str)
                    ):
                        raise api.ClinicalNotebookError(
                            "Clinical Notebook returned an invalid notebook list."
                        )
                    items[item["id"]] = item["title"]
                following = page.get("next_cursor")
                if following is None or following == "":
                    break
                if not isinstance(following, str) or following == cursor:
                    raise api.ClinicalNotebookError(
                        "Notebook list is incomplete. Refresh before choosing a destination."
                    )
                cursor = following
            else:
                raise api.ClinicalNotebookError(
                    "Notebook list is too long. Narrow the connection grants."
                )
        with self.state(owner, write=True) as state:
            if state.get("current") != connection["id"]:
                raise api.ClinicalNotebookError(
                    "Connection changed while loading notebooks. Refresh."
                )
            state["notebooks"] = items
            state["capabilities"] = capabilities
            state["destinations"] = {
                k: v for k, v in state.get("destinations", {}).items() if v["notebook_id"] in items
            }
        return [{"id": ident, "title": title} for ident, title in items.items()]

    def save_destination(self, owner: str, exam_key: str, notebook_id: str) -> None:
        with self.state(owner, write=True) as state:
            title = state.get("notebooks", {}).get(notebook_id)
            if title is None or not state.get("current"):
                raise api.ClinicalNotebookError("Load and choose an authorized notebook first.")
            state.setdefault("destinations", {})[exam_key] = {
                "notebook_id": notebook_id,
                "notebook_title": title,
                "connection_id": state["current"],
            }

    def status(self, owner: str, exam_key: str | None = None) -> dict[str, Any]:
        with self.state(owner) as state:
            connection = state.get("connections", {}).get(state.get("current"))
            destination = state.get("destinations", {}).get(exam_key)
        return {
            "configured": bool(connection and connection.get("token")),
            "installation": connection["label"] if connection else None,
            "endpoint": connection["endpoint"] if connection else None,
            "destination": {k: v for k, v in destination.items() if k != "connection_id"}
            if destination
            else None,
        }

    def enqueue(self, owner: str, exam_key: str, lecture_id: int, files: Sequence[Material]) -> str:
        if not files or len(files) > 2 or len({f.role for f in files}) != len(files):
            raise api.ClinicalNotebookError(
                "Select the available lecture PDF and/or cleaned transcript."
            )
        with self.state(owner) as state:
            destination = state.get("destinations", {}).get(exam_key)
            if not destination or destination["connection_id"] != state.get("current"):
                raise api.ClinicalNotebookError(
                    "Choose a Clinical Notebook destination for this exam first."
                )
            destination = dict(destination)
            instance = state["instance_id"]
            limit = min(
                api.MAX_BYTES, state.get("capabilities", {}).get("max_file_bytes", api.MAX_BYTES)
            )
        connection = self.connection(owner, destination["connection_id"])
        directory = self.directory(owner) / "files"
        directory.mkdir(parents=True, exist_ok=True)
        _restrict_owner_only(directory, directory=True)
        temporary: list[Path] = []
        prepared = []
        try:
            for material in sorted(files, key=lambda f: f.role):
                name = material.filename
                if (
                    material.role not in {"slides", "transcript"}
                    or Path(name).name != name
                    or "\\" in name
                    or any(ord(c) < 32 for c in name)
                    or Path(name).suffix.lower() not in {".pdf", ".txt", ".md"}
                ):
                    raise api.ClinicalNotebookError(
                        "Use the approved lecture PDF or cleaned transcript."
                    )
                path = directory / secrets.token_hex(16)
                temporary.append(path)
                size = 0
                with path.open("xb") as target:
                    _restrict_owner_only(path, directory=False)
                    while chunk := material.stream.read(1024 * 1024):
                        size += len(chunk)
                        if size > limit:
                            raise api.ClinicalNotebookError(
                                "The lecture file exceeds this installation’s size limit."
                            )
                        target.write(chunk)
                if not size:
                    raise api.ClinicalNotebookError("Empty materials cannot be delivered.")
                if name.lower().endswith(".pdf"):
                    try:
                        validate_pdf(path)
                    except Exception as error:
                        raise api.ClinicalNotebookError(
                            "The selected slides are not a readable PDF."
                        ) from error
                digest = sha256_file(path)
                if material.expected_sha256 and digest != material.expected_sha256:
                    raise api.ClinicalNotebookError(
                        "Material changed while preparing the delivery. "
                        "Check the current lecture files."
                    )
                document = f"hub-{instance}-{lecture_id}-{material.role}"
                ident = hashlib.sha256(
                    json.dumps(
                        [connection["id"], destination["notebook_id"], document, digest]
                    ).encode()
                ).hexdigest()
                prepared.append(
                    {
                        "id": ident,
                        "filename": name,
                        "path": str(path),
                        "sha256": digest,
                        "size": size,
                        "role": material.role,
                        "revision_id": material.revision_id,
                        "title": name,
                        "media_type": mimetypes.guess_type(name)[0] or "text/plain",
                        "external_document_id": document,
                        "idempotency_key": f"hub-{ident}",
                        "connection_id": connection["id"],
                        "notebook_id": destination["notebook_id"],
                        "metadata": {
                            "study_hub": {
                                "lecture_id": lecture_id,
                                "revision_id": material.revision_id,
                                "role": material.role,
                                "exam_key": exam_key,
                            }
                        },
                        "status": "queued",
                        "message": "Queued",
                        "receipt": None,
                    }
                )
            ids = [item["id"] for item in prepared]
            job_id = hashlib.sha256(json.dumps(ids).encode()).hexdigest()
            with self.state(owner, write=True) as state:
                if state.get("current") != connection["id"]:
                    raise api.ClinicalNotebookError(
                        "Connection changed while preparing files. Choose the destination again."
                    )
                deliveries = state.setdefault("deliveries", {})
                for item in prepared:
                    if any(
                        old["connection_id"] == item["connection_id"]
                        and old["notebook_id"] == item["notebook_id"]
                        and old["external_document_id"] == item["external_document_id"]
                        and old["sha256"] != item["sha256"]
                        for old in deliveries.values()
                    ):
                        raise api.ClinicalNotebookError(
                            "Lecture content changed after a delivery was prepared. "
                            "Review the source in Clinical Notebook before replacing it."
                        )
                for item in prepared:
                    if item["id"] not in deliveries:
                        deliveries[item["id"]] = item
                        temporary.remove(Path(item["path"]))
                state.setdefault("jobs", {}).setdefault(
                    job_id,
                    {
                        "id": job_id,
                        "lecture_id": lecture_id,
                        "files": ids,
                        "connection_id": connection["id"],
                        "installation": connection["label"],
                        "endpoint": connection["endpoint"],
                        "notebook_id": destination["notebook_id"],
                        "notebook_title": destination["notebook_title"],
                    },
                )
            self.resume(owner, job_id)
            return job_id
        finally:
            for path in temporary:
                path.unlink(missing_ok=True)

    def resume(self, owner: str, job_id: str) -> None:
        with self.lock:
            active = self.active.get((owner, job_id))
            if active and not active.done():
                return
            with self.state(owner) as state:
                job = state.get("jobs", {}).get(job_id)
                if not job:
                    raise api.ClinicalNotebookError("Delivery not found for this Hub owner.")
                pending = any(not state["deliveries"][ident]["receipt"] for ident in job["files"])
            if not pending:
                return
            if sum(not f.done() for f in self.active.values()) >= 20:
                raise api.ClinicalNotebookError(
                    "The delivery queue is full. Wait for current transfers."
                )
            self.active = {k: v for k, v in self.active.items() if not v.done()}
            self.active[(owner, job_id)] = self.executor.submit(self._run, owner, job_id)

    def _run(self, owner: str, job_id: str) -> None:
        with self.state(owner) as state:
            job = state["jobs"][job_id]
        for ident in job["files"]:
            with self.state(owner) as state:
                item = state["deliveries"][ident]
            if item["receipt"]:
                continue
            try:
                connection = self.connection(owner, job["connection_id"])
                path = Path(item["path"])
                if (
                    path.is_symlink()
                    or not path.is_relative_to(self.directory(owner) / "files")
                    or not path.is_file()
                    or sha256_file(path) != item["sha256"]
                ):
                    raise api.ClinicalNotebookError(
                        "Prepared material is missing or changed. Review the saved delivery."
                    )
                with self.state(owner, write=True) as state:
                    state["deliveries"][ident].update(
                        status="sending", message="Sending pinned material"
                    )
                with self.client_factory() as client:
                    receipt = api.SourceAPI(
                        client, connection["endpoint"], connection["token"]
                    ).upload(job["notebook_id"], item)
                with self.state(owner, write=True) as state:
                    state["deliveries"][ident].update(
                        receipt=receipt,
                        status=receipt["status"],
                        message="Awaiting acceptance by Clinical Notebook"
                        if receipt["status"] == "pending"
                        else "Received by Clinical Notebook",
                    )
            except Exception as error:
                retryable = isinstance(error, api.ClinicalNotebookError) and error.retryable
                message = (
                    str(error)
                    if isinstance(error, api.ClinicalNotebookError)
                    else "Delivery interrupted. Retry the saved material."
                )
                with self.state(owner, write=True) as state:
                    state["deliveries"][ident].update(
                        status="pending" if retryable else "needs_attention", message=message
                    )
                if isinstance(error, api.ClinicalNotebookError) and error.invalidates_destination:
                    self.invalidate_destination(owner, job)

    def invalidate_destination(self, owner: str, job: dict[str, Any]) -> None:
        with self.state(owner, write=True) as state:
            state["destinations"] = {
                exam: destination
                for exam, destination in state.get("destinations", {}).items()
                if (destination["connection_id"], destination["notebook_id"])
                != (job["connection_id"], job["notebook_id"])
            }

    def check(self, owner: str, job_id: str) -> None:
        with self.state(owner) as state:
            job = state.get("jobs", {}).get(job_id)
            if not job:
                raise api.ClinicalNotebookError("Delivery not found for this Hub owner.")
            items = [state["deliveries"][ident] for ident in job["files"]]
        connection = self.connection(owner, job["connection_id"])
        with self.client_factory() as client:
            remote = api.SourceAPI(client, connection["endpoint"], connection["token"])
            for item in items:
                if not item["receipt"]:
                    continue
                try:
                    receipt = remote.check(job["notebook_id"], item)
                except api.ClinicalNotebookError as error:
                    if error.invalidates_destination:
                        self.invalidate_destination(owner, job)
                    raise
                with self.state(owner, write=True) as state:
                    state["deliveries"][item["id"]].update(
                        receipt=receipt,
                        status=receipt["status"],
                        message="Awaiting acceptance by Clinical Notebook"
                        if receipt["status"] == "pending"
                        else "Received by Clinical Notebook",
                    )

    def jobs(self, owner: str, *, lecture_id: int | None = None) -> list[dict[str, Any]]:
        with self.state(owner) as state:
            jobs = list(state.get("jobs", {}).values())
            deliveries = state.get("deliveries", {})
        result = []
        for job in reversed(jobs):
            if lecture_id is not None and job["lecture_id"] != lecture_id:
                continue
            files = [
                {
                    k: item[k]
                    for k in (
                        "filename",
                        "role",
                        "revision_id",
                        "sha256",
                        "status",
                        "message",
                        "receipt",
                    )
                }
                for ident in job["files"]
                for item in [deliveries[ident]]
            ]
            active = self.active.get((owner, job["id"]))
            running = active and not active.done()
            for item in files:
                if item["status"] in {"queued", "sending"} and not running:
                    item.update(
                        status="pending",
                        message="Delivery paused. Retry the pinned material "
                        "when Clinical Notebook is available.",
                    )
            states = {item["status"] for item in files}
            status = (
                "sending"
                if running
                else "ready"
                if states == {"ready"}
                else "needs_attention"
                if "needs_attention" in states
                else "partial"
                if states & {"ready", "accepted"} and states & {"pending", "queued"}
                else "accepted"
                if states <= {"ready", "accepted"}
                else "pending"
            )
            result.append(
                {
                    **{k: v for k, v in job.items() if k not in {"connection_id", "files"}},
                    "files": files,
                    "status": status,
                    "can_retry": not running and any(not item["receipt"] for item in files),
                }
            )
        return result
