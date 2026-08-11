# ruff: noqa: E501

import hashlib
from pathlib import Path

import pytest
from sqlalchemy import inspect, select, text

from oms_hub.config import Settings
from oms_hub.db import Database
from oms_hub.existing_artifact_import import ExistingArtifactImporter, ExistingArtifactImportRequest
from oms_hub.models import (
    ExistingArtifactImportModel,
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
        connection.execute(text("UPDATE schema_version SET version=19 WHERE id=1"))
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    database.migrate()
    with database.engine.connect() as connection:
        audit_fks = {
            row[3]: row[2]
            for row in connection.execute(
                text("PRAGMA foreign_key_list(existing_artifact_imports)")
            )
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
    source_sha, pdf_sha = (
        hashlib.sha256(path.read_bytes()).hexdigest() for path in (source, derived)
    )
    with database.session() as session:
        session.add(UploadBatchModel(id="slide-batch-v19", kind="slides", state="complete"))
        session.add(
            UploadItemModel(
                id="slide-item-v19",
                batch_id="slide-batch-v19",
                kind="slides",
                original_filename="source.pptx",
                staged_path=str(source),
                sha256=source_sha,
                size_bytes=4,
                state="complete",
                lecture_id=lecture_id,
                confidence=1,
                manual_assignment=True,
            )
        )
        session.flush()
        slide = StudyRevisionModel(
            upload_item_id="slide-item-v19",
            lecture_id=lecture_id,
            kind="slides",
            source_sha256=source_sha,
            immutable_source_path=str(source),
            derived_sha256=pdf_sha,
            immutable_derived_path=str(derived),
            canonical_source_path=str(canonical_source),
            canonical_derived_path=str(canonical_derived),
            state="current",
            current=True,
        )
        session.add(slide)
        session.flush()
        slide_id = slide.id
    transcript = tmp_path / "cleaned.txt"
    transcript.write_text("clean transcript", encoding="utf-8")
    outline = tmp_path / "outline.pdf"
    outline.write_bytes(
        OutlinePdfRenderer().render(
            "Outline",
            "# CORE CONCEPTS\n- One\n# DEPTH MAP\n- Two\n# PROFESSOR EMPHASIS FLAGS\n- Three",
        )
    )
    result = ExistingArtifactImporter(database, settings).import_artifacts(
        ExistingArtifactImportRequest(
            lecture_id,
            slide_id,
            source_sha,
            pdf_sha,
            transcript,
            hashlib.sha256(transcript.read_bytes()).hexdigest(),
            outline,
            hashlib.sha256(outline.read_bytes()).hexdigest(),
        )
    )
    assert result.status == "complete"
    with database.engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM existing_artifact_imports WHERE outline_id=:id"),
                {"id": result.outline_id},
            ).scalar_one()
            == 1
        )


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
    _create_true_v20_import_fixture(database)
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


