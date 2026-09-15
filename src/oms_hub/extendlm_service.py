"""Per-browser encrypted grants and durable, explicitly resumed upload receipts."""

import hashlib
import hmac
import json
import secrets
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

from oms_hub import extendlm as api
from oms_hub.files.atomic import sha256_file
from oms_hub.files.pdf import validate_pdf
from oms_hub.security.secret_store import SecretStore
from oms_hub.study_generation.notebook_storage import EncryptedNotebookStorage, _restrict_owner_only


class ExtendLMService:
    def __init__(self, root: Path, secret_store: SecretStore):
        self.root, self.secrets = root, secret_store
        self.lock = threading.RLock()
        # ponytail: one upload worker for this personal Hub; add per-account workers if needed.
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="extendlm-upload")
        self.active: dict[tuple[str, str], Future[None]] = {}
        self.client_factory = api.http_client

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=True)

    def session_key(self, cookie: str, owner: str) -> str:
        if len(cookie) != 64 or any(c not in "0123456789abcdef" for c in cookie):
            raise api.ExtendLMError("Sign in to ExtendLM on this computer first.")
        return hashlib.sha256(f"{owner}:{cookie}".encode()).hexdigest()

    @contextmanager
    def state(self, key: str, *, write: bool = False) -> Iterator[dict[str, Any]]:
        with self.lock:
            storage = EncryptedNotebookStorage(self.root / key / "session.enc", self.secrets)
            with storage.plaintext(writable=write) as path:
                data = json.loads(path.read_text()) if path.is_file() else {}
                yield data
                if write:
                    path.write_text(json.dumps(data), encoding="utf-8")

    def begin(self, owner: str, callback: str) -> tuple[str, str]:
        cookie = secrets.token_hex(32)
        key = self.session_key(cookie, owner)
        client_key = hashlib.sha256(callback.encode()).hexdigest()
        with self.state(client_key, write=True) as registered:
            if not registered.get("client_id"):
                with self.client_factory() as client:
                    registered["client_id"] = api.register(client, callback)
            client_id = registered["client_id"]
        url, pending = api.authorization(client_id, callback)
        with self.state(key, write=True) as state:
            state.update(pending=pending, expires=time.time() + 30 * 86400, jobs={})
        return cookie, url

    def finish(self, key: str, code: str, state_value: str, issuer: str) -> None:
        with self.state(key, write=True) as state:
            pending = state.get("pending", {})
            if (
                not pending
                or pending["expires"] < time.time()
                or not hmac.compare_digest(pending["state"], state_value)
                or issuer != api.ISSUER
                or not code
                or len(code) > 8192
            ):
                raise api.ExtendLMError(
                    "Sign-in expired or did not match this browser. Start again."
                )
            # Persist consumption before the network call; callbacks cannot be replayed.
            del state["pending"]
        with self.client_factory() as client:
            tokens = api.exchange(
                client,
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": pending["callback"],
                    "client_id": pending["client_id"],
                    "code_verifier": pending["verifier"],
                },
            )
        with self.state(key, write=True) as state:
            state.update(tokens=tokens, client_id=pending["client_id"])

    def token(self, key: str) -> str:
        with self.state(key, write=True) as state:
            if state.get("expires", 0) < time.time() or not state.get("tokens"):
                raise api.ExtendLMError("Sign in to ExtendLM on this computer first.")
            tokens = state["tokens"]
            if tokens["expires_at"] <= time.time() + 60:
                refresh = tokens.get("refresh_token")
                if not refresh:
                    raise api.ExtendLMError("ExtendLM sign-in expired. Sign in again.")
                with self.client_factory() as client:
                    updated = api.exchange(
                        client,
                        {
                            "grant_type": "refresh_token",
                            "refresh_token": refresh,
                            "client_id": state["client_id"],
                        },
                    )
                updated.setdefault("refresh_token", refresh)
                state["tokens"] = tokens = updated
            token: str = tokens["access_token"]
            return token

    @contextmanager
    def connection(self, key: str) -> Iterator[api.MCPClient]:
        token = self.token(key)
        with self.client_factory() as client:
            mcp = api.MCPClient(client, token)
            mcp.initialize()
            yield mcp

    def disconnect(self, key: str) -> None:
        with self.lock:
            if any(k[0] == key and not f.done() for k, f in self.active.items()):
                raise api.ExtendLMError(
                    "Wait for this computer's upload to stop before disconnecting."
                )
            with self.state(key) as state:
                tokens, client_id = state.get("tokens", {}), state.get("client_id")
            if tokens:
                with self.client_factory() as client:
                    api.checked(
                        client.post(
                            f"{api.OAUTH}/revoke",
                            data={
                                "token": tokens.get("refresh_token") or tokens["access_token"],
                                "client_id": client_id,
                            },
                        )
                    )
            with self.state(key, write=True) as state:
                state.pop("tokens", None)
                state.pop("selection", None)
                state.pop("pending", None)
                state["expires"] = 0

    def accounts(self, key: str) -> list[dict[str, str]]:
        with self.connection(key) as client:
            result = client.call("list_notebook_users", {})
        choices: dict[str, dict[str, str]] = {}
        public = []
        for index, connection in enumerate(result.get("connections", []), start=1):
            for user in connection.get("users", []):
                selector, account = connection.get("extension_connection"), user.get("auth_user_id")
                if (
                    not isinstance(selector, str)
                    or not isinstance(account, str)
                    or not 16 <= len(selector) <= 128
                    or not account.isdecimal()
                ):
                    raise api.ExtendLMError(
                        "ExtendLM returned an invalid browser/account selection."
                    )
                choice_id = hashlib.sha256(f"{selector}:{account}".encode()).hexdigest()
                choices[choice_id] = {"extension_connection": selector, "auth_user_id": account}
                email = user.get("email") if connection.get("emails_shared") else None
                label = f"Browser {index} · Account {account}" + (f" · {email}" if email else "")
                public.append({"id": choice_id, "label": label})
        with self.state(key, write=True) as state:
            state["choices"] = choices
        return public

    def select(self, key: str, choice_id: str) -> None:
        with self.state(key) as state:
            selected = state.get("choices", {}).get(choice_id)
        if not selected:
            raise api.ExtendLMError("Refresh connections and choose an advertised account.")
        with self.connection(key) as client:
            result = client.call("get_notebooklm_capabilities", selected)
            if not result.get("authenticated"):
                raise api.ExtendLMError(
                    "Sign in to Gemini Notebook in the selected browser profile."
                )
        with self.state(key, write=True) as state:
            state["selection"] = selected
            state["notebooks"] = {}

    def selection(self, key: str) -> dict[str, str]:
        with self.state(key) as state:
            selection = state.get("selection")
        if not selection:
            raise api.ExtendLMError("Choose the browser and Google account before uploading.")
        return dict(selection)

    def notebooks(self, key: str, cursor: str = "") -> dict[str, Any]:
        selection = self.selection(key)
        with self.connection(key) as client:
            result = client.call("list_notebooks", {**selection, "limit": 200, "cursor": cursor})
        with self.state(key, write=True) as state:
            if state.get("selection") != selection:
                raise api.ExtendLMError(
                    "Account changed while loading notebooks. Refresh the list."
                )
            if not cursor:
                state["notebooks"] = {}
            for item in result.get("items", []):
                if not item.get("is_public"):
                    state["notebooks"][item["id"]] = item["title"]
        return {
            "items": [i for i in result.get("items", []) if not i.get("is_public")],
            "next_cursor": result.get("next_cursor", ""),
        }

    def enqueue(
        self,
        key: str,
        notebook_id: str,
        files: Sequence[tuple[str, BinaryIO]],
        *,
        lecture_id: int | None = None,
    ) -> str:
        self.token(key)
        selection = self.selection(key)
        with self.state(key) as state:
            title = state.get("notebooks", {}).get(notebook_id)
        if title is None:
            raise api.ExtendLMError("Choose a notebook from the loaded account's list.")
        if not 1 <= len(files) <= 20:
            raise api.ExtendLMError("Choose between 1 and 20 files.")
        directory = self.root / key / "files"
        directory.mkdir(parents=True, exist_ok=True)
        _restrict_owner_only(directory, directory=True)
        inputs = []
        total = 0
        temporary: list[Path] = []
        try:
            for filename, stream in files:
                api.validate_filename(filename)
                path = directory / secrets.token_hex(16)
                temporary.append(path)
                size = 0
                with path.open("xb") as target:
                    _restrict_owner_only(path, directory=False)
                    while chunk := stream.read(1024 * 1024):
                        size += len(chunk)
                        total += len(chunk)
                        if size > api.MAX_BYTES or total > 500 * 1024 * 1024:
                            raise api.ExtendLMError(
                                "Use files under 100 MiB and batches under 500 MiB."
                            )
                        target.write(chunk)
                if not size:
                    raise api.ExtendLMError("Empty files cannot be uploaded.")
                if filename.lower().endswith(".pdf"):
                    try:
                        validate_pdf(path)
                    except Exception as error:
                        raise api.ExtendLMError(
                            "The selected slides are not a readable PDF."
                        ) from error
                digest = sha256_file(path)
                inputs.append(
                    {
                        "filename": filename,
                        "sha256": digest,
                        "path": str(path),
                        "stage": "pending",
                        "size": size,
                    }
                )
            fingerprint = json.dumps(
                [selection, notebook_id, [(i["filename"], i["sha256"]) for i in inputs]],
                sort_keys=True,
            )
            job_id = hashlib.sha256(fingerprint.encode()).hexdigest()
            with self.state(key, write=True) as state:
                if state.get("selection") != selection:
                    raise api.ExtendLMError(
                        "Account changed while preparing files. Choose it again."
                    )
                if job_id not in state["jobs"]:
                    for index, item in enumerate(inputs):
                        item["idempotency_key"] = f"hub-{job_id}-{index}"
                    state["jobs"][job_id] = {
                        "id": job_id,
                        "notebook_id": notebook_id,
                        "notebook_title": title,
                        "selection": selection,
                        "files": inputs,
                        "status": "queued",
                        "created_at": time.time(),
                        "lecture_id": lecture_id,
                        "message": "Queued",
                    }
                    temporary.clear()
            self.resume(key, job_id)
            return job_id
        finally:
            for path in temporary:
                path.unlink(missing_ok=True)

    def resume(self, key: str, job_id: str) -> None:
        self.token(key)
        with self.lock:
            existing = self.active.get((key, job_id))
            if existing is not None and not existing.done():
                return
            with self.state(key, write=True) as state:
                job = state.get("jobs", {}).get(job_id)
                if not job:
                    raise api.ExtendLMError("Upload receipt not found for this browser sign-in.")
                if job["status"] in {"accepted", "partial", "ready"}:
                    return
                if sum(not f.done() for f in self.active.values()) >= 20:
                    raise api.ExtendLMError("The upload queue is full. Wait for current uploads.")
                job.update(status="queued", message="Queued; original browser/account retained")
            self.active = {k: f for k, f in self.active.items() if not f.done()}
            self.active[(key, job_id)] = self.executor.submit(self._run, key, job_id)

    def _update_job(self, key: str, job_id: str, change: Callable[[dict[str, Any]], None]) -> None:
        with self.state(key, write=True) as state:
            change(state["jobs"][job_id])

    def _update_file(self, key: str, job_id: str, index: int, item: dict[str, Any]) -> None:
        with self.state(key, write=True) as state:
            state["jobs"][job_id]["files"][index] = item.copy()

    def _run(self, key: str, job_id: str) -> None:
        try:
            self._update_job(
                key, job_id, lambda j: j.update(status="uploading", message="Uploading")
            )
            with self.state(key) as state:
                job = state["jobs"][job_id]
            with self.connection(key) as client:
                if not client.call("get_notebooklm_capabilities", job["selection"]).get(
                    "authenticated"
                ):
                    raise api.ExtendLMError(
                        "The original browser/account is offline. Reopen it and resume."
                    )
                for index, item in enumerate(job["files"]):
                    if item["stage"] == "accepted":
                        continue
                    path = Path(item["path"])
                    if (
                        not path.is_relative_to(self.root / key / "files")
                        or not path.is_file()
                        or sha256_file(path) != item["sha256"]
                    ):
                        raise api.ExtendLMError(
                            "Staged file is missing or changed. Start a new upload."
                        )
                    kind = "pdf" if item["filename"].lower().endswith(".pdf") else "file"
                    if item["stage"] not in {"uploaded", "adding"}:
                        prepared = client.call(
                            f"prepare_{kind}_source_upload",
                            {
                                **job["selection"],
                                "filename": item["filename"],
                            },
                        )
                        client.upload(prepared, path)
                        item.update(stage="uploaded", resource_id=prepared["resource_id"])
                        self._update_file(key, job_id, index, item)
                    item["stage"] = "adding"
                    self._update_file(key, job_id, index, item)
                    result = client.call(
                        f"add_{kind}_source",
                        {
                            **job["selection"],
                            "notebook_id": job["notebook_id"],
                            "resource_id": item["resource_id"],
                            "idempotency_key": item["idempotency_key"],
                            **({"split_mode": "single"} if kind == "pdf" else {}),
                        },
                    )
                    ids = result.get(
                        "source_ids", [result["source_id"]] if "source_id" in result else []
                    )
                    if (
                        result.get("notebook_id") != job["notebook_id"]
                        or result.get("resource_id") != item["resource_id"]
                        or not ids
                        or any(not isinstance(source_id, str) or not source_id for source_id in ids)
                    ):
                        raise api.ExtendLMError(
                            "ExtendLM has not confirmed matching source IDs. Resume to reconcile."
                        )
                    item.update(
                        stage="accepted",
                        source_ids=ids,
                        complete=result.get("complete", True),
                        source_status="unknown",
                    )
                    self._update_file(key, job_id, index, item)
                    path.unlink(missing_ok=True)
            complete = all(i.get("complete", False) for i in job["files"])
            self._update_job(
                key,
                job_id,
                lambda j: j.update(
                    status="accepted" if complete else "partial",
                    message="Sources accepted; check indexing."
                    if complete
                    else "Partial import. Review notebook; accepted files will not be resent.",
                ),
            )
        except Exception as error:
            message = (
                str(error)
                if isinstance(error, api.ExtendLMError)
                else ("Upload stopped. Check the connection and resume using this receipt.")
            )
            self._update_job(
                key, job_id, lambda j: j.update(status="needs_attention", message=message)
            )

    def jobs(self, key: str) -> list[dict[str, Any]]:
        with self.state(key) as state:
            jobs = list(state.get("jobs", {}).values())
        result = []
        for job in reversed(jobs):
            active = self.active.get((key, job["id"]))
            if job["status"] in {"queued", "uploading"} and (active is None or active.done()):
                job.update(
                    status="needs_attention", message="Hub restarted. Resume the saved upload."
                )
            result.append(
                {k: job[k] for k in ("id", "notebook_title", "status", "message")}
                | {
                    "files": [
                        {k: i.get(k) for k in ("filename", "stage", "source_ids", "source_status")}
                        for i in job["files"]
                    ],
                }
            )
        return result

    def check_sources(self, key: str, job_id: str) -> None:
        with self.state(key) as state:
            job = state.get("jobs", {}).get(job_id)
        if not job:
            raise api.ExtendLMError("Upload receipt not found.")
        statuses: dict[str, str] = {}
        with self.connection(key) as client:
            cursor = ""
            # Bound provider pagination; unobserved sources remain unknown.
            for _ in range(25):
                page = client.call(
                    "list_notebook_sources",
                    {
                        **job["selection"],
                        "notebook_id": job["notebook_id"],
                        "limit": 200,
                        "cursor": cursor,
                    },
                )
                statuses.update({s["id"]: s["source_status"] for s in page.get("items", [])})
                next_cursor = page.get("next_cursor", "")
                if not next_cursor or next_cursor == cursor:
                    break
                cursor = next_cursor

        def update(current: dict[str, Any]) -> None:
            for item in current["files"]:
                observed = [statuses.get(i, "unknown") for i in item.get("source_ids", [])]
                item["source_status"] = (
                    "ready"
                    if observed and all(s == "ready" for s in observed)
                    else "failed"
                    if "failed" in observed
                    else "processing"
                    if "processing" in observed
                    else "unknown"
                )
            if current["status"] in {"accepted", "ready", "indexing_failed"}:
                if all(i["source_status"] == "ready" for i in current["files"]):
                    current.update(
                        status="ready", message="All uploaded sources are indexed and ready."
                    )
                elif any(i["source_status"] == "failed" for i in current["files"]):
                    current.update(
                        status="indexing_failed",
                        message="NotebookLM could not index a source. Review it in the notebook.",
                    )
                else:
                    current.update(
                        status="accepted", message="Indexing is not yet confirmed for every source."
                    )

        self._update_job(key, job_id, update)
