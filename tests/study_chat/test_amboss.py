import pytest

from oms_hub.study_chat.amboss import AmbossReference, AmbossUnavailable
from oms_hub.study_chat.service import ChatService

from .test_repository import request
from .test_service import FakeClient


def test_reference_access_is_not_assumed():
    with pytest.raises(AmbossUnavailable, match="has not been configured"):
        AmbossReference().search("Explain this mechanism")


def test_reference_mode_is_unavailable_without_gpt_fallback(setup):
    repo, _, _ = setup
    client = FakeClient()
    answer = ChatService(repo, client, model="chosen").answer(request(repo, "medical_reference"))
    assert answer.status == "unavailable" and not answer.citation_ids
    assert "has not used AMBOSS" in answer.text
    assert client.requests == []
