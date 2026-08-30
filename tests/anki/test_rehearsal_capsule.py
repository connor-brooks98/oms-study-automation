import json
import os
import sqlite3
import stat
from contextlib import closing
from hashlib import sha256
from pathlib import Path

import pytest

from oms_hub.anki.rehearsal.capsule import (
    CapsuleIdentity,
    CapsuleIntegrityError,
    build_capsule_manifest,
    make_capsule_read_only,
    verify_capsule,
    verify_capsule_zip,
    write_deterministic_capsule_zip,
)
from oms_hub.anki.rehearsal.materialize import (
    _PATH_COLUMNS,
    _is_materialized_logical_path,
    materialize_capsule,
)
from oms_hub.migrations import LATEST_SCHEMA_VERSION


@pytest.fixture(autouse=True)
def _restore_tmp_path_permissions(tmp_path: Path) -> None:
    """Keep deliberately read-only capsule fixtures removable on Windows."""
    yield
    try:
        for path in sorted(tmp_path.rglob("*"), reverse=True):
            mode = stat.S_IMODE(path.stat().st_mode) | stat.S_IWUSR
            if path.is_dir():
                mode |= stat.S_IRUSR | stat.S_IXUSR
            os.chmod(path, mode)
    except OSError:
        pass
    try:
        os.chmod(tmp_path, stat.S_IMODE(tmp_path.stat().st_mode) | stat.S_IWUSR | stat.S_IXUSR)
    except OSError:
        pass


def _write_minimal_database(path: Path, source: Path, *, schema: int = 25) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE schema_version (id INTEGER PRIMARY KEY, version INTEGER)")
        connection.execute("INSERT INTO schema_version VALUES (1, ?)", (schema,))
        connection.execute(
            "CREATE TABLE study_revisions ("
            "id INTEGER PRIMARY KEY, immutable_source_path TEXT, immutable_derived_path TEXT, "
            "canonical_source_path TEXT, canonical_derived_path TEXT, icloud_path TEXT)"
        )
        connection.execute(
            "INSERT INTO study_revisions (id, immutable_source_path) VALUES (1, ?)",
            (str(source),),
        )


def _capsule(tmp_path: Path, *, database_schema: int = 25) -> Path:
    root = tmp_path / "capsule"
    source = root / "roots" / "study" / "lecture.txt"
    source.parent.mkdir(parents=True)
    source.write_text("frozen lecture", encoding="utf-8")
    database = root / "hub" / "hub.db"
    database.parent.mkdir(parents=True)
    _write_minimal_database(
        database, Path(r"C:\Study\lecture.txt"), schema=database_schema
    )
    _refresh_manifest(root, database_schema=database_schema)
    return root