def test_v22_upgrade_adds_only_null_adoption_fields_to_existing_import_bundle(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    _create_true_v20_import_fixture(database)
    database.migrate()
    with database.engine.begin() as connection:
        connection.execute(text("UPDATE schema_version SET version=22 WHERE id=1"))
    database.migrate()
    database.migrate()
    fields = (
        "expected_current_pdf_sha256",
        "previous_pdf_sha256",
        "previous_immutable_pdf_path",
        "imported_pdf_sha256",
        "imported_immutable_pdf_path",
        "derived_provenance",
        "adoption_operator",
        "adoption_reason",
        "adoption_confirmed_at",
        "recovery_phase",
    )
    with database.engine.connect() as connection:
        row = connection.execute(
            text(f"SELECT {', '.join(fields)} FROM existing_artifact_imports WHERE id='import-1'")
        ).one()
        assert row == (None,) * len(fields)
        assert (
            connection.execute(text("SELECT version FROM schema_version WHERE id=1")).scalar_one()
            == 23
        )


def _current_v23_adoption_fixture(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        study_root=tmp_path / "study",
        icloud_staging_root=tmp_path / "icloud",
        database_url=f"sqlite:///{tmp_path / 'v23.db'}",
    )
    database = Database(settings.database_url)
    database.migrate()
    lecture_id = CatalogRepository(database).upsert_lecture(LectureInput("N", 1, 2, "T", "", None))
    root = settings.data_dir / "artifacts" / "v2" / "slides"
    source, old = root / "source.pptx", root / "old.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"pptx")
    old.write_bytes(OutlinePdfRenderer().render("old", "# CORE CONCEPTS\n- old"))
    canonical_source, canonical_pdf, icloud = (
        settings.study_root / "N" / "source.pptx",
        settings.study_root / "N" / "old.pdf",
        settings.icloud_staging_root / "N" / "icloud.pdf",
    )
    canonical_source.parent.mkdir(parents=True, exist_ok=True)
    icloud.parent.mkdir(parents=True, exist_ok=True)
    canonical_source.write_bytes(source.read_bytes())
    canonical_pdf.write_bytes(old.read_bytes())
    icloud.write_bytes(old.read_bytes())

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    with database.session() as session:
        session.add(UploadBatchModel(id="b", kind="slides", state="complete"))
        session.add(
            UploadItemModel(
                id="i",
                batch_id="b",
                kind="slides",
                original_filename="source.pptx",
                staged_path=str(source),
                sha256=digest(source),
                size_bytes=4,
                state="complete",
                lecture_id=lecture_id,
                confidence=1,
                manual_assignment=True,
            )
        )
        session.flush()
        slide = StudyRevisionModel(
            upload_item_id="i",
            lecture_id=lecture_id,
            kind="slides",
            source_sha256=digest(source),
            immutable_source_path=str(source),
            derived_sha256=digest(old),
            immutable_derived_path=str(old),
            canonical_source_path=str(canonical_source),
            canonical_derived_path=str(canonical_pdf),
            icloud_path=str(icloud),
            state="current",
            current=True,
        )
        session.add(slide)
        session.flush()
        slide_id = slide.id
    transcript, outline, target = (
        tmp_path / "cleaned.txt",
        tmp_path / "outline.pdf",
        tmp_path / "target.pdf",
    )
    transcript.write_text("clean transcript", encoding="utf-8")
    outline.write_bytes(
        OutlinePdfRenderer().render(
            "outline",
            "# CORE CONCEPTS\n- one\n# DEPTH MAP\n- two\n# PROFESSOR EMPHASIS FLAGS\n- three",
        )
    )
    target.write_bytes(OutlinePdfRenderer().render("target", "# CORE CONCEPTS\n- target"))
    result = ExistingArtifactImporter(database, settings).import_artifacts(
        ExistingArtifactImportRequest(
            lecture_id,
            slide_id,
            digest(source),
            digest(target),
            transcript,
            digest(transcript),
            outline,
            digest(outline),
            target,
            digest(old),
            "operator",
            "reason",
            True,
        )
    )
    return database, settings, result


def test_current_v23_completed_adoption_startup_is_idempotent(tmp_path):
    database, _settings, result = _current_v23_adoption_fixture(tmp_path)
    with database.engine.connect() as connection:
        before = connection.execute(text("SELECT * FROM existing_artifact_imports")).all()
        version = connection.execute(
            text("SELECT version FROM schema_version WHERE id=1")
        ).scalar_one()
    database.migrate()
    database.migrate()
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT * FROM existing_artifact_imports")).all() == before
        assert (
            connection.execute(text("SELECT version FROM schema_version WHERE id=1")).scalar_one()
            == version
            == 23
        )
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    assert result.status == "complete"


@pytest.mark.parametrize(
    "artifact",
    [
        "immutable-pptx",
        "canonical-pptx",
        "old-office-pdf",
        "adopted-pdf",
        "canonical-pdf",
        "icloud-pdf",
        "immutable-transcript",
        "canonical-transcript",
        "immutable-outline",
        "canonical-outline",
    ],
)
def test_current_v23_completed_adoption_hashes_every_persisted_artifact_before_mutation(
    tmp_path, artifact
):
    database, _settings, result = _current_v23_adoption_fixture(tmp_path)
    with database.session() as session:
        audit = session.get(ExistingArtifactImportModel, result.import_id)
        slide = session.get(StudyRevisionModel, result.slides_revision_id)
        assert audit is not None and slide is not None
        paths = {
            "immutable-pptx": Path(slide.immutable_source_path),
            "canonical-pptx": Path(slide.canonical_source_path or ""),
            "old-office-pdf": Path(audit.previous_immutable_pdf_path or ""),
            "adopted-pdf": Path(audit.imported_immutable_pdf_path or ""),
            "canonical-pdf": Path(slide.canonical_derived_path or ""),
            "icloud-pdf": Path(slide.icloud_path or ""),
            "immutable-transcript": Path(audit.immutable_transcript_path or ""),
            "canonical-transcript": Path(audit.canonical_transcript_path or ""),
            "immutable-outline": Path(audit.immutable_outline_path or ""),
            "canonical-outline": Path(audit.canonical_outline_path or ""),
        }
    paths[artifact].write_bytes(b"tampered persisted artifact")
    before = _database_snapshot(database)
    with pytest.raises(RuntimeError, match="schema v23 imported-derived adoption files are invalid"):
        database.migrate()
    assert _database_snapshot(database) == before


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("immutable_transcript_path", "same-audit-transcript.txt"),
        ("immutable_outline_path", "nested/outline.pdf"),
    ],
)
def test_current_v23_completed_adoption_rejects_same_byte_repointed_audit_evidence(
    tmp_path, field, replacement
):
    database, _settings, result = _current_v23_adoption_fixture(tmp_path)
    with database.session() as session:
        audit = session.get(ExistingArtifactImportModel, result.import_id)
        transcript = session.get(StudyRevisionModel, result.transcript_revision_id)
        outline = session.get(OutlineOutputModel, result.outline_id)
        assert audit is not None and transcript is not None and outline is not None
        original = Path(getattr(audit, field) or "")
        repointed = original.parent / replacement
        repointed.parent.mkdir(parents=True, exist_ok=True)
        repointed.write_bytes(original.read_bytes())
        setattr(audit, field, str(repointed))
        if field == "immutable_transcript_path":
            transcript.immutable_source_path = str(repointed)
            transcript.immutable_derived_path = str(repointed)
            item = session.get(UploadItemModel, transcript.upload_item_id)
            assert item is not None
            item.staged_path = str(repointed)
        else:
            outline.immutable_path = str(repointed)
    before = _database_snapshot(database)

    with pytest.raises(RuntimeError, match="imported-derived adoption path is invalid"):
        database.migrate()

    assert _database_snapshot(database) == before


