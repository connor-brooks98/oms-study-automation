from __future__ import annotations

import hashlib
import json
import os
import re
import zipfile
import zlib
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CAPSULE_SCHEMA_VERSION = 1
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_FORBIDDEN_NAMES = {
    ".env",
    "cookies.sqlite",
    "cookies.db",
    "cookies",
    "collection.anki2",
    "collection.media",
    "login-data",
    "login data",
    "credentials.json",
    "id_rsa",
    "id_dsa",
    "id_ed25519",
}
# Full path-component tokens only: this blocks exported credentials without
# rejecting ordinary implementation names such as ``tokenizer.py``.
_SENSITIVE_COMPONENT = re.compile(
    r"(?:^|[._\- ])(?:api[._\- ]*key|apikey|secret|password|passwd|cookie|session|"
    r"token)(?:$|[._\- ])",
    re.IGNORECASE,
)


class CapsuleIntegrityError(ValueError):
    """The capsule cannot be trusted or safely materialized."""


class CapsuleIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    commit_sha: str
    tree_sha: str
    database_schema: int = Field(ge=1)
    companion_generation: str = Field(min_length=1)
    semantic_generation: str = Field(min_length=1)
    companion_note_count: int = Field(ge=1)
    semantic_note_count: int = Field(ge=1)

    @field_validator("commit_sha", "tree_sha")
    @classmethod
    def validate_git_identity(cls, value: str) -> str:
        if not _COMMIT.fullmatch(value):
            raise ValueError("Git identities must be full lowercase SHA-1 values")
        return value

    @model_validator(mode="after")
    def validate_note_counts(self) -> CapsuleIdentity:
        if self.semantic_note_count > self.companion_note_count:
            raise ValueError("semantic notes cannot exceed companion notes")
        return self


class CapsuleFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    bytes: int = Field(ge=0)
    sha256: str

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or not value or ".." in path.parts or "\\" in value:
            raise ValueError("capsule file paths must be safe relative POSIX paths")
        if value == "capsule.json":
            raise ValueError("capsule.json is self-excluding")
        return value

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("file SHA-256 is invalid")
        return value


class CapsuleManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = CAPSULE_SCHEMA_VERSION
    identity: CapsuleIdentity
    logical_roots: dict[str, str]
    source_roots: dict[str, str]
    files: tuple[CapsuleFile, ...]
    manifest_rule: str = "self-excluding"

    @model_validator(mode="after")
    def validate_manifest(self) -> CapsuleManifest:
        if self.schema_version != CAPSULE_SCHEMA_VERSION:
            raise ValueError("unsupported capsule schema")
        if self.manifest_rule != "self-excluding":
            raise ValueError("capsule manifest must be self-excluding")
        if set(self.logical_roots) != set(self.source_roots):
            raise ValueError("logical and source roots must have identical keys")
        paths = [entry.path for entry in self.files]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("capsule files must be unique and sorted")
        for value in self.logical_roots.values():
            path = PurePosixPath(value)
            if path.is_absolute() or ".." in path.parts or "\\" in value:
                raise ValueError("logical roots must be safe relative POSIX paths")
        return self


