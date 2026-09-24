"""FR-007 mining: mock extraction, malformed retry, truncation halving,
>25-item halving, depth cap, provider retries (FR-006)."""

import json

import pytest

from app.core.model_client import (
    MockModelClient,
    ModelResponse,
    ProviderRateLimited,
    ProviderUnavailable,
)
from app.pipeline.chunking import Chunk
from app.pipeline.mine import mine_chunks


def _chunk(text: str, index: int = 0, page_start: int = 1, page_end: int = 1) -> Chunk:
    return Chunk(
        index=index, section_heading="Section L", parent_section="Section L",
        page_start=page_start, page_end=page_end, text=text, context_summary="",
    )


def _no_sleep(_seconds: float) -> None:
    return None


def test_mock_client_extracts_modal_lines():
    chunk = _chunk("L.1 The offeror shall submit a technical volume.\nThis is narrative prose only.")
    result = mine_chunks(MockModelClient(), [chunk], sleep_fn=_no_sleep)
    assert len(result.items) == 2
    binding = [i for i in result.items if i.is_requirement]
    assert len(binding) == 1
    assert binding[0].clause_id == "L.1"
    assert binding[0].page == 1
    assert result.calls and result.calls[0].finish_reason == "stop"
    assert result.failed_chunks == 0


def test_mock_client_respects_page_markers_inside_multi_page_chunk():
    chunk = _chunk(
        "[[PAGE 1]]\nL.1 The offeror shall submit volume one.\n"
        "[[PAGE 2]]\nL.2 The offeror must submit volume two.",
        page_start=1,
        page_end=2,
    )
    result = mine_chunks(MockModelClient(), [chunk], sleep_fn=_no_sleep)
    assert [(item.clause_id, item.page) for item in result.items] == [
        ("L.1", 1),
        ("L.2", 2),
    ]


class _MalformedOnceClient:
    def __init__(self):
        self.calls = 0

    def complete(self, *, system, user, max_tokens, json_schema=None):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(text="{not json", tokens_in=10, tokens_out=10)
        return ModelResponse(
            text=json.dumps({"requirements": [{
                "clause_id": "L.9", "section": "Section L", "page": 1,
                "text": "The offeror shall comply.", "excerpt": "shall comply",
                "is_requirement": True}]}),
            tokens_in=10, tokens_out=20,
        )


def test_malformed_output_retries_once_then_succeeds():
    client = _MalformedOnceClient()
    result = mine_chunks(client, [_chunk("anything")], sleep_fn=_no_sleep)
    assert client.calls == 2
    assert result.failed_chunks == 0
    assert result.items[0].clause_id == "L.9"


class _AlwaysMalformedClient:
    def complete(self, *, system, user, max_tokens, json_schema=None):
        return ModelResponse(text="nope", tokens_in=1, tokens_out=1)


def test_malformed_after_retry_counts_failed_chunk_and_completes():
    result = mine_chunks(_AlwaysMalformedClient(), [_chunk("a"), _chunk("b", index=1)], sleep_fn=_no_sleep)
    assert result.failed_chunks == 2
    assert result.items == []


class _TruncatingClient:
    def __init__(self):
        self.calls = 0

    def complete(self, *, system, user, max_tokens, json_schema=None):
        self.calls += 1
        return ModelResponse(text="{}", tokens_in=1, tokens_out=max_tokens, finish_reason="length")


def test_truncation_halves_until_depth_cap_then_counts():
    client = _TruncatingClient()
    # max_tokens=600 keeps the dynamic-sizing pre-check (est ~500) from
    # firing, isolating the truncation-halving path.
    result = mine_chunks(client, [_chunk("x" * 2000)], max_tokens=600, sleep_fn=_no_sleep)
    # depth 0 (1 call) -> halves (2 calls at depth 1) -> halves (4 calls at
    # depth 2) -> depth cap reached: 4 failed sub-chunks
    assert result.failed_chunks == 4
    assert client.calls == 7


class _ManyItemsClient:
    def __init__(self):
        self.calls = 0

    def complete(self, *, system, user, max_tokens, json_schema=None):
        self.calls += 1
        items = [
            {"clause_id": f"L.{i}", "section": "Section L", "page": 1,
             "text": f"The offeror shall do thing {i}.", "excerpt": f"thing {i}",
             "is_requirement": True}
            for i in range(30)
        ]
        return ModelResponse(text=json.dumps({"requirements": items}), tokens_in=5, tokens_out=500)


def test_more_than_25_requirements_halves_chunk():
    client = _ManyItemsClient()
    result = mine_chunks(client, [_chunk("dense section")], sleep_fn=_no_sleep)
    # 30 items at depth 0 (1 call) -> halve (2 calls at depth 1) -> halve
    # (4 calls at depth 2) -> accepted (depth cap stops further splitting)
    assert client.calls == 7
    assert result.failed_chunks == 0
    assert len(result.items) == 4 * 30


class _FlakyClient:
    def __init__(self, fail_times: int):
        self.calls = 0
        self.fail_times = fail_times

    def complete(self, *, system, user, max_tokens, json_schema=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ProviderRateLimited("429")
        return ModelResponse(
            text=json.dumps({"requirements": []}), tokens_in=1, tokens_out=1,
        )


def test_provider_retries_succeed_within_three():
    client = _FlakyClient(fail_times=2)
    result = mine_chunks(client, [_chunk("x")], sleep_fn=_no_sleep)
    assert client.calls == 3


def test_provider_retries_exhausted_raises():
    client = _FlakyClient(fail_times=99)
    with pytest.raises(ProviderUnavailable):
        mine_chunks(client, [_chunk("x")], sleep_fn=_no_sleep)
    assert client.calls == 3  # three backed-off attempts, no more


def test_permanent_4xx_fails_fast_no_retry():
    """FR-006 retries target 429/5xx ONLY. A permanent 4xx (401 bad key,
    400 bad request) must fail on the first attempt without burning the
    retry budget or sleeping — the error cannot self-heal."""
    from app.core.model_client import ProviderClientError

    class _BadKeyClient:
        def __init__(self):
            self.calls = 0
            self.slept = []

        def complete(self, *, system, user, max_tokens, json_schema=None):
            self.calls += 1
            raise ProviderClientError("openai client error 401: Unauthorized")

    tracker = _BadKeyClient()
    with pytest.raises(ProviderClientError):
        mine_chunks(tracker, [_chunk("x")], sleep_fn=tracker.slept.append)
    assert tracker.calls == 1  # no retry — permanent error
    assert tracker.slept == []  # no backoff sleep either


def test_page_cites_clamped_to_chunk_span():
    class _OutOfRangeClient:
        def complete(self, *, system, user, max_tokens, json_schema=None):
            return ModelResponse(
                text=json.dumps({"requirements": [{
                    "clause_id": "L.1", "section": "Section L", "page": 99,
                    "text": "The offeror shall comply fully.", "excerpt": "x",
                    "is_requirement": True}]}),
                tokens_in=1, tokens_out=1,
            )

    result = mine_chunks(_OutOfRangeClient(), [_chunk("x", page_start=3, page_end=5)], sleep_fn=_no_sleep)
    assert 3 <= result.items[0].page <= 5
