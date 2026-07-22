from pathlib import Path

import msoffcrypto  # type: ignore[import-untyped]


class OfficeSecurityError(ValueError):
    pass


def office_file_is_encrypted(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            return bool(msoffcrypto.OfficeFile(stream).is_encrypted())
    except Exception as error:
        raise OfficeSecurityError("Office file encryption status could not be verified") from error
