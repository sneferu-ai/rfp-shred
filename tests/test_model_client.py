"""FR-011 / FR-007: the strict JSON schema reaches the wire on BOTH reference
adapters (no network — the HTTP layer is stubbed at ``_post``)."""

import json

from app.core.model_client import AnthropicAdapter, OpenAIAdapter
from app.pipeline.prompts import EXTRACTION_SCHEMA


class _StubOpenAI(OpenAIAdapter):
    def __init__(self):
        super().__init__(api_key="k", model="m", base_url="http://stub")
        self.sent_body = None

    def _post(self, url, headers, body):
        self.sent_body = body
        return {
            "choices": [{"message": {"content": '{"requirements": []}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        }


class _StubAnthropic(AnthropicAdapter):
    def __init__(self, payload):
        super().__init__(api_key="k", model="m", base_url="http://stub")
        self.sent_body = None
        self._payload = payload

    def _post(self, url, headers, body):
        self.sent_body = body
        return self._payload


def test_openai_adapter_sends_strict_json_schema():
    client = _StubOpenAI()
    resp = client.complete(system="s", user="u", max_tokens=100, json_schema=EXTRACTION_SCHEMA)
    fmt = client.sent_body["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["schema"] == EXTRACTION_SCHEMA
    assert fmt["json_schema"]["strict"] is True
    assert resp.text == '{"requirements": []}'
    assert resp.tokens_in == 3 and resp.tokens_out == 2


def test_anthropic_adapter_forces_tool_with_strict_schema():
    """Anthropic has no response_format; the strict schema must ride the
    forced-tool mechanism, and the tool_use input IS the JSON payload."""
    tool_input = {"requirements": [
        {"clause_id": "L.1", "section": "Section L", "page": 1,
         "text": "The offeror shall comply.", "excerpt": "shall comply",
         "is_requirement": True}
    ]}
    payload = {
        "content": [{"type": "tool_use", "name": "extraction", "input": tool_input}],
        "usage": {"input_tokens": 5, "output_tokens": 7},
        "stop_reason": "tool_use",
    }
    client = _StubAnthropic(payload)
    resp = client.complete(system="s", user="u", max_tokens=100, json_schema=EXTRACTION_SCHEMA)

    tools = client.sent_body["tools"]
    assert tools[0]["input_schema"] == EXTRACTION_SCHEMA
    assert client.sent_body["tool_choice"] == {"type": "tool", "name": "extraction"}
    # the tool_use input round-trips back to JSON text for the mine layer
    assert json.loads(resp.text) == tool_input
    assert resp.tokens_in == 5 and resp.tokens_out == 7
    assert resp.finish_reason == "stop"


def test_anthropic_adapter_without_schema_sends_no_tools():
    """IFF: absent a schema the request body is byte-equivalent to legacy —
    plain text blocks still concatenate."""
    payload = {
        "content": [{"type": "text", "text": "hello "}, {"type": "text", "text": "world"}],
        "usage": {},
        "stop_reason": "end_turn",
    }
    client = _StubAnthropic(payload)
    resp = client.complete(system="s", user="u", max_tokens=100)
    assert "tools" not in client.sent_body
    assert "tool_choice" not in client.sent_body
    assert resp.text == "hello world"
    assert resp.finish_reason == "stop"
