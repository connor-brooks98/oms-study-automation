from __future__ import annotations

import json
import os
from uuid import uuid4

from playwright.sync_api import Page, sync_playwright


def _assert_restored_import_row(page: Page, title: str) -> None:
    row = page.locator("[data-import-source-row]", has_text=title)
    row.wait_for(state="visible")
    assert row.count() == 1
    assert row.locator("[data-import-row-role]").input_value() == "questions"
    notebook = row.locator("[data-import-row-notebook]")
    assert notebook.is_disabled()
    assert not notebook.is_checked()


def main() -> None:
    base_url = os.environ.get("OMS_HUB_TEST_BASE_URL", "http://127.0.0.1:8876").rstrip("/")
    subject = os.environ.get("OMS_HUB_TEST_SUBJECT", "Neuro")
    exam_number = os.environ.get("OMS_HUB_TEST_EXAM", "1")
    browser_channel = os.environ.get("OMS_HUB_BROWSER_CHANNEL", "chrome")
    title = f"F22 browser source {uuid4().hex[:12]}"
    studio_url = (
        f"{base_url}/studio?subject={subject.lower()}&exam={exam_number}&workflow=import"
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=browser_channel, headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(studio_url, wait_until="networkidle")
            assert page.locator("[data-studio-course]").input_value() == subject
            assert page.locator("[data-studio-exam]").input_value() == exam_number
            assert page.locator('[data-workflow-tab="import"]').get_attribute(
                "aria-pressed"
            ) == "true"

            form = page.locator(
                '[data-import-source-form][data-import-source-type="text"]'
            )
            form.locator('[name="title"]').fill(title)
            form.locator('[name="text"]').fill("Question 1: Which choice is correct?")
            form.locator('[name="role"]').select_option("questions")
            assert form.locator('[name="attach_to_notebook"]').is_disabled()

            with page.expect_response(
                lambda response: response.url.endswith("/studio/import/sources/text")
                and response.request.method == "POST"
            ) as response_info:
                form.locator('button[type="submit"]').click()
            response = response_info.value
            assert response.status == 202
            request = response.request
            content_type = request.headers.get("content-type", "")
            assert content_type.startswith("multipart/form-data; boundary=")
            posted = request.post_data or ""
            for expected in (
                title,
                "Question 1: Which choice is correct?",
                'name="role"',
                "questions",
                'name="attach_to_notebook"',
                "false",
                'name="subject"',
                subject,
                'name="exam_number"',
                exam_number,
            ):
                assert expected in posted
            _assert_restored_import_row(page, title)

            page.reload(wait_until="networkidle")
            _assert_restored_import_row(page, title)
            assert page.locator("[data-studio-course]").input_value() == subject
            assert page.locator("[data-studio-exam]").input_value() == exam_number

            page.goto(f"{base_url}/health/live", wait_until="domcontentloaded")
            page.go_back(wait_until="networkidle")
            _assert_restored_import_row(page, title)

            page.goto(studio_url, wait_until="networkidle")
            _assert_restored_import_row(page, title)
            assert page.locator('[data-workflow-tab="import"]').get_attribute(
                "aria-pressed"
            ) == "true"

            evidence = {
                "base_url": base_url,
                "request_content_type": content_type,
                "request_path": request.url,
                "source_title": title,
                "scope": {"subject": subject, "exam_number": exam_number},
                "safe_defaults": {
                    "role": "questions",
                    "attach_to_notebook": False,
                },
                "reload": "pass",
                "navigate_away_back": "pass",
                "direct_return": "pass",
            }
            print(json.dumps(evidence, sort_keys=True))
            print("REAL_STUDIO_F22_BROWSER_PASS")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
