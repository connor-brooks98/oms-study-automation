from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from oms_hub.web.routes import _course_hue

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "src" / "oms_hub" / "web" / "templates"
STATIC = ROOT / "src" / "oms_hub" / "web" / "static"


def source(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def static_source(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_shared_stylesheet_is_the_approved_blue_workbench_source() -> None:
    installed = STATIC / "study-hub.css"

    assert installed.exists()
    assert (
        "0bf617b203d2d91bedfec046b23be45e89cad9bdf67a47aa9c65d80749cfbd80"
        == sha256(installed.read_bytes()).hexdigest()
    )


def test_stylesheet_order_and_system_font_contract() -> None:
    base = source("base.html")
    assert base.index('href="/static/reset.css"') < base.index('href="/static/tokens.css"')
    assert base.index('href="/static/tokens.css"') < base.index('href="/static/study-hub.css"')
    assert base.index('href="/static/study-hub.css"') < base.index('href="/static/app.css')
    assert '<body class="sh-app">' in base

    for name, page_css in (
        ("public_quiz.html", "/public/quizzes/assets/player.css"),
        ("public_quiz_library.html", "/public/quizzes/assets/library.css"),
        ("studio_quiz_preview.html", "/public/quizzes/assets/player.css"),
    ):
        template = source(name)
        assert "IBM+Plex+Sans" not in template
        assert (
            template.index("reset.css") < template.index("study-hub.css") < template.index(page_css)
        )
        assert 'class="sh-app' in template or '<body class="sh-app"' in template


def test_private_shell_uses_approved_navigation_and_dialog_contracts() -> None:
    base = source("base.html")
    shell_js = static_source("study_hub_shell.js")

    assert 'class="site-header sh-topbar"' in base
    assert "Home</a>" in base
    assert "Lectures</a>" in base
    assert "Anki</a>" in base
    assert "Quiz Builder</a>" in base
    assert "Practice Questions</a>" in base
    assert '<details class="sh-more">' in base
    for destination in (
        "/uploads/slides",
        "/uploads/transcripts",
        "/quarantine",
        "/review",
        "/settings",
    ):
        assert destination in base

    assert '<dialog class="sh-dialog sh-command" id="command-palette"' in base
    assert '<dialog class="sh-dialog sh-mobile-nav" id="mobile-navigation"' in base
    assert 'href="/" data-dialog-initial-focus>Home</a>' in base
    assert 'aria-keyshortcuts="Meta+K Control+K"' in base
    assert "event.metaKey || event.ctrlKey" in shell_js
    assert 'event.key === "ArrowDown" || event.key === "ArrowUp"' in shell_js
    assert 'dialog.addEventListener("close", () => restoreFocus(dialog))' in shell_js


def test_template_layout_matrix_uses_locked_containers_and_headers() -> None:
    wide = (
        "home.html",
        "dashboard.html",
        "lecture.html",
        "settings.html",
        "anki.html",
        "anki_review.html",
        "notebook_studio.html",
        "tracker_preview.html",
        "public_quiz_library.html",
    )
    narrow = (
        "uploads.html",
        "quarantine.html",
        "review.html",
        "artifact_text.html",
        "studio_quiz_review.html",
        "studio_quiz_images.html",
        "public_quiz.html",
        "studio_quiz_preview.html",
    )
    for name in wide:
        assert "sh-container" in source(name), name
    for name in narrow:
        assert "sh-container--narrow" in source(name), name

    for name in (
        "home.html",
        "dashboard.html",
        "lecture.html",
        "settings.html",
        "anki.html",
        "notebook_studio.html",
    ):
        template = source(name)
        assert "sh-header" in template, name
        assert "sh-title" in template, name


def test_public_library_separates_public_identity_while_players_remain_focus_mode() -> None:
    library = source("public_quiz_library.html")
    assert "sh-nav" in library
    assert "Study Hub Quizzes" in library
    assert "{% if owner_navigation %}" in library
    assert "NUC online" not in library
    assert 'class="sh-title"' in library
    for hook in (
        "data-title-form",
        "data-move-quiz-library",
        "data-remove-quiz",
        "data-quiz-drag-handle",
        "data-quiz-overflow",
    ):
        assert hook in library

    for name in ("public_quiz.html", "studio_quiz_preview.html"):
        template = source(name)
        assert "sh-nav" not in template
        assert "quiz-library-button" in template or "← Back to" in template
        assert "quiz-app" in template


def test_presentational_contracts_cover_forms_and_deferred_review_hooks() -> None:
    assert 'class="page-shell sh-container--narrow upload-page"' in source("uploads.html")
    assert "sh-select" in source("quarantine.html")
    assert "sh-select" in source("settings.html")
    assert "sh-select" in source("notebook_studio.html")
    assert "sh-option" in (STATIC / "public_quiz.js").read_text(encoding="utf-8")
    assert "data-practice-review" in source("studio_quiz_review.html")
    assert "data-review-blockers" in source("studio_quiz_review.html")


def test_daily_workbench_keeps_progress_labels_and_unavailable_actions_honest() -> None:
    dashboard = source("home.html")
    lecture = source("lecture.html")
    uploads = source("uploads.html")
    review = source("review.html")

    assert 'class="dashboard-workbench"' in dashboard
    assert 'class="lecture-workbench"' in lecture
    assert 'href="/uploads/slides" aria-disabled="true"' not in lecture
    assert "Lecture PDF unavailable" in lecture
    assert "Cleaned transcript unavailable" in lecture
    assert '<progress class="upload-progress" max="100" value="0"' in uploads
    assert "data-cancel-duplicate" in uploads
    assert "subject }} Lecture {{ \"%02d\"|format(lecture.lecture_number) }}" in review


def test_tracker_control_and_disclosure_scans_cover_the_locked_residuals() -> None:
    anki = source("anki.html")
    studio = source("notebook_studio.html")
    dashboard = source("dashboard.html")
    library = source("public_quiz_library.html")

    assert "<fieldset" not in anki and "<legend" not in anki
    assert "<fieldset" not in studio and "<legend" not in studio
    assert all('class="sh-select"' in line for line in anki.splitlines() if "<select" in line)
    assert "sh-input" in studio and "sh-textarea" in studio and "sh-file" in studio
    assert dashboard.index("needs review") < dashboard.index('class="heading-actions"')
    assert "⌄" not in library
    assert "sh-nav" in library and "sh-subject-dot" in library
    assert "13/13 complete" not in source("lecture.html")
    assert "release_steps|length }}/{{ release_steps|length }} complete" in source("lecture.html")
    assert '"✕"' in (STATIC / "public_quiz.js").read_text(encoding="utf-8")
    assert "data-quiz-drag-handle" in library
    assert "Move up" not in library and "Move down" not in library


def test_quiz_builder_import_forms_use_locked_controls_without_losing_hooks() -> None:
    studio = source("notebook_studio.html")
    assert "<fieldset" not in studio and "<legend" not in studio
    assert studio.count("data-import-source-form") == 3
    assert studio.count('class="studio-intake-form" data-import-source-form') == 3
    assert studio.count('class="sh-check studio-intake-notebook"') == 3
    assert 'class="sh-validation" data-import-source-list' in studio
    for tag, required in (("select", "sh-select"), ("textarea", "sh-textarea")):
        assert all(required in line for line in studio.splitlines() if f"<{tag}" in line)
    assert "data-import-destination-course" in studio
    assert "data-import-destination-exam" in studio
    assert "Add one source at a time" in studio
    assert "Add related files in separate passes" in studio


def test_settings_custom_models_have_connected_visible_labels() -> None:
    settings = source("settings.html")
    settings_js = static_source("settings.js")

    assert 'for="custom-model-{{ provider.name }}" hidden>Custom model ID</label>' in settings
    assert 'id="custom-model-{{ provider.name }}"' in settings
    assert 'for="assignment-custom-{{ row.task }}" hidden>Custom model ID</label>' in settings
    assert 'id="assignment-custom-{{ row.task }}"' in settings
    assert "customInput.labels || []" in settings_js


def test_locked_components_are_not_repainted_by_late_legacy_css() -> None:
    app_css = (STATIC / "app.css").read_text(encoding="utf-8")
    player_css = (STATIC / "public_quiz.css").read_text(encoding="utf-8")

    for selector in (
        ".page-shell:not(.sh-container):not(.sh-container--narrow)",
        ".button:not(.sh-btn)",
        ".card:not(.sh-card)",
        ".empty-state:not(.sh-empty)",
        ".status-pill:not(.sh-pill)",
        ".status-dot:not(.sh-dot)",
        ".upload-zone:not(.sh-dropzone)",
        ".studio-import-intake [data-import-source-form]:not(.sh-card)",
    ):
        assert selector in app_css
    assert ".file-card.missing { border-style: dashed; }" not in app_css
    assert "content: \"+\"" not in app_css
    assert "content: \"−\"" not in app_css
    assert "details[open] > summary > .sh-disclose" in app_css

    for selector in (
        ".quiz-library-button:not(.sh-btn)",
        ".quiz-primary:not(.sh-btn)",
        ".quiz-secondary:not(.sh-btn)",
        ".quiz-tool:not(.sh-btn)",
        ".quiz-answer-row.sh-option .quiz-answer",
        ".quiz-result h1:not(.sh-title)",
    ):
        assert selector in player_css
    assert "color: var(--quiz-brand);" not in player_css.split(".quiz-label", 1)[1].split(
        ".quiz-question", 1
    )[0]


def test_status_and_focus_player_paths_use_locked_semantic_components() -> None:
    anki = source("anki.html")
    anki_review = source("anki_review.html")
    lecture = source("lecture.html")
    quarantine = source("quarantine.html")
    anki_js = (STATIC / "anki.js").read_text(encoding="utf-8")
    player_js = (STATIC / "public_quiz.js").read_text(encoding="utf-8")

    for template in (anki, anki_review, lecture):
        for modifier in ("sh-pill--ok", "sh-pill--info", "sh-pill--warn", "sh-pill--err"):
            assert modifier in template
    assert "sh-dot" in anki and "job_tone.replace('pill', 'dot')" in anki
    assert "sh-pill--err' if items|length else 'sh-pill--bare" in quarantine
    assert "const statusTone" in anki_js
    assert "sh-pill sh-pill--info" in anki_js
    assert '"anki-group-empty sh-empty"' in anki_js
    assert '"anki-select-control sh-check"' in anki_js

    for token in (
        "quiz-secondary quiz-review sh-btn sh-btn--secondary",
        "quiz-tool sh-btn sh-btn--ghost sh-btn--sm",
        "quiz-strike sh-iconbtn",
        'element(documentRef, "h1", "sh-title", "Quiz complete")',
    ):
        assert token in player_js

    assert 'class="nuc-state__dot" aria-hidden="true"></span>NUC online' in source("base.html")


def test_visual_followups_keep_layout_and_restart_controls_in_their_owners() -> None:
    app_css = static_source("app.css")
    library_css = static_source("public_quiz_library.css")
    player_js = static_source("public_quiz.js")
    library = source("public_quiz_library.html")

    assert "a.sh-btn--primary:hover { color: var(--brand-ink); }" in app_css
    assert ".anki-workbench-panel" in app_css
    assert ".provider-card-heading > :first-child" in app_css
    assert ".exam-card { margin-top: var(--sp-2); padding: 0;" in library_css
    assert ".sh-topbar .sh-nav { width: 100%; flex-wrap: wrap; overflow: visible; }" in library_css
    assert 'title="Restart {{ row.title }}"' in library
    assert "Reset quiz" not in player_js
    assert "Start Over" not in player_js


def test_subject_hues_and_statuses_use_the_locked_route_owned_maps() -> None:
    assert {
        subject: _course_hue(subject)
        for subject in (
            "Clinical Neuroscience",
            "Neuro",
            "MSK",
            "OPP",
            "EPC",
            "Heme Lymph",
            "Heme/Lymph",
            "Cardio",
            "Renal",
            "Resp",
            "Respiratory",
        )
    } == {
        "Clinical Neuroscience": 290,
        "Neuro": 290,
        "MSK": 50,
        "OPP": 175,
        "EPC": 95,
        "Heme Lymph": 15,
        "Heme/Lymph": 15,
        "Cardio": 340,
        "Renal": 135,
        "Resp": 210,
        "Respiratory": 210,
    }

    review = source("review.html")
    library = source("public_quiz_library.html")
    assert "subject_hues" not in review and "lecture_hues.get(lecture.id, 255)" in review
    assert "subject_hues" not in library and "course.hue" in library

    anki_js = (STATIC / "anki.js").read_text(encoding="utf-8")
    assert '"ready_for_review"].includes(normalized)) return "sh-pill--warn"' in anki_js
    assert '"ready_for_review", "configured"' not in anki_js


def test_shared_chrome_containers_and_settings_pills_resist_legacy_cascade() -> None:
    app_css = (STATIC / "app.css").read_text(encoding="utf-8")
    settings = source("settings.html")

    for selector in (
        ".utility-nav:not(.sh-nav)",
        ".brand-bar:not(.sh-brand)",
        ".brand:not(.sh-brand__name)",
        ".brand-mark:not(.sh-brand__tile)",
        ".upload-page:not(.sh-container):not(.sh-container--narrow)",
        ".settings-page:not(.sh-container):not(.sh-container--narrow)",
        ".credential-state:not(.sh-pill)",
    ):
        assert selector in app_css
    assert ".site-header {" not in app_css
    assert ".connection-button.is-connected" not in app_css
    assert ".connection-button.is-failed" not in app_css

    heading_start = settings.index('<div class="provider-card-heading">')
    actions_start = settings.index('<div class="provider-buttons">', heading_start)
    connection_state = settings.index("data-connection-state", heading_start)
    assert heading_start < connection_state < actions_start
    action_row = settings[actions_start : settings.index("</div>", actions_start)]
    assert "data-connection-state" not in action_row
    assert "connection-button {% if" not in settings
    for hook in (
        "data-voyage-configured",
        "data-notebook-badge",
        "data-assignment-key",
        "data-runtime-port-source",
    ):
        position = settings.index(hook)
        assert "credential-state sh-pill" in settings[position - 120 : position]


def test_player_and_dynamic_foundations_preserve_shared_components() -> None:
    player_css = (STATIC / "public_quiz.css").read_text(encoding="utf-8")
    anki_js = (STATIC / "anki.js").read_text(encoding="utf-8")
    lecture_js = (STATIC / "lecture.js").read_text(encoding="utf-8")
    library = source("public_quiz_library.html")
    lecture = source("lecture.html")

    for selector in (
        ".quiz-app:not(.sh-container--narrow)",
        ".quiz-app.sh-container--narrow",
        ".quiz-flag-select:not(.sh-select)",
        ".studio-preview-actions:not(.sh-card)",
    ):
        assert selector in player_css
    assert ".quiz-app {" not in player_css
    assert ".quiz-flag-select { max-width: 16rem; }" in player_css
    assert 'class="chevron sh-disclose"' in library
    assert 'aria-expanded="false"' in library
    assert '"sh-empty anki-empty-compact"' in anki_js
    assert '"sh-empty__title"' in anki_js
    assert '"1 Study Hub quiz is ready."' in lecture_js
    assert "1 Study Hub quiz is ready." in lecture
    assert "slide_revision.canonical_derived_path.name" in lecture
    assert "transcript_revision.canonical_derived_path.name" in lecture


def test_final_visual_corrections_preserve_locked_chrome() -> None:
    library_css = (STATIC / "public_quiz_library.css").read_text(encoding="utf-8")
    player_css = (STATIC / "public_quiz.css").read_text(encoding="utf-8")
    app_css = (STATIC / "app.css").read_text(encoding="utf-8")
    anki = source("anki.html")

    assert ".skip-link:focus-visible" in library_css
    assert "clip: rect(0, 0, 0, 0);" in library_css
    assert "[hidden] { display: none !important; }" in library_css
    disclosure = library_css.split(".disclosure {", 1)[1].split("}", 1)[0]
    assert "border: 0;" in disclosure and "background: transparent;" in disclosure
    assert 'class="anki-advanced"' in anki
    assert ".anki-advanced:not(.sh-card)" in app_css
    assert ".checkbox-inline:not(.sh-check) input[type=\"checkbox\"]" in app_css
    selected_medallion = player_css.split(
        ".quiz-answer-row.is-selected .quiz-choice-letter {", 1
    )[1].split("}", 1)[0]
    assert "background: var(--brand);" in selected_medallion
    assert "--quiz-brand-mid" not in selected_medallion


def test_sh_disclose_summaries_hide_only_the_native_marker() -> None:
    app_css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert "details > summary:has(> .sh-disclose) { list-style: none; }" in app_css
    assert (
        "details > summary:has(> .sh-disclose)::-webkit-details-marker"
        " { display: none; }"
    ) in app_css

    for name in ("anki.html", "lecture.html", "settings.html"):
        assert '<summary><span class="sh-disclose"' in source(name) or (
            '<summary>\n      <span class="sh-disclose"' in source(name)
        )


def test_import_checks_and_image_rows_escape_late_legacy_cascade() -> None:
    app_css = (STATIC / "app.css").read_text(encoding="utf-8")

    assert ".studio-import-intake label:not(.sh-check)" in app_css
    assert ".studio-import-intake [data-import-source-row] label:not(.sh-check)" in app_css
    assert (
        '.studio-import-intake [data-import-source-row] .sh-check '
        'input[type="checkbox"] { width: 16px; min-width: 16px; }'
    ) in app_css
    assert ".studio-import-intake label {" not in app_css
    assert ".studio-import-intake [data-import-source-row] label {" not in app_css

    assert ".studio-image-question-list li:not(.sh-row)" in app_css
    assert ".studio-image-question-list li.is-overridden:not(.sh-row)" in app_css
    assert ".studio-image-question-list li {" not in app_css
    assert ".studio-image-question-list li.is-overridden {" not in app_css


def test_targeted_visual_acceptance_layouts_are_scoped_and_responsive() -> None:
    app_css = static_source("app.css")
    library_css = static_source("public_quiz_library.css")
    player_css = static_source("public_quiz.css")

    assert ".exam-group {\n  flex: 1 1 100%;\n  min-width: 0;\n  width: 100%;\n}" in app_css
    exam_toggle_rule = app_css.split(
        "button.course-toggle,\nbutton.exam-toggle {", 1
    )[1].split("}", 1)[0]
    assert "width: 100%;" in exam_toggle_rule
    assert "min-height: var(--control-h);" in exam_toggle_rule
    assert "justify-content: flex-start;" in exam_toggle_rule
    lecture_row_rule = app_css.split(".lecture-row {", 1)[1].split("}", 1)[0]
    assert "min-height: var(--control-h);" in lecture_row_rule
    review_link_rule = app_css.split(".review-lecture-link {", 1)[1].split("}", 1)[0]
    assert "min-height: var(--control-h);" in review_link_rule
    assert "grid-template-columns: minmax(0, 1fr) auto;" in app_css
    assert ".provider-card-heading > :first-child { min-width: 0; }" in app_css
    provider_title_rule = app_css.split(".provider-card-heading h2,", 1)[1].split("}", 1)[0]
    assert "overflow-wrap: normal;" in provider_title_rule
    assert "word-break: normal;" in provider_title_rule
    assert "container-type: inline-size;" in app_css
    assert (
        "@container (max-width: 22rem) {\n  .provider-card-heading {\n"
        "    grid-template-columns: minmax(0, 1fr);\n  }\n}"
        in app_css
    )
    assert (
        "@media (max-width: 1040px) {\n  .provider-grid {\n"
        "    grid-template-columns: repeat(2, minmax(0, 1fr));\n  }\n}"
        in app_css
    )
    assert (
        "@media (max-width: 430px) {\n  .count-pill, .exam-count { display: none; }\n"
        "  .provider-card-heading { grid-template-columns: minmax(0, 1fr); }"
        in app_css
    )
    assert (
        ".provider-card-status {\n  display: flex;\n  flex-direction: column;\n"
        "  align-items: flex-end;\n  min-width: 0;\n  max-width: 100%;"
        in app_css
    )

    assert (
        ".lecture-list {\n  display: grid;\n  gap: var(--sp-2);\n"
        "  padding: 0 var(--sp-4) var(--sp-2);\n}"
        in library_css
    )
    option_rule = player_css.split(".quiz-answer-row.sh-option .quiz-answer {", 1)[1].split(
        "}", 1
    )[0]
    assert "border-radius: inherit;" in option_rule

    assert "[data-workflow-panel] {\n  display: grid;\n  gap: var(--sp-6);" in app_css
    assert (
        "[data-workflow-panel] .studio-runner form {\n  display: grid;\n  gap: var(--sp-4);"
        in app_css
    )
