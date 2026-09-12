from oms_hub.study_progress.taxonomy import TAXONOMIES

from .test_blocks import blocks  # noqa: F401
from .test_routes import client_for
from .test_sessions import sessions  # noqa: F401
from .test_tags import FakeClient, service_for


def test_block_and_review_forms_are_private_explicit_and_csrf_checked(blocks, tmp_path):  # noqa: F811
    service, bank, _, publications = blocks
    client, app = client_for(service.sessions, bank, [publications["quiz"]])
    model = FakeClient()
    topics = service_for(service, tmp_path, model)
    app.state.study_block_service = service
    app.state.study_topic_service = topics
    with client:
        page = client.get("/study/blocks?course=neuro&exam=1&exam=2&count=2")
        assert page.status_code == 200 and "2 questions selected" in page.text
        assert page.headers["cache-control"] == "private, no-store"
        keys = [
            q.key.question_id for q in service.catalog("owner").questions if q.course == "neuro"
        ]
        token = client.headers.pop("X-CSRF-Token")
        payload = {"course": "neuro", "exam_numbers": [1, 2], "count": 2, "selected_keys": keys}
        assert client.post("/study/blocks", json=payload).status_code == 403
        started = client.post(
            "/study/blocks/start",
            data={
                "course": "neuro",
                "exam": [1, 2],
                "selected_key": keys,
                "count": 2,
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert started.status_code == 303
        content = client.get(started.headers["location"] + "/content")
        assert len(content.json()["questions"]) == 2
        assert "Secret objective" not in content.text and "correct_choice_id" not in content.text
        source = "/study/blocks/questions/" + keys[0]
        context = topics.context("owner", keys[0])
        category = TAXONOMIES[0].categories[0].canonical_id
        review = {"content_hash": context.content_hash, "category": category, "csrf_token": token}
        assert (
            client.post(source + "/topics", data=review | {"csrf_token": "bad"}).status_code == 403
        )
        assert (
            client.post(source + "/topics", data=review, follow_redirects=False).status_code == 303
        )
        assert bank.get_question(context.candidate.key).topics[0].method == "user"
        assert (
            "1 questions selected"
            in client.get(
                "/study/blocks", params={"course": "neuro", "exam": 1, "topic": category}
            ).text
        )
        prepared = client.post(
            source + "/suggestions", data={"csrf_token": token}, follow_redirects=False
        )
        path = prepared.headers["location"]
        assert model.calls == [] and "Generate suggestions" in client.get(path).text
        assert (
            client.post(
                path + "/run", data={"csrf_token": token}, follow_redirects=False
            ).status_code
            == 303
        )
        completed = client.get(path)
        assert "Pending suggestions" in completed.text and "raw_response_text" not in completed.text
        assert (
            client.post(
                path + "/accept",
                data={"csrf_token": token, "category": category},
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert bank.get_question(context.candidate.key).topics[0].method == "model"
        assert bank.iter_attempts(learner_id="owner") == ()
        topics.client = None
        assert "Model suggestions are unavailable" in client.get(source).text
        assert client.post(source + "/suggestions", data={"csrf_token": token}).status_code == 503
        app.state.test_owner = "other"
        assert client.get(path).status_code == 403 and client.get(source).status_code == 403
        app.state.test_owner = None
        assert client.get("/study/blocks").status_code == 401
        assert client.post(path + "/run", data={"csrf_token": token}).status_code == 401