def build_capsule_manifest(
    root: Path,
    *,
    identity: CapsuleIdentity,
    logical_roots: dict[str, str],
    source_roots: dict[str, str],
) -> CapsuleManifest:
    resolved = _resolve_capsule_root(root)
    if not resolved.is_dir():
        raise CapsuleIntegrityError("capsule root is unavailable")
    files: list[CapsuleFile] = []
    for path in sorted(resolved.rglob("*")):
        if path.name == "capsule.json" and path.parent == resolved:
            continue
        if path.is_symlink():
            raise CapsuleIntegrityError(f"capsule contains a symbolic link: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(resolved).as_posix()
        _reject_sensitive_path(relative)
        files.append(
            CapsuleFile(
                path=relative,
                bytes=path.stat().st_size,
                sha256=_sha256_file(path),
            )
        )
    return CapsuleManifest(
        identity=identity,
        logical_roots=dict(sorted(logical_roots.items())),
        source_roots=dict(sorted(source_roots.items())),
        files=tuple(files),
    )


def verify_capsule(root: Path) -> CapsuleManifest:
    resolved = _resolve_capsule_root(root)
    manifest_path = resolved / "capsule.json"
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        manifest = CapsuleManifest.model_validate(raw)
    except (OSError, ValueError) as exc:
        raise CapsuleIntegrityError("capsule manifest is invalid") from exc
    verify_capsule_contents(resolved, manifest)
    return manifest


def verify_capsule_contents(root: Path, manifest: CapsuleManifest) -> None:
    """Verify capsule files against an already trusted manifest.

    Materialization uses this after copying so its integrity decision remains
    anchored to the manifest verified in the immutable source capsule.
    """

    resolved = _resolve_capsule_root(root)
    manifest_path = resolved / "capsule.json"
    expected = {entry.path: entry for entry in manifest.files}
    observed: set[str] = set()
    for path in sorted(resolved.rglob("*")):
        if path == manifest_path:
            continue
        if path.is_symlink():
            raise CapsuleIntegrityError(f"capsule contains a symbolic link: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(resolved).as_posix()
        _reject_sensitive_path(relative)
        entry = expected.get(relative)
        if entry is None:
            raise CapsuleIntegrityError(f"capsule contains an unmanifested file: {relative}")
        if path.stat().st_size != entry.bytes:
            raise CapsuleIntegrityError(f"capsule byte count changed: {relative}")
        if _sha256_file(path) != entry.sha256:
            raise CapsuleIntegrityError(f"capsule SHA-256 changed: {relative}")
        observed.add(relative)
    missing = sorted(set(expected) - observed)
    if missing:
        raise CapsuleIntegrityError("capsule files are missing: " + ", ".join(missing))


def write_capsule_manifest(root: Path, manifest: CapsuleManifest) -> Path:
    path = root / "capsule.json"
    payload = json.dumps(
        manifest.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    path.write_text(payload + "\n", encoding="utf-8")
    return path


def write_deterministic_capsule_zip(root: Path, archive: Path) -> Path:
    """Package a verified capsule with normalized ZIP metadata.

    This is intentionally not ``Compress-Archive``: its directory entries and
    timestamps make byte equality dependent on the host/run time.
    """
    if archive.exists():
        raise CapsuleIntegrityError("capsule archive destination already exists")
    manifest = verify_capsule(root)
    expected = ("capsule.json", *(entry.path for entry in manifest.files))
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as output:
        for relative in expected:
            data = (root / relative).read_bytes()
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100400 << 16
            output.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    verify_capsule_zip(archive)
    return archive


def verify_capsule_zip(archive: Path) -> CapsuleManifest:
    """Reopen and bind ZIP CRC, members, byte counts and digests to manifest."""
    try:
        with zipfile.ZipFile(archive, "r") as source:
            if source.testzip() is not None:
                raise CapsuleIntegrityError("capsule archive CRC verification failed")
            infos = source.infolist()
            names = tuple(info.filename for info in infos)
            if len(names) != len(set(names)) or any(info.is_dir() for info in infos):
                raise CapsuleIntegrityError("capsule archive member set is invalid")
            raw_manifest = source.read("capsule.json")
            manifest = CapsuleManifest.model_validate(json.loads(raw_manifest.decode("utf-8-sig")))
            expected = ("capsule.json", *(entry.path for entry in manifest.files))
            if names != expected:
                raise CapsuleIntegrityError("capsule archive member set does not match manifest")
            entries = {entry.path: entry for entry in manifest.files}
            for info in infos:
                data = source.read(info)
                if info.CRC != (zlib.crc32(data) & 0xFFFFFFFF):
                    raise CapsuleIntegrityError(f"capsule archive CRC changed: {info.filename}")
                if info.filename == "capsule.json":
                    continue
                entry = entries[info.filename]
                if info.file_size != entry.bytes or len(data) != entry.bytes:
                    raise CapsuleIntegrityError(
                        f"capsule archive byte count changed: {info.filename}"
                    )
                if hashlib.sha256(data).hexdigest() != entry.sha256:
                    raise CapsuleIntegrityError(f"capsule archive SHA-256 changed: {info.filename}")
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        if isinstance(exc, CapsuleIntegrityError):
            raise
        raise CapsuleIntegrityError("capsule archive is invalid") from exc
    return manifest


def _reject_sensitive_path(relative: str) -> None:
    names = [part.casefold() for part in PurePosixPath(relative).parts]
    if any(_sensitive_filename(name) for name in names) or any(
        _SENSITIVE_COMPONENT.search(name) for name in names
    ):
        raise CapsuleIntegrityError(f"capsule contains forbidden sensitive material: {relative}")


def _sensitive_filename(name: str) -> bool:
    """Reject credential stores and private-key conventions before export.

    This policy is deliberately filename based: a rehearsal capsule is never
    an authorized secret transport, including when the file's contents happen
    to be harmless in a particular test fixture.
    """
    return (
        name in _FORBIDDEN_NAMES
        or name.startswith(".env.")
        or name.startswith(("credentials", "service-account", "service_account", "oauth", "auth"))
        or name.startswith(("id_rsa.", "id_dsa.", "id_ed25519."))
        or name.endswith((".key", ".pem", ".p12", ".pfx", ".ppk"))
        or "private-key" in name
        or "private_key" in name
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def make_capsule_read_only(root: Path) -> None:
    if root.is_symlink():
        raise CapsuleIntegrityError("capsule root cannot be a symbolic link")
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_symlink():
            raise CapsuleIntegrityError("capsule cannot contain symbolic links")
        mode = 0o500 if path.is_dir() else 0o400
        os.chmod(path, mode)
    os.chmod(root, 0o500)


def _resolve_capsule_root(root: Path) -> Path:
    if root.is_symlink():
        raise CapsuleIntegrityError("capsule root cannot be a symbolic link")
    return root.resolve()
