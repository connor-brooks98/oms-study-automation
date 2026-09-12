import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from fastapi.staticfiles import StaticFiles
from playwright.sync_api import sync_playwright
from tests.study_progress.test_sessions import sessions
from tests.study_progress.test_blocks import blocks
from tests.study_progress.test_routes import client_for
from tests.study_progress.test_tags import service_for, FakeClient
from oms_hub.study_progress.taxonomy import TAXONOMIES

ROOT = Path.cwd()
PLAYER = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "src/oms_hub/web/static/public_quiz.js"
with TemporaryDirectory(prefix="c4-ui-") as tmp:
    fixture = sessions.__wrapped__(Path(tmp))
    original = next(fixture)
    block_service, bank, database, publications = blocks.__wrapped__(original)
    client, app = client_for(block_service.sessions, bank, [publications["quiz"]])
    fake = FakeClient()
    topics = service_for(block_service, Path(tmp), fake)
    app.state.study_block_service = block_service
    app.state.study_topic_service = topics
    app.mount("/static", StaticFiles(directory=ROOT / "src/oms_hub/web/static"), name="static")
    with client, sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            headless=True,
        )
        context = browser.new_context(viewport={"width": 1440, "height": 1100})
        context.add_cookies(
            [
                {
                    "name": "study_hub_csrf",
                    "value": client.cookies.get("study_hub_csrf"),
                    "url": "http://testserver",
                }
            ]
        )
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        def route(request_route):
            request = request_route.request
            parsed = urlsplit(request.url)
            if parsed.netloc != "testserver":
                request_route.abort()
                return
            if parsed.path == "/public/quizzes/assets/player.js":
                request_route.fulfill(
                    status=200, body=PLAYER.read_text(), content_type="text/javascript"
                )
                return
            response = client.request(
                request.method,
                parsed.path + ("?" + parsed.query if parsed.query else ""),
                content=request.post_data_buffer,
                headers=request.headers,
                follow_redirects=False,
            )
            headers = {
                k: v
                for k, v in response.headers.items()
                if k.lower() not in ("content-encoding", "content-length")
            }
            request_route.fulfill(
                status=response.status_code, body=response.content, headers=headers
            )

        context.route("**/*", route)
        page.goto("http://testserver/study/blocks")
        page.locator("#block-course").select_option("neuro")
        page.get_by_label("Exam 1", exact=True).check()
        page.get_by_label("Exam 2", exact=True).check()
        page.locator("#block-count").fill("2")
        page.get_by_role("button", name="Preview block").click()
        page.get_by_role("button", name="Start 2-question block").wait_for()
        page.screenshot(path="/tmp/c4-block-preview.png", full_page=True)
        review_url = page.get_by_role(
            "link", name="Review categories", exact=True
        ).first.get_attribute("href")
        page.get_by_role("button", name="Start 2-question block").click()
        page.locator(".quiz-source-label").wait_for()
        assert "Secret objective" not in page.locator("#quiz-player").inner_text()
        first_label = page.locator(".quiz-source-label").inner_text()
        choice = "c2" if "Exam 2" in first_label else "c1"
        page.screenshot(path="/tmp/c4-player-source.png", full_page=True)
        session_url = page.url
        page.locator('[data-focus-key="answer-' + choice + '"]').click()
        page.get_by_role("button", name="Submit Answer", exact=True).click()
        page.get_by_role("heading", name="Correct", exact=True).wait_for()
        page.reload()
        page.locator(".quiz-source-label").wait_for()
        assert page.locator(".quiz-source-label").inner_text() != first_label
        restored = client.get(urlsplit(session_url).path + "/content").json()
        assert restored["questions"][0]["feedback"]["correct"]
        assert len(bank.iter_attempts(learner_id="owner")) == 1
        page.goto("http://testserver" + review_url)
        category = TAXONOMIES[0].categories[0]
        source_context = topics.context("owner", review_url.rsplit("/", 1)[-1])
        fake.text = json.dumps(
            {
                "tags": [
                    {
                        "canonical_id": category.canonical_id,
                        "evidence_quote": source_context.evidence["slices"]["stem"],
                        "confidence": 0.8,
                    }
                ]
            }
        )
        page.locator("form details").first.locator("summary").click()
        page.get_by_label(category.label, exact=True).check()
        page.get_by_role("button", name="Save reviewed categories").click()
        assert len(bank.iter_attempts(learner_id="owner")) == 1
        page.get_by_role("button", name="Prepare suggestions").click()
        assert fake.calls == []
        page.get_by_role("button", name="Generate suggestions").click()
        page.get_by_role("heading", name="Pending suggestions").wait_for()
        page.screenshot(path="/tmp/c4-pending-review.png", full_page=True)
        page.get_by_label(category.label, exact=True).check()
        page.get_by_role("button", name="Accept selected suggestions").click()
        page.wait_for_url("**/study/blocks/questions/**")
        page.get_by_role("button", name="Save reviewed categories").wait_for()
        assert len(fake.calls) == 1
        topics.client = None
        page.reload()
        page.get_by_text("Model suggestions are unavailable", exact=False).wait_for()
        page.goto(
            "http://testserver/study/blocks?course=neuro&exam=1&exam=2&topic="
            + category.canonical_id
        )
        page.get_by_role("button", name="Start 1-question block").wait_for()
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path="/tmp/c4-block-mobile.png", full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert not errors, errors
        assert len(bank.iter_attempts(learner_id="owner")) == 1
        print(
            "PASS: cumulative preview/start, source label, answer and resume, manual review/filter, fake-only suggestions/accept, unavailable model, mobile overflow; zero JS errors"
        )
        browser.close()
    try:
        next(fixture)
    except StopIteration:
        pass
