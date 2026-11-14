import io
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pytest

import tender_assistant


CONFIG = {"DASHSCOPE_MODEL": "test", "DASHSCOPE_BASE_URL": "https://example.invalid",
          "DASHSCOPE_API_KEY": "test"}


def test_transient_disconnect_recovers(monkeypatch):
    success = io.BytesIO(b'{"choices":[{"message":{"content":"{}"}}]}')
    request = Mock(side_effect=[URLError("connection reset"), success])
    monkeypatch.setattr(tender_assistant, "urlopen", request)
    monkeypatch.setattr(tender_assistant, "sleep", Mock())
    assert tender_assistant.agent_messages_json([], CONFIG, 50) == {}
    assert request.call_count == 2


def test_authentication_error_is_not_retried(monkeypatch):
    request = Mock(side_effect=HTTPError("https://example.invalid", 401, "Unauthorized", None, None))
    monkeypatch.setattr(tender_assistant, "urlopen", request)
    with pytest.raises(HTTPError):
        tender_assistant.agent_messages_json([], CONFIG, 50)
    assert request.call_count == 1
