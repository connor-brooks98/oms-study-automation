import hashlib

from fastapi.testclient import TestClient
from selectolax.parser import HTMLParser

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.ingestion.domain import StagedUpload, UploadKind
from oms_hub.models import StudyRevisionModel
from oms_hub.repositories import LectureInput


def test_lecture_page_shows_separate_outline_and_quiz_controls(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
            study_root=tmp_path / "study",
        )
    )
    lecture_id = app.state.catalog_repository.upsert_lecture(
        LectureInput("Neuro", 1, 1, "Seizures", "Faculty", None)
    )

    page = TestClient(app).get(f"/lectures/{lecture_id}")

    assert page.status_code == 200
    assert page.text.count("Generate Lecture Outline") == 1
    assert page.text.count("Generate Lecture Quiz") == 1
    assert "Lecture Outline (PDF)" in page.text
    assert "Lecture Quiz" in page.text
    assert "Built from this lecture's PDF and cleaned transcript only." in page.text
    assert "Gemini Quiz Gem" not in page.text
    assert f'href="/uploads/slides?lecture_id={lecture_id}"' not in page.text
    assert "Upload Lecture PPTX" not in page.text
    assert f'href="/uploads/transcripts?lecture_id={lecture_id}"' not in page.text
    assert "Upload Lecture Transcript" not in page.text
    assert "Lecture Summary" in page.text
    assert "Lecture Quiz Generation" in page.text


def test_lecture_page_shows_five_pass_ledger_rows_between_expandable_panels(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        )
    )
    lecture_id = app.state.catalog_repository.upsert_lecture(
        LectureInput("Neuro", 1, 1, "Seizures", "Faculty", None)
    )

    page = TestClient(app).get(f"/lectures/{lecture_id}")
    document = HTMLParser(page.text)

    assert page.status_code == 200
    processing = document.css_first("[data-processing-checklist]")
    tracker = document.css_first("[data-pass-tracker]")
    metadata = document.css_first(".metadata-panel")
    assert processing is not None and processing.tag == "details"
    assert tracker is not None and tracker.tag == "details"
    assert metadata is not None and metadata.tag == "details"
    assert (
        page.text.index("data-processing-checklist")
        < page.text.index("data-pass-tracker")
        < page.text.index("metadata-panel")
    )

    rows = tracker.css("[data-pass-row]")
    assert len(rows) == 5
    for position, row in enumerate(rows, start=1):
        assert row.attributes["data-pass-position"] == str(position)
        completion = row.css_first("[data-pass-complete]")
        assert completion is not None and completion.attributes["type"] == "checkbox"
        assert row.css_first("[data-pass-date]") is not None
        assert row.css_first("select[data-pass-resource]") is not None
        custom = row.css_first("[data-pass-resource-custom]")
        assert custom is not None and "hidden" in custom.attributes
        custom_input = custom.css_first("[data-pass-resource-name]")
        assert custom_input is not None
        assert custom_input.attributes["maxlength"] == "100"
        assert "required" in custom_input.attributes
        assert custom_input.attributes["id"] == f"pass-resource-name-{position}"
        label = custom.css_first("label")
        assert label is not None
        assert label.attributes["for"] == custom_input.attributes["id"]
        add_resource = custom.css_first("[data-add-pass-resource]")
        assert add_resource is not None
        assert add_resource.text(strip=True) == "Add & use"
    assert tracker.css_first("[data-pass-count]").text(strip=True) == "0/5"
    add_pass = tracker.css_first("[data-add-pass]")
    assert add_pass is not None and "disabled" in add_pass.attributes


def test_lecture_page_renders_reusable_custom_resources_for_other_lectures(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        )
    )
    original_id = app.state.catalog_repository.upsert_lecture(
        LectureInput("Neuro", 1, 1, "Seizures", "Faculty", None)
    )
    other_id = app.state.catalog_repository.upsert_lecture(
        LectureInput("Neuro", 1, 2, "Spine", "Faculty", None)
    )
    client = TestClient(app)
    client.get(f"/lectures/{original_id}")
    csrf_token = client.cookies.get("study_hub_csrf")
    assert csrf_token is not None
    headers = {"X-CSRF-Token": csrf_token}

    saved = client.patch(
        f"/api/lectures/{original_id}/passes/1",
        json={"resource": "Pathoma"},
        headers=headers,
    )
    changed = client.patch(
        f"/api/lectures/{original_id}/passes/1",
        json={"resource": "Anki"},
        headers=headers,
    )
    document = HTMLParser(client.get(f"/lectures/{other_id}").text)
    options = document.css_first("[data-pass-resource]").css("option")

    assert saved.status_code == 200
    assert changed.status_code == 200
    assert [option.attributes.get("value") for option in options].count("Pathoma") == 1