@pytest.mark.parametrize(
    "path_name",
    [
        "slide-immutable-source",
        "slide-canonical-source",
        "slide-canonical-pdf",
        "slide-icloud-pdf",
        "transcript-immutable",
        "transcript-canonical",
        "outline-immutable",
        "outline-canonical",
    ],
)
def test_current_v23_startup_rejects_same_byte_symlinked_persisted_paths(
    tmp_path, monkeypatch, path_name
):
    database, _settings, result = _current_v23_adoption_fixture(tmp_path)
    with database.session() as session:
        audit = session.get(ExistingArtifactImportModel, result.import_id)
        slide = session.get(StudyRevisionModel, result.slides_revision_id)
        assert audit is not None and slide is not None
        paths = {
            "slide-immutable-source": Path(slide.immutable_source_path),
            "slide-canonical-source": Path(slide.canonical_source_path or ""),
            "slide-canonical-pdf": Path(slide.canonical_derived_path or ""),
            "slide-icloud-pdf": Path(slide.icloud_path or ""),
            "transcript-immutable": Path(audit.immutable_transcript_path or ""),
            "transcript-canonical": Path(audit.canonical_transcript_path or ""),
            "outline-immutable": Path(audit.immutable_outline_path or ""),
            "outline-canonical": Path(audit.canonical_outline_path or ""),
        }
    target = paths[path_name]
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: original_is_symlink(path) or path == target,
    )
    before = _database_snapshot(database)

    with pytest.raises(RuntimeError, match="imported-derived adoption path is invalid"):
        database.migrate()

    assert _database_snapshot(database) == before


@pytest.mark.parametrize(
    "component",
    [
        "v2-root",
        "slide-ancestor",
        "study-root",
        "icloud-root",
        "imports-ancestor",
    ],
)
def test_current_v23_startup_rejects_mocked_windows_junction_components(
    tmp_path, monkeypatch, component
):
    """Junction safety must be covered even on platforms that cannot create one."""
    database, settings, result = _current_v23_adoption_fixture(tmp_path)
    with database.session() as session:
        audit = session.get(ExistingArtifactImportModel, result.import_id)
        assert audit is not None
        components = {
            "v2-root": settings.data_dir / "artifacts" / "v2",
            "slide-ancestor": settings.data_dir / "artifacts" / "v2" / "slides",
            "study-root": settings.study_root,
            "icloud-root": settings.icloud_staging_root,
            "imports-ancestor": Path(audit.immutable_transcript_path or "").parent.parent,
        }
    target = components[component]
    original_is_junction = getattr(Path, "is_junction", None)

    def is_junction(path: Path) -> bool:
        return path == target or (
            callable(original_is_junction) and bool(original_is_junction(path))
        )

    monkeypatch.setattr(Path, "is_junction", is_junction, raising=False)
    before = _database_snapshot(database)

    with pytest.raises(RuntimeError, match="imported-derived adoption path is invalid"):
        database.migrate()

    assert _database_snapshot(database) == before


@pytest.mark.parametrize("missing", ["immutable-source", "canonical-source"])
def test_current_v23_incomplete_adoption_requires_both_pptx_paths_without_mutation(
    tmp_path, missing
):
    database, _settings, result = _current_v23_adoption_fixture(tmp_path)
    with database.session() as session:
        audit = session.get(ExistingArtifactImportModel, result.import_id)
        slide = session.get(StudyRevisionModel, result.slides_revision_id)
        assert audit is not None and slide is not None
        old = Path(audit.previous_immutable_pdf_path or "")
        canonical = Path(slide.canonical_derived_path or "")
        icloud = Path(slide.icloud_path or "")
        audit.status = "preparing"
        audit.recovery_phase = "archived"
        slide.derived_sha256 = audit.previous_pdf_sha256
        slide.immutable_derived_path = audit.previous_immutable_pdf_path
        slide.provenance_kind = "llm_cleaned"
        slide.import_id = None
        canonical.write_bytes(old.read_bytes())
        icloud.write_bytes(old.read_bytes())
        target = (
            Path(slide.immutable_source_path)
            if missing == "immutable-source"
            else Path(slide.canonical_source_path or "")
        )
    target.unlink()
    before = _database_snapshot(database)

    with pytest.raises(RuntimeError, match="imported-derived adoption path is invalid"):
        database.migrate()

    assert _database_snapshot(database) == before