def _refresh_manifest(
    root: Path,
    *,
    database_schema: int = 25,
    source_root: str = r"C:\Study",
) -> None:
    manifest = build_capsule_manifest(
        root,
        identity=CapsuleIdentity(
            commit_sha="1" * 40,
            tree_sha="2" * 40,
            database_schema=database_schema,
            companion_generation="companion-1",
            semantic_generation="semantic-1",
            companion_note_count=28_258,
            semantic_note_count=28_257,
        ),
        logical_roots={"study": "roots/study"},
        source_roots={"study": source_root},
    )
    (root / "capsule.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )


def test_manifest_is_self_excluding_and_detects_tampering(tmp_path: Path) -> None:
    root = _capsule(tmp_path)
    verified = verify_capsule(root)
    assert "capsule.json" not in {entry.path for entry in verified.files}
    assert verified.identity.companion_note_count == 28_258
    (root / "roots" / "study" / "lecture.txt").write_text("tampered", encoding="utf-8")
    with pytest.raises(CapsuleIntegrityError, match="changed"):
        verify_capsule(root)


def test_deterministic_capsule_zip_reopens_and_is_byte_identical(tmp_path: Path) -> None:
    root = _capsule(tmp_path)
    first = write_deterministic_capsule_zip(root, tmp_path / "first.zip")
    second = write_deterministic_capsule_zip(root, tmp_path / "second.zip")
    assert first.read_bytes() == second.read_bytes()
    assert verify_capsule_zip(first).identity.commit_sha == "1" * 40


def _source_snapshot(root: Path) -> tuple[dict[str, str], dict[str, int]]:
    hashes = {
        path.relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }
    modes = {
        ".": stat.S_IMODE(root.stat().st_mode),
        **{
            path.relative_to(root).as_posix(): stat.S_IMODE(path.stat().st_mode)
            for path in root.rglob("*")
        },
    }
    return hashes, modes


def test_materializer_rebases_only_registered_paths_and_preserves_read_only_capsule(
    tmp_path: Path,
) -> None:
    root = _capsule(tmp_path)
    with closing(sqlite3.connect(root / "hub" / "hub.db")) as connection:
        assert connection.execute(
            "SELECT immutable_source_path FROM study_revisions WHERE id = 1"
        ).fetchone() == (r"C:\Study\lecture.txt",)
    make_capsule_read_only(root)
    before_hashes, before_modes = _source_snapshot(root)
    overlay = materialize_capsule(root, tmp_path / "overlay")
    with closing(sqlite3.connect(overlay.database_path)) as connection, connection:
        value = connection.execute(
            "SELECT immutable_source_path FROM study_revisions WHERE id = 1"
        ).fetchone()
    assert value == (str(overlay.root / "roots" / "study" / "lecture.txt"),)
    assert overlay.path_audit
    assert os.stat(overlay.database_path).st_mode & stat.S_IWUSR
    (overlay.root / "rehearsal" / "overlay-writable.txt").write_text("ok", encoding="utf-8")
    assert _source_snapshot(root) == (before_hashes, before_modes)
    with closing(sqlite3.connect(root / "hub" / "hub.db")) as connection:
        assert connection.execute(
            "SELECT immutable_source_path FROM study_revisions WHERE id = 1"
        ).fetchone() == (r"C:\Study\lecture.txt",)
    with pytest.raises(CapsuleIntegrityError, match="already exists"):
        materialize_capsule(root, overlay.root)


def test_materializer_path_registry_tracks_and_supports_current_schema(tmp_path: Path) -> None:
    assert set(_PATH_COLUMNS) == set(range(25, LATEST_SCHEMA_VERSION + 1))
    assert _PATH_COLUMNS[27] == _PATH_COLUMNS[26]
    assert _PATH_COLUMNS[28] == _PATH_COLUMNS[27]
    assert _PATH_COLUMNS[29] == _PATH_COLUMNS[28]
    assert _PATH_COLUMNS[30] == _PATH_COLUMNS[29]
    root = _capsule(tmp_path, database_schema=LATEST_SCHEMA_VERSION)
    overlay = materialize_capsule(root, tmp_path / "overlay")
    assert overlay.path_audit


def test_materialized_windows_overlay_path_is_not_rejected_as_source_residue(
    tmp_path: Path,
) -> None:
    logical_root = Path(r"C:\overlay\roots\study")
    assert _is_materialized_logical_path(
        r"C:\overlay\roots\study\lecture.txt", {"study": logical_root}
    )
    assert not _is_materialized_logical_path(
        r"C:\Study\lecture.txt", {"study": logical_root}
    )


@pytest.mark.parametrize(
    ("source_root", "source_path"),
    (
        (r"C:\Study", "C:/Study/lecture.txt"),
        (r"\\server\share", r"\\server\share\lecture.txt"),
    ),
)
def test_materializer_rebases_supported_windows_absolute_path_forms(
    tmp_path: Path, source_root: str, source_path: str
) -> None:
    root = _capsule(tmp_path)
    database = root / "hub" / "hub.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE study_revisions SET immutable_source_path = ?", (source_path,))
    _refresh_manifest(root, source_root=source_root)

    overlay = materialize_capsule(root, tmp_path / "overlay")
    with closing(sqlite3.connect(overlay.database_path)) as connection:
        assert connection.execute(
            "SELECT immutable_source_path FROM study_revisions WHERE id = 1"
        ).fetchone() == (str(overlay.root / "roots" / "study" / "lecture.txt"),)