def test_lecture_page_links_previous_and_next_within_subject_exam_order(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        )
    )
    previous_id = app.state.catalog_repository.upsert_lecture(
        LectureInput("Neuro", 1, 1, "Brain", "", None)
    )
    current_id = app.state.catalog_repository.upsert_lecture(
        LectureInput("Neuro", 1, 2, "Spinal cord", "", None)
    )
    next_id = app.state.catalog_repository.upsert_lecture(
        LectureInput("Neuro", 2, 1, "Neuropathy", "", None)
    )
    other_subject_id = app.state.catalog_repository.upsert_lecture(
        LectureInput("MSK", 1, 1, "Shoulder", "", None)
    )

    page = TestClient(app).get(f"/lectures/{current_id}")

    assert page.status_code == 200
    assert f'href="/lectures/{previous_id}"' in page.text
    assert f'href="/lectures/{next_id}"' in page.text
    assert f'href="/lectures/{other_subject_id}"' not in page.text
    assert "Previous lecture" in page.text
    assert "Next lecture" in page.text


def test_lecture_page_does_not_mark_a_changed_pdf_as_ready(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path / "data",
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
            study_root=tmp_path / "study",
        )
    )
    lecture_id = app.state.catalog_repository.upsert_lecture(
        LectureInput("Neuro", 1, 1, "Seizures", "Faculty", None)
    )
    changed_pdf = tmp_path / "changed.pdf"
    changed_pdf.write_bytes(b"changed after filing")
    repository = app.state.ingestion_repository
    batch_id = repository.create_batch(UploadKind.SLIDES)
    repository.add_item(
        UploadKind.SLIDES,
        StagedUpload(
            batch_id=batch_id,
            item_id="changed-slide",
            path=changed_pdf,
            sha256=hashlib.sha256(b"source").hexdigest(),
            size_bytes=len(b"source"),
            original_filename="lecture.pptx",
        ),
    )
    with app.state.database.session() as session:
        session.add(
            StudyRevisionModel(
                upload_item_id="changed-slide",
                lecture_id=lecture_id,
                kind=UploadKind.SLIDES.value,
                source_sha256=hashlib.sha256(b"source").hexdigest(),
                immutable_source_path=str(changed_pdf),
                derived_sha256=hashlib.sha256(b"original pdf").hexdigest(),
                immutable_derived_path=str(changed_pdf),
                canonical_source_path=str(changed_pdf),
                canonical_derived_path=str(changed_pdf),
                state="current",
                current=True,
            )
        )

    page = TestClient(app).get(f"/lectures/{lecture_id}")

    assert page.status_code == 200
    assert "Lecture PDF file checksum does not match." in page.text
    assert "Open Lecture PDF" not in page.text


def test_dashboard_has_separate_slide_and_transcript_uploads(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        )
    )

    page = TestClient(app).get("/lectures")

    assert 'href="/uploads/slides"' in page.text
    assert "+ Upload lecture" in page.text
    assert 'href="/uploads/transcripts"' in page.text
    assert "+ Upload transcript" in page.text


def test_dashboard_only_exposes_the_first_course_and_exam_by_default(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        )
    )
    app.state.catalog_repository.upsert_lecture(
        LectureInput("MSK", 1, 7, "Shoulder", "", None)
    )
    app.state.catalog_repository.upsert_lecture(
        LectureInput("Neuro", 1, 1, "Brain", "", None)
    )

    page = TestClient(app).get("/lectures")

    assert page.status_code == 200
    assert page.text.count('aria-expanded="true"') == 2
    neuro = page.text[page.text.index('data-course="Neuro"'):]
    assert 'aria-expanded="false"' in neuro
    assert 'id="course-2" hidden' in neuro


def test_dashboard_links_exam_label_to_pass_overview_beside_its_disclosure_button(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        )
    )
    app.state.catalog_repository.upsert_lecture(
        LectureInput("Heme/Lymph", 2, 12, "Platelet Disorders", "", None)
    )

    page = TestClient(app).get("/lectures")
    document = HTMLParser(page.text)
    exam = document.css_first(".exam-group")

    assert exam is not None
    overview = exam.css_first("a.exam-overview-link")
    disclosure = exam.css_first("button.exam-toggle")
    assert overview is not None
    assert overview.text(strip=True) == "Exam 2"
    assert overview.attributes["href"] == (
        "/lectures/exams/2/passes?subject=Heme/Lymph"
    )
    assert disclosure is not None and "data-disclosure" in disclosure.attributes
    assert disclosure.attributes["aria-controls"]


