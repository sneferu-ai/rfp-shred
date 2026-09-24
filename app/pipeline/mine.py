"""Requirement mining (FR-007).

Each chunk goes to the model with the strict JSON schema. Safety behavior:
- >25 requirements in one chunk  -> halve, process as two sub-chunks
- estimated output > MODEL_MAX_TOKENS -> halve preemptively
- finish_reason == 'length' (or completion near the cap) -> halve and
  re-process; maximum re-chunk depth 2, then the sub-chunk fails and is
  counted while the run completes
- malformed output (non-truncation) retries its chunk once, then that chunk
  fails and is counted while the run completes
Raw model output is returned for persistence to ``raw_model_outputs`` before
any transformation.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from app.core.model_client import (
    ModelClient,
    ProviderClientError,
    ProviderRateLimited,
    ProviderUnavailable,
)
from app.pipeline.chunking import Chunk, halve_chunk
from app.pipeline.prompts import (
    EXTRACTION_SCHEMA,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    USER_PROMPT_TEMPLATE,
)

log = logging.getLogger(__name__)

MAX_REQUIREMENTS_PER_CHUNK = 25
MAX_RECHUNK_DEPTH = 2
TRUNCATION_TOKEN_MARGIN = 100
PROVIDER_RETRIES = 3


class ChunkItem(BaseModel):
    clause_id: str
    section: str
    page: int
    text: str
    excerpt: str
    is_requirement: bool


class ChunkExtraction(BaseModel):
    requirements: list[ChunkItem]


@dataclass
class CallRecord:
    chunk_index: int
    raw_json: dict[str, Any]
    tokens_in: int
    tokens_out: int
    finish_reason: str | None
    prompt_version: str = PROMPT_VERSION


@dataclass
class MinedItem:
    clause_id: str
    section: str
    page: int
    text: str
    excerpt: str
    is_requirement: bool
    chunk_index: int
    doc_order: int  # (page, chunk, position) flattened for deterministic seq


@dataclass
class MineResult:
    items: list[MinedItem] = field(default_factory=list)
    calls: list[CallRecord] = field(default_factory=list)
    failed_chunks: int = 0


def _estimate_requirements(text: str) -> int:
    lowered = text.lower()
    return max(1, lowered.count("shall") + lowered.count("must") + lowered.count("required"))


def _validate_payload(payload: dict[str, Any]) -> ChunkExtraction | None:
    try:
        return ChunkExtraction.model_validate(payload)
    except ValidationError:
        return None


class ChunkFailed(RuntimeError):
    pass


def _call_with_retries(
    client: ModelClient,
    *,
    system: str,
    user: str,
    max_tokens: int,
    sleep_fn: Callable[[float], None] = time.sleep,
):
    """FR-006: three backed-off retries on provider 429/5xx, targeting the
    configured provider only (no automatic failover — A-023). A permanent
    4xx client error (401/403/400 — ``ProviderClientError``) is NOT retryable
    and propagates immediately so the run fails fast with cause instead of
    burning the retry budget on an error that cannot self-heal."""
    last_exc: Exception | None = None
    for attempt in range(PROVIDER_RETRIES):
        try:
            return client.complete(
                system=system, user=user, max_tokens=max_tokens, json_schema=EXTRACTION_SCHEMA
            )
        except ProviderClientError:
            raise  # permanent 4xx (non-429) — fail fast, no retry, no sleep
        except (ProviderRateLimited, ProviderUnavailable) as exc:
            last_exc = exc
            if attempt + 1 < PROVIDER_RETRIES:  # no pointless sleep before raising
                sleep_fn(min(2.0 * (attempt + 1), 8.0))
    raise ProviderUnavailable(str(last_exc))


def _process_chunk(
    client: ModelClient,
    chunk: Chunk,
    *,
    max_tokens: int,
    depth: int,
    result: MineResult,
    sleep_fn: Callable[[float], None],
) -> None:
    # Dynamic chunk sizing: estimated output would exceed the cap -> halve
    # preemptively.
    est_tokens = _estimate_requirements(chunk.text) * 500
    if est_tokens > max_tokens and depth < MAX_RECHUNK_DEPTH:
        for sub in halve_chunk(chunk):
            _process_chunk(client, sub, max_tokens=max_tokens, depth=depth + 1, result=result, sleep_fn=sleep_fn)
        return

    user = USER_PROMPT_TEMPLATE.format(
        section_heading=chunk.section_heading,
        parent_section=chunk.parent_section,
        page_start=chunk.page_start,
        page_end=chunk.page_end,
        section_context_summary=chunk.context_summary,
        chunk_text=chunk.text,
    )

    parsed: ChunkExtraction | None = None
    for attempt in range(2):  # malformed output retries the chunk once
        response = _call_with_retries(
            client, system=SYSTEM_PROMPT, user=user, max_tokens=max_tokens, sleep_fn=sleep_fn
        )
        truncated = response.finish_reason == "length" or response.tokens_out >= max_tokens - TRUNCATION_TOKEN_MARGIN
        if truncated:
            if depth < MAX_RECHUNK_DEPTH:
                for sub in halve_chunk(chunk):
                    _process_chunk(client, sub, max_tokens=max_tokens, depth=depth + 1, result=result, sleep_fn=sleep_fn)
                return
            result.failed_chunks += 1  # depth exceeded: counted, run completes
            return
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            parsed = _validate_payload(payload)
        if parsed is not None:
            break
        log.info("chunk %s malformed model output (attempt %s)", chunk.index, attempt + 1)
    if parsed is None:
        result.failed_chunks += 1  # malformed after retry: counted, run completes
        return

    # >25 requirements -> halve and process as two sub-chunks.
    if len(parsed.requirements) > MAX_REQUIREMENTS_PER_CHUNK and depth < MAX_RECHUNK_DEPTH:
        for sub in halve_chunk(chunk):
            _process_chunk(client, sub, max_tokens=max_tokens, depth=depth + 1, result=result, sleep_fn=sleep_fn)
        return

    result.calls.append(
        CallRecord(
            chunk_index=chunk.index,
            raw_json={"requirements": [item.model_dump() for item in parsed.requirements]},
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            finish_reason=response.finish_reason,
        )
    )

    for pos, item in enumerate(parsed.requirements):
        page = item.page if item.page >= 1 else chunk.page_start
        if page < chunk.page_start or page > max(chunk.page_end, chunk.page_start):
            # keep cites inside the chunk's own page span
            page = min(max(page, chunk.page_start), max(chunk.page_end, chunk.page_start))
        result.items.append(
            MinedItem(
                clause_id=item.clause_id,
                section=item.section,
                page=page,
                text=item.text,
                excerpt=item.excerpt,
                is_requirement=item.is_requirement,
                chunk_index=chunk.index,
                doc_order=len(result.items),
            )
        )


def mine_chunks(
    client: ModelClient,
    chunks: list[Chunk],
    *,
    max_tokens: int = 8192,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> MineResult:
    result = MineResult()
    for chunk in chunks:
        _process_chunk(client, chunk, max_tokens=max_tokens, depth=0, result=result, sleep_fn=sleep_fn)
    return result