def test_materializer_rejects_unknown_future_schema(tmp_path: Path) -> None:
    future_schema = LATEST_SCHEMA_VERSION + 1
    root = _capsule(tmp_path, database_schema=future_schema)
    with pytest.raises(
        CapsuleIntegrityError,
        match=f"unsupported path registry for database schema {future_schema}",
    ):
        materialize_capsule(root, tmp_path / "overlay")


def test_materializer_rejects_unknown_windows_absolute_path(tmp_path: Path) -> None:
    root = _capsule(tmp_path)
    database = root / "hub" / "hub.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE unknown_paths (id INTEGER, path TEXT)")
        connection.execute("INSERT INTO unknown_paths VALUES (1, ?)", (r"D:\escape.txt",))
    # Rebuild after the deliberate source change so the unknown-path check, not
    # the integrity check, is the asserted failure.
    _refresh_manifest(root)
    with pytest.raises(CapsuleIntegrityError, match="unregistered Windows path"):
        materialize_capsule(root, tmp_path / "overlay")
    assert not (tmp_path / "overlay").exists()


@pytest.mark.parametrize("value", ("C:/escape.txt", r"\\server\share\escape.txt"))
def test_materializer_rejects_unregistered_supported_windows_absolute_path_forms(
    tmp_path: Path, value: str
) -> None:
    root = _capsule(tmp_path)
    database = root / "hub" / "hub.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE unknown_paths (id INTEGER, path TEXT)")
        connection.execute("INSERT INTO unknown_paths VALUES (1, ?)", (value,))
    _refresh_manifest(root)

    with pytest.raises(CapsuleIntegrityError, match="unregistered Windows path"):
        materialize_capsule(root, tmp_path / "overlay")
    assert not (tmp_path / "overlay").exists()


def test_materializer_rejects_registered_path_when_its_overlay_target_is_missing(
    tmp_path: Path,
) -> None:
    root = _capsule(tmp_path)
    database = root / "hub" / "hub.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "UPDATE study_revisions SET immutable_source_path = ?", (r"C:\Study\missing.txt",)
        )
    _refresh_manifest(root)

    with pytest.raises(CapsuleIntegrityError, match="materialized path target is unavailable"):
        materialize_capsule(root, tmp_path / "overlay")
    assert not (tmp_path / "overlay").exists()


def test_materializer_keeps_import_audit_path_in_capsule_until_overlay_rewrite(
    tmp_path: Path,
) -> None:
    root = _capsule(tmp_path)
    previous_pdf = root / "roots" / "study" / "previous-immutable.pdf"
    previous_pdf.write_bytes(b"%PDF-previous\n")
    database = root / "hub" / "hub.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "CREATE TABLE existing_artifact_imports ("
            "id INTEGER PRIMARY KEY, canonical_transcript_path TEXT, canonical_outline_path TEXT, "
            "immutable_transcript_path TEXT, immutable_outline_path TEXT, "
            "previous_immutable_pdf_path TEXT, imported_immutable_pdf_path TEXT)"
        )
        connection.execute(
            "INSERT INTO existing_artifact_imports "
            "(id, previous_immutable_pdf_path) VALUES (1, ?)",
            (r"C:\Study\previous-immutable.pdf",),
        )
    _refresh_manifest(root)
    make_capsule_read_only(root)

    overlay = materialize_capsule(root, tmp_path / "overlay")
    with closing(sqlite3.connect(root / "hub" / "hub.db")) as connection:
        assert connection.execute(
            "SELECT previous_immutable_pdf_path FROM existing_artifact_imports WHERE id = 1"
        ).fetchone() == (r"C:\Study\previous-immutable.pdf",)
    with closing(sqlite3.connect(overlay.database_path)) as connection:
        assert connection.execute(
            "SELECT previous_immutable_pdf_path FROM existing_artifact_imports WHERE id = 1"
        ).fetchone() == (str(overlay.root / "roots" / "study" / previous_pdf.name),)


