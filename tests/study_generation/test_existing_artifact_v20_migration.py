# ruff: noqa: E501

import hashlib

import pytest
from sqlalchemy import inspect, text

from oms_hub.config import Settings
from oms_hub.db import Database
from oms_hub.existing_artifact_import import ExistingArtifactImporter, ExistingArtifactImportRequest
from oms_hub.models import (
    GenerationJobModel,
    OutlineOutputModel,
    StudyRevisionModel,
    UploadBatchModel,
    UploadItemModel,
)
from oms_hub.repositories import CatalogRepository, LectureInput
from oms_hub.study_generation.outline import OutlinePdfRenderer


def test_v19_outline_rebuild_preserves_generated_row_and_is_repeatable(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    lecture_id = CatalogRepository(database).upsert_lecture(
        LectureInput("Neuro", 1, 1, "Topic", "", None)
    )
    with database.session() as session:
        session.add(
            GenerationJobModel(
                id="generated-job",
                lecture_id=lecture_id,
                kind="outline",
                state="complete",
                stage="complete",
                attempts=1,
            )
        )
    with database.engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=OFF"))
        connection.execute(text("DROP TABLE outline_outputs"))
        connection.execute(
            text("""
            CREATE TABLE outline_outputs (
              id INTEGER PRIMARY KEY, lecture_id INTEGER NOT NULL REFERENCES lectures(id),
              job_id VARCHAR(36) NOT NULL UNIQUE REFERENCES generation_jobs(id),
              path TEXT NOT NULL, sha256 VARCHAR(64) NOT NULL,
              current BOOLEAN NOT NULL DEFAULT 1, created_at VARCHAR(40) NOT NULL
            )
        """)
        )
        connection.execute(
            text("""
            INSERT INTO outline_outputs VALUES
            (7, :lecture, 'generated-job', '/outline.pdf', :digest, 1, '2026-01-01')
        """),
            {"lecture": lecture_id, "digest": "a" * 64},
        )
        connection.execute(text("UPDATE schema_version SET version=19 WHERE id=1"))
        connection.execute(text("PRAGMA foreign_keys=ON"))
    database.migrate()
    database.migrate()
    with database.session() as session:
        row = session.get(OutlineOutputModel, 7)
        assert row is not None
        assert row.job_id == "generated-job"
        assert row.provenance_kind == "notebooklm_generated"
        assert row.current is True
    inspector = inspect(database.engine)
    assert any(
        item["name"] == "uq_outline_outputs_current_lecture"
        for item in inspector.get_indexes("outline_outputs")
    )
    with database.engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []


def test_v19_outline_rebuild_does_not_retarget_new_audit_outline_fk(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    with database.engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.execute(text("DROP TABLE existing_artifact_imports"))
        connection.execute(text("DROP TABLE outline_outputs"))
        connection.execute(text("""
            CREATE TABLE outline_outputs (
              id INTEGER PRIMARY KEY, lecture_id INTEGER NOT NULL REFERENCES lectures(id),
              job_id VARCHAR(36) NOT NULL UNIQUE REFERENCES generation_jobs(id),
              path TEXT NOT NULL, sha256 VARCHAR(64) NOT NULL,
              current BOOLEAN NOT NULL DEFAULT 1, created_at VARCHAR(40) NOT NULL
            )
        """))
        connection.execute(text("UPDATE schema_version SET version=19 WHERE id=1"))
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    database.migrate()
    with database.engine.connect() as connection:
        audit_fks = {
            row[3]: row[2]
            for row in connection.execute(text("PRAGMA foreign_key_list(existing_artifact_imports)"))
        }
        assert audit_fks["outline_id"] == "outline_outputs"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []

    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        study_root=tmp_path / "study",
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
    )
    lecture_id = CatalogRepository(database).upsert_lecture(
        LectureInput("Neuro", 1, 1, "Topic", "", None)
    )
    source = settings.data_dir / "artifacts" / "v2" / "slides" / "source.pptx"
    derived = settings.data_dir / "artifacts" / "v2" / "slides" / "source.pdf"
    canonical_source = settings.study_root / "Neuro" / "source.pptx"
    canonical_derived = settings.study_root / "Neuro" / "source.pdf"
    for path, payload in ((source, b"pptx"), (derived, b"pdf")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    canonical_source.parent.mkdir(parents=True, exist_ok=True)
    canonical_source.write_bytes(source.read_bytes())
    canonical_derived.write_bytes(derived.read_bytes())
    source_sha, pdf_sha = (hashlib.sha256(path.read_bytes()).hexdigest() for path in (source, derived))
    with database.session() as session:
        session.add(UploadBatchModel(id="slide-batch-v19", kind="slides", state="complete"))
        session.add(UploadItemModel(id="slide-item-v19", batch_id="slide-batch-v19", kind="slides", original_filename="source.pptx", staged_path=str(source), sha256=source_sha, size_bytes=4, state="complete", lecture_id=lecture_id, confidence=1, manual_assignment=True))
        session.flush()
        slide = StudyRevisionModel(upload_item_id="slide-item-v19", lecture_id=lecture_id, kind="slides", source_sha256=source_sha, immutable_source_path=str(source), derived_sha256=pdf_sha, immutable_derived_path=str(derived), canonical_source_path=str(canonical_source), canonical_derived_path=str(canonical_derived), state="current", current=True)
        session.add(slide)
        session.flush()
        slide_id = slide.id
    transcript = tmp_path / "cleaned.txt"
    transcript.write_text("clean transcript", encoding="utf-8")
    outline = tmp_path / "outline.pdf"
    outline.write_bytes(OutlinePdfRenderer().render("Outline", "# CORE CONCEPTS\n- One\n# DEPTH MAP\n- Two\n# PROFESSOR EMPHASIS FLAGS\n- Three"))
    result = ExistingArtifactImporter(database, settings).import_artifacts(
        ExistingArtifactImportRequest(lecture_id, slide_id, source_sha, pdf_sha, transcript, hashlib.sha256(transcript.read_bytes()).hexdigest(), outline, hashlib.sha256(outline.read_bytes()).hexdigest())
    )
    assert result.status == "complete"
    with database.engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
        assert connection.execute(text("SELECT COUNT(*) FROM existing_artifact_imports WHERE outline_id=:id"), {"id": result.outline_id}).scalar_one() == 1


def _create_true_v20_import_fixture(
    database: Database, *, dangling: bool = False, ambiguous: bool = False
) -> int:
    """Install only the v20 shapes for the three import-owned tables.

    This is intentionally not a v21 database with its version number edited:
    ``slide_source_sha256`` and ``slide_pdf_sha256`` do not exist until the
    migration under test adds them.
    """
    lecture_id = CatalogRepository(database).upsert_lecture(LectureInput("N", 1, 1, "T", "", None))
    source = "b1c7abc3fb5d86476a3477d397e679ec42e61cff982fcec9dcb55a9d0a9c5469"
    pdf = "8bb427c3265f3a97997fd870f42794d59bd4850f963ccf292f3f9160ea9e0d38"
    transcript = "d" * 64
    outline = "47a55e7cdfb6ddf4bc240626f48233392fd016fd7cc9acb96e331a820b7053ea"
    with database.engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.execute(text("DROP TABLE existing_artifact_imports"))
        connection.execute(text("DROP TABLE outline_outputs"))
        connection.execute(text("DROP TABLE study_revisions"))
        connection.execute(
            text("""
            CREATE TABLE study_revisions (
                id INTEGER PRIMARY KEY, upload_item_id VARCHAR(36) NOT NULL UNIQUE,
                lecture_id INTEGER NOT NULL REFERENCES lectures(id), kind VARCHAR(20) NOT NULL,
                source_sha256 VARCHAR(64) NOT NULL, immutable_source_path TEXT NOT NULL,
                derived_sha256 VARCHAR(64), immutable_derived_path TEXT,
                canonical_source_path TEXT, canonical_derived_path TEXT, icloud_path TEXT,
                prompt_sha256 VARCHAR(64), provenance_kind VARCHAR(40) NOT NULL DEFAULT 'llm_cleaned',
                import_id VARCHAR(36), state VARCHAR(30) NOT NULL DEFAULT 'proposed',
                current BOOLEAN NOT NULL DEFAULT 0, created_at VARCHAR(40) NOT NULL,
                promoted_at VARCHAR(40), UNIQUE(lecture_id, kind, source_sha256)
            )
        """)
        )
        connection.execute(
            text("""
            CREATE TABLE existing_artifact_imports (
                id VARCHAR(36) PRIMARY KEY, bundle_sha256 VARCHAR(64) NOT NULL UNIQUE,
                lecture_id INTEGER NOT NULL REFERENCES lectures(id),
                slide_revision_id INTEGER NOT NULL REFERENCES study_revisions(id) ON DELETE RESTRICT,
                slide_sha256 VARCHAR(64), transcript_sha256 VARCHAR(64) NOT NULL,
                outline_sha256 VARCHAR(64) NOT NULL, subject VARCHAR(200) NOT NULL DEFAULT '',
                exam_number INTEGER NOT NULL DEFAULT 0, lecture_number INTEGER NOT NULL DEFAULT 0,
                topic VARCHAR(500) NOT NULL DEFAULT '', canonical_transcript_path TEXT,
                canonical_outline_path TEXT, immutable_transcript_path TEXT,
                immutable_outline_path TEXT, transcript_filename VARCHAR(500),
                outline_filename VARCHAR(500), status VARCHAR(20) NOT NULL DEFAULT 'preparing',
                attempts INTEGER NOT NULL DEFAULT 1, transcript_revision_id INTEGER REFERENCES study_revisions(id) ON DELETE RESTRICT,
                outline_id INTEGER REFERENCES outline_outputs(id) ON DELETE RESTRICT,
                error TEXT, owner VARCHAR(100), created_at VARCHAR(40) NOT NULL, updated_at VARCHAR(40) NOT NULL
            )
        """)
        )
        connection.execute(
            text("""
            CREATE TABLE outline_outputs (
                id INTEGER PRIMARY KEY, lecture_id INTEGER NOT NULL REFERENCES lectures(id),
                job_id VARCHAR(36) REFERENCES generation_jobs(id) UNIQUE, path TEXT NOT NULL,
                sha256 VARCHAR(64) NOT NULL, current BOOLEAN NOT NULL DEFAULT 1,
                created_at VARCHAR(40) NOT NULL, provenance_kind VARCHAR(40) NOT NULL DEFAULT 'notebooklm_generated',
                original_filename VARCHAR(500), immutable_path TEXT,
                slide_revision_id INTEGER REFERENCES study_revisions(id) ON DELETE RESTRICT,
                slide_sha256 VARCHAR(64), transcript_revision_id INTEGER REFERENCES study_revisions(id) ON DELETE RESTRICT,
                transcript_sha256 VARCHAR(64), import_id VARCHAR(36) REFERENCES existing_artifact_imports(id) ON DELETE RESTRICT
            )
        """)
        )
        connection.execute(
            text("""
            INSERT INTO upload_batches (id, kind, state, created_at, updated_at)
            VALUES ('slide-batch', 'slides', 'complete', '2026-08-09T00:00:00Z', '2026-08-09T00:00:00Z'),
                   ('transcript-batch', 'transcripts', 'complete', '2026-08-09T00:00:00Z', '2026-08-09T00:00:00Z')
        """)
        )
        connection.execute(
            text("""
            INSERT INTO upload_items (id, batch_id, kind, original_filename, staged_path, sha256, size_bytes, state, lecture_id, confidence, evidence_json, manual_assignment, created_at, updated_at)
            VALUES ('slide-item', 'slide-batch', 'slides', 'source.pptx', '/staged/source.pptx', :source, 42, 'complete', :lecture, 1, '[]', 1, '2026-08-09T00:00:00Z', '2026-08-09T00:00:00Z'),
                   ('transcript-item', 'transcript-batch', 'transcripts', 'cleaned.txt', '/imports/import-1/cleaned.txt', :transcript, 24, 'complete', :lecture, 1, '[]', 1, '2026-08-09T00:00:00Z', '2026-08-09T00:00:00Z')
        """),
            {"source": source, "transcript": transcript, "lecture": lecture_id},
        )
        connection.execute(
            text("""
            INSERT INTO study_revisions VALUES
            (7, 'slide-item', :lecture, 'slides', :source, '/immutable/source.pptx', :pdf, '/immutable/source.pdf', '/current/source.pptx', '/current/source.pdf', '/icloud/source.pdf', NULL, 'llm_cleaned', NULL, 'current', 1, '2026-08-09T00:00:00Z', '2026-08-09T00:01:00Z'),
            (8, 'transcript-item', :lecture, 'transcripts', :transcript, '/imports/import-1/cleaned.txt', :transcript, '/imports/import-1/cleaned.txt', '/current/cleaned.txt', '/current/cleaned.txt', NULL, NULL, 'imported_cleaned', 'import-1', 'current', 1, '2026-08-09T00:00:00Z', '2026-08-09T00:01:00Z')
        """),
            {"lecture": lecture_id, "source": source, "pdf": pdf, "transcript": transcript},
        )
        connection.execute(
            text("""
            INSERT INTO existing_artifact_imports
            (id, bundle_sha256, lecture_id, slide_revision_id, slide_sha256, transcript_sha256, outline_sha256, subject, exam_number, lecture_number, topic, canonical_transcript_path, canonical_outline_path, immutable_transcript_path, immutable_outline_path, transcript_filename, outline_filename, status, attempts, transcript_revision_id, outline_id, created_at, updated_at)
            VALUES ('import-1', :bundle, :lecture, :slide_id, :pdf, :transcript, :outline, 'N', 1, 1, 'T', '/current/cleaned.txt', '/current/outline.pdf', '/imports/import-1/cleaned.txt', '/imports/import-1/outline.pdf', 'cleaned.txt', 'outline.pdf', 'complete', 1, 8, 9, '2026-08-09T00:00:00Z', '2026-08-09T00:01:00Z')
        """),
            {
                "bundle": "c" * 64,
                "lecture": lecture_id,
                "slide_id": 999 if dangling else 7,
                "pdf": pdf,
                "transcript": transcript,
                "outline": outline,
            },
        )
        connection.execute(
            text("""
            INSERT INTO outline_outputs VALUES
            (9, :lecture, NULL, '/current/outline.pdf', :outline, 1, '2026-08-09T00:01:00Z', 'imported_notebooklm', 'outline.pdf', '/imports/import-1/outline.pdf', 7, :pdf, 8, :transcript, 'import-1')
        """),
            {"lecture": lecture_id, "pdf": pdf, "outline": outline, "transcript": transcript},
        )
        if ambiguous:
            connection.execute(
                text("""
                INSERT INTO upload_items (id, batch_id, kind, original_filename, staged_path, sha256, size_bytes, state, lecture_id, confidence, evidence_json, manual_assignment, created_at, updated_at)
                VALUES ('other-slide-item', 'slide-batch', 'slides', 'other.pptx', '/staged/other.pptx', :other, 43, 'complete', :lecture, 1, '[]', 1, '2026-08-09T00:00:00Z', '2026-08-09T00:00:00Z')
            """),
                {"other": "a" * 64, "lecture": lecture_id},
            )
            connection.execute(
                text("""
                INSERT INTO study_revisions VALUES
                (10, 'other-slide-item', :lecture, 'slides', :other, '/immutable/other.pptx', :pdf, '/immutable/other.pdf', '/current/other.pptx', '/current/other.pdf', '/icloud/other.pdf', NULL, 'llm_cleaned', NULL, 'current', 1, '2026-08-09T00:00:00Z', '2026-08-09T00:01:00Z')
            """),
                {"lecture": lecture_id, "other": "a" * 64, "pdf": pdf},
            )
        connection.execute(text("UPDATE schema_version SET version=20 WHERE id=1"))
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    return lecture_id


def test_v20_imported_rows_backfill_exact_slide_source_and_pdf_identities(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    lecture_id = _create_true_v20_import_fixture(database)
    source = "b1c7abc3fb5d86476a3477d397e679ec42e61cff982fcec9dcb55a9d0a9c5469"
    pdf = "8bb427c3265f3a97997fd870f42794d59bd4850f963ccf292f3f9160ea9e0d38"
    with database.engine.connect() as connection:
        legacy_import_columns = {
            row[1]
            for row in connection.execute(text("PRAGMA table_info(existing_artifact_imports)"))
        }
        legacy_outline_columns = {
            row[1] for row in connection.execute(text("PRAGMA table_info(outline_outputs)"))
        }
    assert "slide_source_sha256" not in legacy_import_columns
    assert "slide_pdf_sha256" not in legacy_import_columns
    assert "slide_source_sha256" not in legacy_outline_columns
    database.migrate()
    database.migrate()
    with database.engine.connect() as connection:
        row = connection.execute(
            text("""
            SELECT i.slide_source_sha256, i.slide_pdf_sha256, i.subject, i.exam_number,
                   i.lecture_number, i.topic, i.canonical_transcript_path,
                   i.canonical_outline_path, i.immutable_transcript_path,
                   i.immutable_outline_path, i.transcript_filename, i.outline_filename,
                   i.transcript_revision_id, i.outline_id, i.status, i.attempts,
                   t.provenance_kind, t.import_id, t.current, o.provenance_kind,
                   o.import_id, o.slide_revision_id, o.slide_source_sha256,
                   o.slide_sha256, o.transcript_revision_id, o.transcript_sha256, o.current
            FROM existing_artifact_imports i
            JOIN study_revisions t ON t.id=i.transcript_revision_id
            JOIN outline_outputs o ON o.id=i.outline_id WHERE i.id='import-1'
        """)
        ).one()
        assert tuple(row) == (
            source,
            pdf,
            "N",
            1,
            1,
            "T",
            "/current/cleaned.txt",
            "/current/outline.pdf",
            "/imports/import-1/cleaned.txt",
            "/imports/import-1/outline.pdf",
            "cleaned.txt",
            "outline.pdf",
            8,
            9,
            "complete",
            1,
            "imported_cleaned",
            "import-1",
            1,
            "imported_notebooklm",
            "import-1",
            7,
            source,
            pdf,
            8,
            "d" * 64,
            1,
        )
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
        outline_fks = {
            row[3]: (row[2], row[6])
            for row in connection.execute(text("PRAGMA foreign_key_list(outline_outputs)"))
        }
        assert outline_fks["slide_revision_id"] == ("study_revisions", "RESTRICT")
        assert outline_fks["transcript_revision_id"] == ("study_revisions", "RESTRICT")
        assert outline_fks["import_id"] == ("existing_artifact_imports", "RESTRICT")
        import_fks = {
            row[3]: (row[2], row[6])
            for row in connection.execute(
                text("PRAGMA foreign_key_list(existing_artifact_imports)")
            )
        }
        assert import_fks["slide_revision_id"] == ("study_revisions", "RESTRICT")
        assert import_fks["transcript_revision_id"] == ("study_revisions", "RESTRICT")
        assert import_fks["outline_id"] == ("outline_outputs", "RESTRICT")
        assert (
            connection.execute(text("SELECT version FROM schema_version WHERE id=1")).scalar_one()
            == 22
        )
    assert any(
        item["name"] == "uq_study_revisions_current_lecture_kind"
        for item in inspect(database.engine).get_indexes("study_revisions")
    )
    assert any(
        item["name"] == "uq_outline_outputs_current_lecture"
        for item in inspect(database.engine).get_indexes("outline_outputs")
    )
    assert CatalogRepository(database).get_lecture(lecture_id) is not None


def _database_snapshot(database: Database) -> tuple[tuple[object, ...], ...]:
    """Capture schema, indexes, version, and values to prove a rejected gate is read-only."""
    with database.engine.connect() as connection:
        schema = tuple(
            connection.execute(
                text(
                    "SELECT type, name, tbl_name, sql FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
                )
            ).all()
        )
        tables = [row[1] for row in schema if row[0] == "table"]
        values = tuple(
            (table, tuple(connection.execute(text(f"SELECT * FROM {table} ORDER BY rowid")).all()))
            for table in tables
        )
    return schema + values


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE existing_artifact_imports SET transcript_filename='wrong.txt' WHERE id='import-1'",
        "UPDATE existing_artifact_imports SET canonical_outline_path='/wrong/outline.pdf' WHERE id='import-1'",
        "UPDATE existing_artifact_imports SET subject='wrong subject' WHERE id='import-1'",
        "UPDATE upload_items SET staged_path='/wrong/cleaned.txt' WHERE id='transcript-item'",
        "UPDATE outline_outputs SET original_filename='wrong.pdf' WHERE id=9",
        "UPDATE outline_outputs SET immutable_path='/wrong/outline.pdf' WHERE id=9",
    ],
)
def test_raw_v20_invalid_provenance_graph_is_rejected_without_any_mutation(
    tmp_path, statement
):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    _create_true_v20_import_fixture(database)
    with database.engine.begin() as connection:
        connection.execute(text(statement))
    before = _database_snapshot(database)
    with pytest.raises(RuntimeError, match="schema v20 imported artifact graph is invalid"):
        database.migrate()
    assert _database_snapshot(database) == before


def test_raw_v20_imported_outline_cannot_claim_a_generation_job(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    lecture_id = _create_true_v20_import_fixture(database)
    with database.session() as session:
        session.add(
            GenerationJobModel(
                id="otherwise-valid-generation-job",
                lecture_id=lecture_id,
                kind="outline",
                state="complete",
                stage="complete",
                attempts=1,
            )
        )
    with database.engine.begin() as connection:
        connection.execute(
            text("UPDATE outline_outputs SET job_id='otherwise-valid-generation-job' WHERE id=9")
        )
    before = _database_snapshot(database)
    with pytest.raises(RuntimeError, match="schema v20 imported artifact graph is invalid"):
        database.migrate()
    assert _database_snapshot(database) == before


@pytest.mark.parametrize("table", ["upload_items", "outline_outputs"])
def test_raw_v20_missing_required_import_table_is_rejected_without_mutation(tmp_path, table):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    _create_true_v20_import_fixture(database)
    with database.engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.execute(text(f"DROP TABLE {table}"))
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    before = _database_snapshot(database)
    with pytest.raises(RuntimeError, match=f"schema v20 imported artifact required table is missing: {table}"):
        database.migrate()
    assert _database_snapshot(database) == before


def test_current_schema_missing_required_import_table_is_not_repaired(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    with database.engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.execute(text("DROP TABLE outline_outputs"))
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    before = _database_snapshot(database)
    with pytest.raises(
        RuntimeError, match="schema v22 imported artifact required table is missing: outline_outputs"
    ):
        database.migrate()
    assert _database_snapshot(database) == before


@pytest.mark.parametrize(
    ("statement", "verification"),
    [
        (
            "UPDATE existing_artifact_imports SET slide_source_sha256='0' WHERE id='import-1'",
            "SELECT slide_source_sha256 FROM existing_artifact_imports WHERE id='import-1'",
        ),
        (
            "UPDATE existing_artifact_imports SET slide_pdf_sha256='0' WHERE id='import-1'",
            "SELECT slide_pdf_sha256 FROM existing_artifact_imports WHERE id='import-1'",
        ),
        (
            "UPDATE outline_outputs SET slide_source_sha256='0' WHERE id=9",
            "SELECT slide_source_sha256 FROM outline_outputs WHERE id=9",
        ),
        (
            "UPDATE outline_outputs SET slide_sha256='0' WHERE id=9",
            "SELECT slide_sha256 FROM outline_outputs WHERE id=9",
        ),
        (
            "UPDATE study_revisions SET derived_sha256='0' WHERE id=8",
            "SELECT derived_sha256 FROM study_revisions WHERE id=8",
        ),
    ],
)
def test_v21_repeat_migration_validates_without_normalizing_corruption(
    tmp_path, statement, verification
):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    _create_true_v20_import_fixture(database)
    database.migrate()
    with database.engine.begin() as connection:
        connection.execute(text(statement))
    with database.engine.connect() as connection:
        corrupted = connection.execute(text(verification)).scalar_one()
    with pytest.raises(RuntimeError, match="imported artifact graph is invalid"):
        database.migrate()
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT version FROM schema_version WHERE id=1")).scalar_one() == 22
        assert connection.execute(text(verification)).scalar_one() == corrupted


@pytest.mark.parametrize(
    ("fixture", "message"),
    [
        ({"dangling": True}, "imported artifact foreign-key check failed"),
        ({"ambiguous": True}, "cannot add current-artifact uniqueness"),
    ],
)
def test_v21_fails_closed_for_dangling_or_ambiguous_complete_imports(tmp_path, fixture, message):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    _create_true_v20_import_fixture(database, **fixture)
    with pytest.raises(RuntimeError, match=message):
        database.migrate()
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT version FROM schema_version WHERE id=1")).scalar_one() == 20


@pytest.mark.parametrize(
    ("name", "replacement"),
    [
        (
            "uq_study_revisions_current_lecture_kind",
            "CREATE UNIQUE INDEX uq_study_revisions_current_lecture_kind "
            "ON study_revisions(lecture_id, kind) WHERE current = 0",
        ),
        (
            "uq_outline_outputs_current_lecture",
            "CREATE UNIQUE INDEX uq_outline_outputs_current_lecture "
            "ON outline_outputs(lecture_id) WHERE current = 0",
        ),
    ],
)
def test_v21_startup_rejects_missing_or_tampered_current_indexes(tmp_path, name, replacement):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    with database.engine.begin() as connection:
        connection.execute(text(f"DROP INDEX {name}"))
    with pytest.raises(RuntimeError, match=f"current-artifact index is missing or invalid: {name}"):
        database.migrate()
    with database.engine.begin() as connection:
        connection.execute(text(replacement))
    with pytest.raises(RuntimeError, match=f"current-artifact index is missing or invalid: {name}"):
        database.migrate()


def _downgrade_valid_import_to_raw_v21(database: Database) -> None:
    with database.engine.begin() as connection:
        connection.execute(text("DROP TRIGGER trg_outline_replacement_review_insert"))
        connection.execute(text("DROP TRIGGER trg_outline_replacement_review_update"))
        connection.execute(text("DROP TABLE outline_replacement_reviews"))
        connection.execute(text("UPDATE schema_version SET version=21 WHERE id=1"))


def test_raw_v21_validates_read_only_then_creates_v22_review_contract(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    _create_true_v20_import_fixture(database)
    database.migrate()
    _downgrade_valid_import_to_raw_v21(database)

    database.migrate()
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT version FROM schema_version WHERE id=1")).scalar_one() == 22
        assert connection.execute(
            text("SELECT name FROM sqlite_master WHERE type='trigger' AND name="
                 "'trg_outline_replacement_review_insert'")
        ).scalar_one() == "trg_outline_replacement_review_insert"


def test_raw_v21_bad_index_is_rejected_without_mutation(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    _create_true_v20_import_fixture(database)
    database.migrate()
    _downgrade_valid_import_to_raw_v21(database)
    with database.engine.begin() as connection:
        connection.execute(text("DROP INDEX uq_outline_outputs_current_lecture"))
    before = _database_snapshot(database)

    with pytest.raises(RuntimeError, match="current-artifact index is missing or invalid"):
        database.migrate()
    assert _database_snapshot(database) == before


@pytest.mark.parametrize(
    ("statement", "message", "verification"),
    [
        (
            "UPDATE existing_artifact_imports SET slide_sha256='0' WHERE id='import-1'",
            "imported artifact graph is invalid",
            "SELECT slide_sha256 FROM existing_artifact_imports WHERE id='import-1'",
        ),
        (
            "UPDATE outline_outputs SET slide_sha256='0' WHERE id=9",
            "imported artifact graph is invalid",
            "SELECT slide_sha256 FROM outline_outputs WHERE id=9",
        ),
        (
            "UPDATE study_revisions SET derived_sha256='0' WHERE id=8",
            "imported artifact graph is invalid",
            "SELECT derived_sha256 FROM study_revisions WHERE id=8",
        ),
    ],
)
def test_v21_keeps_bad_v20_identities_untouched_and_at_version_20(
    tmp_path, statement, message, verification
):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    _create_true_v20_import_fixture(database)
    with database.engine.begin() as connection:
        connection.execute(text(statement))
    with database.engine.connect() as connection:
        before = connection.execute(text(verification)).scalar_one()
    with pytest.raises(RuntimeError, match=message):
        database.migrate()
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT version FROM schema_version WHERE id=1")).scalar_one() == 20
        assert connection.execute(text(verification)).scalar_one() == before
        # Legacy slide failures happen before the v21 columns are added.
        if "slide_sha256='0'" in statement:
            columns = {row[1] for row in connection.execute(text("PRAGMA table_info(existing_artifact_imports)"))}
            assert "slide_source_sha256" not in columns


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        (
            "UPDATE outline_outputs SET transcript_revision_id=7 WHERE id=9",
            "imported artifact graph is invalid",
        ),
        (
            "UPDATE outline_outputs SET transcript_sha256='0' WHERE id=9",
            "imported artifact graph is invalid",
        ),
    ],
)
def test_v21_fails_closed_when_complete_import_links_are_mismatched(
    tmp_path, statement, message
):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    _create_true_v20_import_fixture(database)
    with database.engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.execute(text(statement))
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    with pytest.raises(RuntimeError, match=message):
        database.migrate()
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT version FROM schema_version WHERE id=1")).scalar_one() == 20
