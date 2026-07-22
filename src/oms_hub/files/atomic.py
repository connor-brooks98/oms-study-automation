import hashlib
import os
import shutil
import uuid
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verified_atomic_copy(source: Path, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.partial-{uuid.uuid4().hex}")
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