def test_exam_pass_overview_lists_each_lecture_count_and_progress(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        )
    )
    first_id = app.state.catalog_repository.upsert_lecture(
        LectureInput("Heme/Lymph", 2, 12, "Platelet Disorders", "", None)
    )
    second_id = app.state.catalog_repository.upsert_lecture(
        LectureInput("Heme/Lymph", 2, 13, "Mononucleosis", "", None)
    )

    page = TestClient(app).get(
        "/lectures/exams/2/passes",
        params={"subject": "Heme/Lymph"},
    )
    document = HTMLParser(page.text)

    assert page.status_code == 200
    assert document.css_first("h1").text(strip=True) == "Exam 2 passes"
    overview = document.css_first("[data-exam-pass-overview]")
    assert overview is not None
    assert [heading.text(strip=True) for heading in overview.css("th")] == [
        "Lecture",
        "Passes",
        "Progress",
        "Last pass",
    ]
    rows = overview.css("[data-exam-lecture]")
    assert len(rows) == 2
    assert [row.css_first("a").attributes["href"] for row in rows] == [
        f"/lectures/{first_id}",
        f"/lectures/{second_id}",
    ]
    for row in rows:
        assert row.css_first("[data-pass-count]").text(strip=True) == "0 / 5"
        progress = row.css_first("[data-pass-progress]")
        assert progress.attributes["role"] == "progressbar"
        assert progress.attributes["aria-valuemin"] == "0"
        assert progress.attributes["aria-valuemax"] == "5"
        assert progress.attributes["aria-valuenow"] == "0"


def test_review_uses_course_relative_human_label_for_proposed_revisions(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        )
    )
    app.state.catalog_repository.upsert_lecture(
        LectureInput("Placeholder", 1, 1, "First", "", None)
    )
    lecture_id = app.state.catalog_repository.upsert_lecture(
        LectureInput("MSK", 1, 7, "Shoulder", "", None)
    )
    source = tmp_path / "proposed.pptx"
    source.write_bytes(b"proposed source")
    batch_id = app.state.ingestion_repository.create_batch(UploadKind.SLIDES)
    app.state.ingestion_repository.add_item(
        UploadKind.SLIDES,
        StagedUpload(
            batch_id=batch_id,
            item_id="proposed-label",
            path=source,
            sha256=hashlib.sha256(b"proposed source").hexdigest(),
            size_bytes=len(b"proposed source"),
            original_filename="proposed.pptx",
        ),
    )
    with app.state.database.session() as session:
        revision = StudyRevisionModel(
            upload_item_id="proposed-label",
            lecture_id=lecture_id,
            kind=UploadKind.SLIDES.value,
            source_sha256=hashlib.sha256(b"proposed source").hexdigest(),
            immutable_source_path=str(source),
            state="proposed",
            current=False,
        )
        session.add(revision)
        session.flush()
        revision_id = revision.id

    page = TestClient(app).get("/review")

    assert page.status_code == 200
    assert "MSK Lecture 07" in page.text
    assert "Lecture 2</h3>" not in page.text
    assert f"Revision {revision_id}" in page.text
    assert f"/artifacts/{revision_id}/pdf" in page.text

    with app.state.database.session() as session:
        interrupted = session.get(StudyRevisionModel, revision_id)
        assert interrupted is not None
        interrupted.state = "promoting"

    recovery_page = TestClient(app).get("/review")

    assert f"Revision {revision_id}" in recovery_page.text
    assert "Promotion was interrupted." in recovery_page.text
    assert "Resume recovery" in recovery_page.text
    assert "Keep current file" not in recovery_page.text


def test_lecture_upload_page_targets_the_selected_lecture(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        )
    )
    lecture_id = app.state.catalog_repository.upsert_lecture(
        LectureInput("Neuro", 1, 1, "Seizures", "Faculty", None)
    )

    page = TestClient(app).get(
        f"/uploads/transcripts?lecture_id={lecture_id}"
    )

    assert page.status_code == 200
    assert "Neuro Lecture 01 · Seizures" in page.text
    assert f'name="lecture_id" value="{lecture_id}"' in page.text


def test_targeted_upload_is_assigned_to_the_selected_lecture(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        )
    )
    lecture_id = app.state.catalog_repository.upsert_lecture(
        LectureInput("Neuro", 1, 1, "Seizures", "Faculty", None)
    )
    client = TestClient(app)

    upload = client.post(
        "/uploads/transcripts",
        data={"lecture_id": str(lecture_id)},
        files={"files": ("lecture.txt", b"Lecture transcript", "text/plain")},
    )
    batch = client.get(
        f"/api/upload-batches/{upload.json()['batch_id']}"
    ).json()

    assert upload.status_code == 202
    assert batch["items"][0]["lecture_id"] == lecture_id
