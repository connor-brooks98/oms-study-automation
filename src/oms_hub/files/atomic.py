import hashlib
import os
import shutil
import uuid
from pathlib import Path

_COPY_TEMPORARY_ATTEMPTS = 16


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_copy_temporary_path(destination: Path) -> Path:
    """Return a short, same-directory candidate for an atomic copy temporary."""
    return destination.parent / f".oms-copy-{uuid.uuid4().hex}.tmp"


def _reserve_atomic_copy_temporary(destination: Path) -> Path:
    for _ in range(_COPY_TEMPORARY_ATTEMPTS):
        temporary = atomic_copy_temporary_path(destination)
        try:
            with temporary.open("xb"):
                pass
        except FileExistsError:
            continue
        return temporary
    raise FileExistsError(
        f"could not reserve an atomic copy temporary for {destination}"
    )


def verified_atomic_copy(source: Path, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = _reserve_atomic_copy_temporary(destination)
    try:
        shutil.copy2(source, temporary)
        source_digest = sha256_file(source)
        if sha256_file(temporary) != source_digest:
            raise OSError("copied file checksum mismatch")
        os.replace(temporary, destination)
        if sha256_file(destination) != source_digest:
            raise OSError("promoted file checksum mismatch")
        return source_digest
    finally:
        temporary.unlink(missing_ok=True)


def verified_atomic_write(payload: bytes, destination: Path) -> str:
    expected_sha256 = hashlib.sha256(payload).hexdigest()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if sha256_file(destination) != expected_sha256:
            raise OSError("immutable destination already contains other data")
        return expected_sha256
    temporary = destination.parent / f".write-{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if sha256_file(temporary) != expected_sha256:
            raise OSError("written file checksum mismatch")
        os.replace(temporary, destination)
        if sha256_file(destination) != expected_sha256:
            raise OSError("promoted file checksum mismatch")
        return expected_sha256
    finally:
        temporary.unlink(missing_ok=True)