@pytest.mark.parametrize("tamper", ["relative", "symlink"])
def test_current_v23_first_write_future_paths_are_structurally_validated(tmp_path, tamper):
    database, settings, result = _current_v23_adoption_fixture(tmp_path)
    with database.session() as session:
        audit = session.get(ExistingArtifactImportModel, result.import_id)
        slide = session.get(StudyRevisionModel, result.slides_revision_id)
        assert audit is not None and slide is not None
        audit_root = Path(audit.immutable_transcript_path or "").parent
        archived = Path(audit.imported_immutable_pdf_path or "")
        old = Path(audit.previous_immutable_pdf_path or "")
        audit.status = "preparing"
        audit.recovery_phase = "preparing"
        slide.derived_sha256 = audit.previous_pdf_sha256
        slide.immutable_derived_path = audit.previous_immutable_pdf_path
        slide.provenance_kind = "llm_cleaned"
        slide.import_id = None
        Path(slide.canonical_derived_path or "").write_bytes(old.read_bytes())
        Path(slide.icloud_path or "").write_bytes(old.read_bytes())
        if tamper == "relative":
            audit.canonical_transcript_path = "relative/transcript.txt"
        else:
            outside = tmp_path / "outside"
            outside.mkdir()
            routed = settings.study_root / "routed"
            routed.symlink_to(outside, target_is_directory=True)
            audit.canonical_outline_path = str(routed / "outline.pdf")
    for path in (Path(audit.immutable_transcript_path or ""), Path(audit.immutable_outline_path or ""), archived):
        path.unlink()
    audit_root.rmdir()
    before = _database_snapshot(database)

    with pytest.raises(RuntimeError, match="imported-derived adoption path is invalid"):
        database.migrate()

    assert _database_snapshot(database) == before


def _prepare_current_v23_precursor_state(
    database: Database,
) -> tuple[tuple[Path, ...], tuple[bytes, ...], Path]:
    """Turn a completed fixture into the exact pre-archive adoption state."""
    with database.session() as session:
        audit = session.scalar(select(ExistingArtifactImportModel))
        assert audit is not None
        slide = session.get(StudyRevisionModel, audit.slide_revision_id)
        assert slide is not None
        old = Path(audit.previous_immutable_pdf_path or "")
        archive = Path(audit.imported_immutable_pdf_path or "")
        paths = (
            Path(audit.immutable_transcript_path or ""),
            Path(audit.immutable_outline_path or ""),
            Path(audit.canonical_transcript_path or ""),
            Path(audit.canonical_outline_path or ""),
        )
        payloads = tuple(path.read_bytes() for path in paths)
        audit.status = "preparing"
        audit.recovery_phase = "preparing"
        slide.derived_sha256 = audit.previous_pdf_sha256
        slide.immutable_derived_path = audit.previous_immutable_pdf_path
        slide.provenance_kind = "llm_cleaned"
        slide.import_id = None
        Path(slide.canonical_derived_path or "").write_bytes(old.read_bytes())
        Path(slide.icloud_path or "").write_bytes(old.read_bytes())
    archive.unlink()
    for path in paths:
        path.unlink()
    return paths, payloads, archive


@pytest.mark.parametrize("present_count", range(5))
def test_current_v23_preparing_precursor_prefixes_startup_cleanly(tmp_path, present_count):
    database, _settings, _result = _current_v23_adoption_fixture(tmp_path)
    paths, payloads, archive = _prepare_current_v23_precursor_state(database)
    for path, payload in zip(paths[:present_count], payloads[:present_count], strict=True):
        path.write_bytes(payload)

    assert not archive.exists()
    database.migrate()
    database.migrate()