def test_materializer_rejects_registered_table_with_schema_registry_drift(tmp_path: Path) -> None:
    root = _capsule(tmp_path)
    database = root / "hub" / "hub.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "CREATE TABLE existing_artifact_imports "
            "(id INTEGER PRIMARY KEY, previous_immutable_pdf_path TEXT)"
        )
    _refresh_manifest(root)

    with pytest.raises(CapsuleIntegrityError, match="path registry/schema drift"):
        materialize_capsule(root, tmp_path / "overlay")
    assert not (tmp_path / "overlay").exists()


def test_materializer_rejects_non_string_registered_path_value(tmp_path: Path) -> None:
    root = _capsule(tmp_path)
    database = root / "hub" / "hub.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE study_revisions SET immutable_source_path = ?", (b"not-a-path",))
    _refresh_manifest(root)

    with pytest.raises(CapsuleIntegrityError, match="registered path has a non-string value"):
        materialize_capsule(root, tmp_path / "overlay")
    assert not (tmp_path / "overlay").exists()


def test_materializer_rejects_foreign_key_failure_and_removes_overlay(
    tmp_path: Path,
) -> None:
    root = _capsule(tmp_path)
    database = root / "hub" / "hub.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE parent_records (id INTEGER PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE child_records (id INTEGER PRIMARY KEY, parent_id INTEGER "
            "REFERENCES parent_records(id))"
        )
        connection.execute("INSERT INTO child_records VALUES (1, 999)")
    _refresh_manifest(root)

    with pytest.raises(CapsuleIntegrityError, match="foreign key check failed"):
        materialize_capsule(root, tmp_path / "overlay")
    assert not (tmp_path / "overlay").exists()


def test_materializer_rejects_a_symbolic_link_capsule_root(tmp_path: Path) -> None:
    root = _capsule(tmp_path)
    link = tmp_path / "capsule-link"
    try:
        link.symlink_to(root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")
    with pytest.raises(CapsuleIntegrityError, match="root cannot be a symbolic link"):
        materialize_capsule(link, tmp_path / "overlay")


@pytest.mark.parametrize(
    "relative",
    (
        "sources/API_KEY.json",
        "sources/api-key.txt",
        "sources/apikey.txt",
        "artifacts/secret.txt",
        "source-index/PASSWD.txt",
        "replay/.env.production",
        "replay/session-cookie.json",
        "replay/token.txt",
        "replay/credentials.json",
        "replay/service-account-prod.json",
        "replay/oauth-client.json",
        "replay/auth-cache.sqlite",
        "replay/id_rsa",
        "replay/id_ed25519.pub",
        "replay/private_key.pem",
        "replay/server.key",
        "replay/client.p12",
        "replay/client.pfx",
        "replay/Login Data",
        "replay/cookies.db",
    ),
)
def test_capsule_rejects_sensitive_filename_across_exported_roots(
    tmp_path: Path, relative: str
) -> None:
    root = _capsule(tmp_path)
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not a secret", encoding="utf-8")
    with pytest.raises(CapsuleIntegrityError, match="forbidden sensitive"):
        build_capsule_manifest(
            root,
            identity=CapsuleIdentity(
                commit_sha="1" * 40,
                tree_sha="2" * 40,
                database_schema=1,
                companion_generation="companion",
                semantic_generation="semantic",
                companion_note_count=1,
                semantic_note_count=1,
            ),
            logical_roots={"repository": "sources/repository"},
            source_roots={"repository": "C:/repository"},
        )


def test_capsule_sensitive_filename_policy_does_not_reject_tokenizer_source(tmp_path: Path) -> None:
    root = _capsule(tmp_path)
    path = root / "sources/repository/tokenizer.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("pass\n", encoding="utf-8")
    assert build_capsule_manifest(
        root,
        identity=CapsuleIdentity(
            commit_sha="1" * 40,
            tree_sha="2" * 40,
            database_schema=1,
            companion_generation="companion",
            semantic_generation="semantic",
            companion_note_count=1,
            semantic_note_count=1,
        ),
        logical_roots={"repository": "sources/repository"},
        source_roots={"repository": "C:/repository"},
    )