@pytest.mark.parametrize("present_count", range(5))
@pytest.mark.parametrize(
    "corruption",
    ["wrong-bytes", "premature-archive", "non-prefix", "directory", "symlink"],
)
def test_current_v23_preparing_precursor_corruption_rejects_before_mutation(
    tmp_path, present_count, corruption
):
    database, _settings, _result = _current_v23_adoption_fixture(tmp_path)
    paths, payloads, archive = _prepare_current_v23_precursor_state(database)
    for path, payload in zip(paths[:present_count], payloads[:present_count], strict=True):
        path.write_bytes(payload)

    if corruption == "wrong-bytes":
        target = paths[max(0, present_count - 1)]
        target.write_bytes(b"wrong precursor bytes")
    elif corruption == "premature-archive":
        archive.write_bytes(b"premature imported PDF")
    elif corruption == "non-prefix":
        target_index = min(max(present_count, 1), 3)
        paths[target_index].write_bytes(payloads[target_index])
        paths[target_index - 1].unlink(missing_ok=True)
    elif corruption == "directory":
        target = paths[min(present_count, 3)]
        target.unlink(missing_ok=True)
        target.mkdir()
    else:
        target = paths[min(present_count, 3)]
        target.unlink(missing_ok=True)
        outside = tmp_path / "outside-precursor.txt"
        outside.write_bytes(payloads[0])
        target.symlink_to(outside)

    before = _database_snapshot(database)
    with pytest.raises(RuntimeError, match="schema v23 imported-derived adoption"):
        database.migrate()
    assert _database_snapshot(database) == before


@pytest.mark.parametrize(
    "corruption",
    [
        "preparing-archive",
        "archive-copying-missing-precursor",
        "archive-copying-wrong-archive",
        "archive-copying-wrong-mutable",
        "archive-copying-complete-status",
    ],
)
def test_current_v23_archive_copying_phase_corruption_rejects_before_mutation(
    tmp_path, corruption
):
    database, _settings, _result = _current_v23_adoption_fixture(tmp_path)
    paths, payloads, archive = _prepare_current_v23_precursor_state(database)
    for path, payload in zip(paths, payloads, strict=True):
        path.write_bytes(payload)
    with database.session() as session:
        audit = session.scalar(select(ExistingArtifactImportModel))
        assert audit is not None
        slide = session.get(StudyRevisionModel, audit.slide_revision_id)
        assert slide is not None
        if corruption == "preparing-archive":
            archive.write_bytes((tmp_path / "target.pdf").read_bytes())
        else:
            audit.recovery_phase = "archive_copying"
            if corruption == "archive-copying-missing-precursor":
                paths[-1].unlink()
            elif corruption == "archive-copying-wrong-archive":
                archive.write_bytes(b"wrong archive")
            elif corruption == "archive-copying-wrong-mutable":
                Path(slide.canonical_derived_path or "").write_bytes(b"third state")
            else:
                audit.status = "complete"
    before = _database_snapshot(database)
    with pytest.raises(RuntimeError, match="schema v23"):
        database.migrate()
    assert _database_snapshot(database) == before


@pytest.mark.parametrize(
    ("phase", "canonical_target", "icloud_target"),
    [
        ("preparing", False, False),
        ("archive_copying", False, False),
        ("archived", False, False),
        ("canonical_promoted", True, False),
        ("icloud_promoted", True, True),
        ("precommit", True, True),
        ("recovery_required", False, True),
    ],
)
def test_current_v23_incomplete_adoption_phase_states_startup_cleanly(
    tmp_path, phase, canonical_target, icloud_target
):
    database, _settings, result = _current_v23_adoption_fixture(tmp_path)
    with database.session() as session:
        audit = session.get(ExistingArtifactImportModel, result.import_id)
        slide = session.get(StudyRevisionModel, result.slides_revision_id)
        assert audit is not None and slide is not None
        old = Path(audit.previous_immutable_pdf_path)
        target = Path(audit.imported_immutable_pdf_path)
        canonical = Path(slide.canonical_derived_path)
        icloud = Path(slide.icloud_path)
        audit.status = "preparing"
        audit.recovery_phase = phase
        slide.derived_sha256 = audit.previous_pdf_sha256
        slide.immutable_derived_path = audit.previous_immutable_pdf_path
        slide.provenance_kind = "llm_cleaned"
        slide.import_id = None
        canonical.write_bytes(target.read_bytes() if canonical_target else old.read_bytes())
        icloud.write_bytes(target.read_bytes() if icloud_target else old.read_bytes())
        if phase == "preparing":
            target.unlink()
    database.migrate()
    database.migrate()


@pytest.mark.parametrize(
    ("status", "phase", "canonical_target", "icloud_target"),
    [
        ("preparing", "archived", False, False),
        ("failed", "recovery_required", True, True),
    ],
)
@pytest.mark.parametrize("corruption", ["audit-target-sha", "slide-lecture"])
def test_current_v23_incomplete_adoption_graph_corruption_rejects_before_mutation(
    tmp_path, status, phase, canonical_target, icloud_target, corruption
):
    database, settings, result = _current_v23_adoption_fixture(tmp_path)
    with database.session() as session:
        audit = session.get(ExistingArtifactImportModel, result.import_id)
        slide = session.get(StudyRevisionModel, result.slides_revision_id)
        assert audit is not None and slide is not None
        old = Path(audit.previous_immutable_pdf_path or "")
        target = Path(audit.imported_immutable_pdf_path or "")
        audit.status = status
        audit.recovery_phase = phase
        slide.derived_sha256 = audit.previous_pdf_sha256
        slide.immutable_derived_path = audit.previous_immutable_pdf_path
        slide.provenance_kind = "llm_cleaned"
        slide.import_id = None
        Path(slide.canonical_derived_path or "").write_bytes(
            target.read_bytes() if canonical_target else old.read_bytes()
        )
        Path(slide.icloud_path or "").write_bytes(
            target.read_bytes() if icloud_target else old.read_bytes()
        )
        audit_id = audit.id
        slide_id = slide.id
    if corruption == "audit-target-sha":
        with database.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE existing_artifact_imports SET slide_pdf_sha256=:digest WHERE id=:id"
                ),
                {"digest": "0" * 64, "id": audit_id},
            )
    else:
        other_lecture_id = CatalogRepository(database).upsert_lecture(
            LectureInput("Other", 9, 9, "Other", "", None)
        )
        with database.engine.begin() as connection:
            connection.execute(
                text("UPDATE study_revisions SET lecture_id=:lecture WHERE id=:id"),
                {"lecture": other_lecture_id, "id": slide_id},
            )
    before_database = _database_snapshot(database)
    before_files = _managed_file_snapshot(settings)

    with pytest.raises(RuntimeError, match="schema v23 incomplete adoption graph is invalid"):
        database.migrate()

    assert _database_snapshot(database) == before_database
    assert _managed_file_snapshot(settings) == before_files


@pytest.mark.parametrize(
    ("phase", "canonical_target", "icloud_target"),
    [
        ("archived", False, True),
        ("canonical_promoted", False, False),
        ("icloud_promoted", True, False),
        ("precommit", False, True),
    ],
)
def test_current_v23_incomplete_adoption_phase_rejects_wrong_mutable_bytes(
    tmp_path, phase, canonical_target, icloud_target
):
    database, _settings, result = _current_v23_adoption_fixture(tmp_path)
    with database.session() as session:
        audit = session.get(ExistingArtifactImportModel, result.import_id)
        slide = session.get(StudyRevisionModel, result.slides_revision_id)
        assert audit is not None and slide is not None
        old = Path(audit.previous_immutable_pdf_path)
        target = Path(audit.imported_immutable_pdf_path)
        canonical = Path(slide.canonical_derived_path)
        icloud = Path(slide.icloud_path)
        audit.status = "preparing"
        audit.recovery_phase = phase
        slide.derived_sha256 = audit.previous_pdf_sha256
        slide.immutable_derived_path = audit.previous_immutable_pdf_path
        slide.provenance_kind = "llm_cleaned"
        slide.import_id = None
        canonical.write_bytes(target.read_bytes() if canonical_target else old.read_bytes())
        icloud.write_bytes(target.read_bytes() if icloud_target else old.read_bytes())
    with pytest.raises(RuntimeError, match="imported-derived adoption files are invalid"):
        database.migrate()


@pytest.mark.parametrize("phase", ["canonical_promoted", "icloud_promoted", "precommit"])
def test_current_v23_later_phase_rejects_old_old_state_regardless_of_status(tmp_path, phase):
    database, _settings, result = _current_v23_adoption_fixture(tmp_path)
    with database.session() as session:
        audit = session.get(ExistingArtifactImportModel, result.import_id)
        slide = session.get(StudyRevisionModel, result.slides_revision_id)
        assert audit is not None and slide is not None
        old = Path(audit.previous_immutable_pdf_path)
        audit.status = "failed"
        audit.recovery_phase = phase
        slide.derived_sha256 = audit.previous_pdf_sha256
        slide.immutable_derived_path = audit.previous_immutable_pdf_path
        slide.provenance_kind = "llm_cleaned"
        slide.import_id = None
        Path(slide.canonical_derived_path).write_bytes(old.read_bytes())
        Path(slide.icloud_path).write_bytes(old.read_bytes())
    with pytest.raises(RuntimeError, match="imported-derived adoption files are invalid"):
        database.migrate()


@pytest.mark.parametrize(
    "mutation",
    [
        "null",
        "uppercase",
        "blank-operator",
        "blank-reason",
        "blank-time",
        "phase",
        "imported-escape",
        "imported-name",
        "previous-equals-imported",
        "previous-tamper",
        "imported-tamper",
        "canonical-tamper",
        "icloud-tamper",
        "slide-hash",
        "slide-path",
        "slide-provenance",
        "outline-edge",
    ],
)
def test_current_v23_adoption_corruption_is_rejected_without_normalization(tmp_path, mutation):
    database, _settings, result = _current_v23_adoption_fixture(tmp_path)
    with database.session() as session:
        audit = session.get(ExistingArtifactImportModel, result.import_id)
        slide = session.get(StudyRevisionModel, result.slides_revision_id)
        outline = session.get(OutlineOutputModel, result.outline_id)
        assert audit is not None and slide is not None and outline is not None
        if mutation == "null":
            audit.adoption_reason = None
        elif mutation == "uppercase":
            audit.imported_pdf_sha256 = audit.imported_pdf_sha256.upper()
        elif mutation == "blank-operator":
            audit.adoption_operator = ""
        elif mutation == "blank-reason":
            audit.adoption_reason = ""
        elif mutation == "blank-time":
            audit.adoption_confirmed_at = ""
        elif mutation == "phase":
            audit.recovery_phase = "preparing"
        elif mutation == "imported-escape":
            audit.imported_immutable_pdf_path = str(tmp_path / "escape.pdf")
        elif mutation == "imported-name":
            audit.imported_immutable_pdf_path = str(
                Path(audit.imported_immutable_pdf_path).with_name("wrong.pdf")
            )
        elif mutation == "previous-equals-imported":
            audit.previous_immutable_pdf_path = audit.imported_immutable_pdf_path
        elif mutation == "previous-tamper":
            Path(audit.previous_immutable_pdf_path).write_bytes(b"bad")
        elif mutation == "imported-tamper":
            Path(audit.imported_immutable_pdf_path).write_bytes(b"bad")
        elif mutation == "canonical-tamper":
            Path(slide.canonical_derived_path).write_bytes(b"bad")
        elif mutation == "icloud-tamper":
            Path(slide.icloud_path).write_bytes(b"bad")
        elif mutation == "slide-hash":
            slide.derived_sha256 = "a" * 64
        elif mutation == "slide-path":
            slide.immutable_derived_path = str(tmp_path / "wrong.pdf")
        elif mutation == "slide-provenance":
            slide.provenance_kind = "llm_cleaned"
        else:
            outline.slide_sha256 = "a" * 64
    with database.engine.connect() as connection:
        before = connection.execute(text("SELECT * FROM existing_artifact_imports")).all()
        version = connection.execute(
            text("SELECT version FROM schema_version WHERE id=1")
        ).scalar_one()
    with pytest.raises(RuntimeError):
        database.migrate()
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT * FROM existing_artifact_imports")).all() == before
        assert (
            connection.execute(text("SELECT version FROM schema_version WHERE id=1")).scalar_one()
            == version
        )


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


def _managed_file_snapshot(settings: Settings) -> tuple[tuple[str, bytes], ...]:
    assert settings.icloud_staging_root is not None
    files: list[tuple[str, bytes]] = []
    for root in (settings.data_dir, settings.study_root, settings.icloud_staging_root):
        if root.exists():
            files.extend(
                (str(path.relative_to(root)), path.read_bytes())
                for path in root.rglob("*")
                if path.is_file()
            )
    return tuple(sorted(files))


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
def test_raw_v20_invalid_provenance_graph_is_rejected_without_any_mutation(tmp_path, statement):
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
    with pytest.raises(
        RuntimeError, match=f"schema v20 imported artifact required table is missing: {table}"
    ):
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
        RuntimeError,
        match="schema v23 imported artifact required table is missing: outline_outputs",
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
        assert (
            connection.execute(text("SELECT version FROM schema_version WHERE id=1")).scalar_one()
            == 23
        )
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
        assert (
            connection.execute(text("SELECT version FROM schema_version WHERE id=1")).scalar_one()
            == 20
        )


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
        assert (
            connection.execute(text("SELECT version FROM schema_version WHERE id=1")).scalar_one()
            == 23
        )
        assert (
            connection.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='trigger' AND name="
                    "'trg_outline_replacement_review_insert'"
                )
            ).scalar_one()
            == "trg_outline_replacement_review_insert"
        )


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
        assert (
            connection.execute(text("SELECT version FROM schema_version WHERE id=1")).scalar_one()
            == 20
        )
        assert connection.execute(text(verification)).scalar_one() == before
        # Legacy slide failures happen before the v21 columns are added.
        if "slide_sha256='0'" in statement:
            columns = {
                row[1]
                for row in connection.execute(text("PRAGMA table_info(existing_artifact_imports)"))
            }
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
def test_v21_fails_closed_when_complete_import_links_are_mismatched(tmp_path, statement, message):
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
        assert (
            connection.execute(text("SELECT version FROM schema_version WHERE id=1")).scalar_one()
            == 20
        )


_V23_ADOPTION_COLUMNS = (
    "expected_current_pdf_sha256",
    "previous_pdf_sha256",
    "previous_immutable_pdf_path",
    "imported_pdf_sha256",
    "imported_immutable_pdf_path",
    "derived_provenance",
    "adoption_operator",
    "adoption_reason",
    "adoption_confirmed_at",
    "recovery_phase",
)


def _raw_v22_import_fixture(tmp_path: Path) -> Database:
    """Build a real v22 import schema that has not received any v23 DDL."""
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    _create_true_v20_import_fixture(database)
    database.migrate()
    with database.engine.begin() as connection:
        for column in _V23_ADOPTION_COLUMNS:
            connection.execute(
                text(f"ALTER TABLE existing_artifact_imports DROP COLUMN {column}")
            )
        connection.execute(text("UPDATE schema_version SET version=22 WHERE id=1"))
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT version FROM schema_version WHERE id=1")).scalar_one() == 22
        columns = {
            row[1]
            for row in connection.execute(text("PRAGMA table_info(existing_artifact_imports)"))
        }
        assert not (set(_V23_ADOPTION_COLUMNS) & columns)
    return database


def test_raw_v22_preflight_validates_before_v23_ddl_and_upgrades(tmp_path):
    database = _raw_v22_import_fixture(tmp_path)

    database.migrate()

    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT version FROM schema_version WHERE id=1")).scalar_one() == 23
        columns = {
            row[1]
            for row in connection.execute(text("PRAGMA table_info(existing_artifact_imports)"))
        }
        assert set(_V23_ADOPTION_COLUMNS) <= columns


@pytest.mark.parametrize(
    "corruption,expected",
    [
        ("graph", "schema v22 imported artifact graph is invalid"),
        ("column", "schema v22 import structural column is missing"),
        ("foreign-key", "schema v22 import foreign-key contract is invalid"),
        ("index", "schema v21 current-artifact index is missing or invalid"),
        ("review-trigger", "schema v22 outline replacement review trigger is invalid"),
        ("review-row", "schema v22 outline replacement review row is invalid"),
    ],
)
def test_raw_v22_preflight_rejects_corruption_without_v23_ddl(
    tmp_path, corruption, expected
):
    database = _raw_v22_import_fixture(tmp_path)
    if corruption == "review-row":
        with database.session() as session:
            session.add(
                GenerationJobModel(
                    id="review-row-job",
                    lecture_id=1,
                    kind="outline",
                )
            )
    with database.engine.begin() as connection:
        if corruption == "graph":
            connection.execute(
                text("UPDATE existing_artifact_imports SET slide_pdf_sha256='0' WHERE id='import-1'")
            )
        elif corruption == "column":
            connection.execute(text("DROP TRIGGER trg_outline_replacement_review_insert"))
            connection.execute(text("DROP TRIGGER trg_outline_replacement_review_update"))
            connection.execute(text("ALTER TABLE existing_artifact_imports DROP COLUMN slide_source_sha256"))
        elif corruption == "foreign-key":
            connection.execute(text("DROP TRIGGER trg_outline_replacement_review_insert"))
            connection.execute(text("DROP TRIGGER trg_outline_replacement_review_update"))
            connection.execute(text("ALTER TABLE outline_replacement_reviews RENAME TO bad_reviews"))
            connection.execute(
                text("""
                CREATE TABLE outline_replacement_reviews (
                    generation_job_id VARCHAR(36) NOT NULL,
                    lecture_id INTEGER NOT NULL REFERENCES lectures(id) ON DELETE RESTRICT,
                    import_id VARCHAR(36) NOT NULL,
                    operator VARCHAR(200) NOT NULL,
                    reason TEXT NOT NULL,
                    confirmed_at VARCHAR(40) NOT NULL,
                    PRIMARY KEY (generation_job_id),
                    FOREIGN KEY(generation_job_id) REFERENCES generation_jobs(id) ON DELETE RESTRICT
                )
                """)
            )
            connection.execute(text("DROP TABLE bad_reviews"))
        elif corruption == "index":
            connection.execute(text("DROP INDEX uq_study_revisions_current_lecture_kind"))
        elif corruption == "review-trigger":
            connection.execute(text("DROP TRIGGER trg_outline_replacement_review_insert"))
        else:
            trigger_sql = connection.execute(
                text(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' "
                    "AND name IN ('trg_outline_replacement_review_insert', "
                    "'trg_outline_replacement_review_update') ORDER BY name"
                )
            ).scalars().all()
            connection.execute(text("DROP TRIGGER trg_outline_replacement_review_insert"))
            connection.execute(text("DROP TRIGGER trg_outline_replacement_review_update"))
            connection.execute(
                text("""
                INSERT INTO outline_replacement_reviews
                (generation_job_id, lecture_id, import_id, operator, reason, confirmed_at)
                VALUES ('review-row-job', 1, 'import-1', 'operator', 'reason', '2026-08-10T00:00:00Z')
                """)
            )
            for definition in trigger_sql:
                connection.execute(text(definition))
    before = _database_snapshot(database)

    with pytest.raises(RuntimeError, match=expected):
        database.migrate()

    assert _database_snapshot(database) == before
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT version FROM schema_version WHERE id=1")).scalar_one() == 22
        columns = {
            row[1]
            for row in connection.execute(text("PRAGMA table_info(existing_artifact_imports)"))
        }
        assert not (set(_V23_ADOPTION_COLUMNS) & columns)
