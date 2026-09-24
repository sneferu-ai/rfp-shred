"""Provider-neutral model client (FR-011).

All model calls pass through the ``ModelClient`` interface selected by
``MODEL_PROVIDER``/``MODEL_NAME``. Two reference adapters ship (OpenAI and
Anthropic) plus a deterministic ``mock`` adapter used by the dev stack and
the test-suite. Nothing outside an adapter knows which vendor serves the
request; swapping provider is configuration only. There is no automatic
runtime failover (A-023).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

from app.core.config import Settings


class ProviderUnavailable(RuntimeError):
    """The configured provider is unreachable (timeout / 5xx). FR-043 input.

    Retryable: FR-006 backs off and retries this (and the 429 subclass)."""


class ProviderRateLimited(ProviderUnavailable):
    """429 from the provider — FR-006 backed-off retries target this."""


class ProviderClientError(ProviderUnavailable):
    """A permanent 4xx provider error other than 429 (e.g. 401/403 bad key,
    400 bad request). FR-006 retries target 429/5xx ONLY — a permanent client
    error is not retryable, so ``_call_with_retries`` re-raises it immediately
    instead of burning the retry budget on an error that cannot self-heal.

    Subclasses ``ProviderUnavailable`` so ``run_extraction`` still maps it to
    ``ExtractionFailed("model provider error: …")`` for a consistent S5 cause
    string."""


@dataclass
class ModelResponse:
    text: str
    tokens_in: int
    tokens_out: int
    finish_reason: str = "stop"


class ModelClient(Protocol):
    def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int,
        json_schema: dict[str, Any] | None = None,
    ) -> ModelResponse: ...


# ---------------------------------------------------------------------------
# Mock adapter (dev/test): deterministic extraction from the chunk text.
# ---------------------------------------------------------------------------

_MODAL_RE = re.compile(
    r"\b(shall|must|will|is required to|is responsible for|are required to)\b", re.I
)
_CLAUSE_RE = re.compile(r"\b([LM]\s*[.\-]?\s*\d+(?:[.\-]\s*\d+)*(?:[.\-]?\s*[a-z])?)\b")
_PAGE_MARKER_RE = re.compile(r"^\[\[PAGE\s+(\d+)\]\]$")


class MockModelClient:
    """Deterministic extractor: finds requirement-looking lines in the chunk.

    A line containing a modal verb becomes ``is_requirement=true``; other
    substantive lines become ``false`` so the noise filter has something to
    route. Excerpts are verbatim slices of the chunk so the FR-008 citation
    audit can genuinely verify them against the page text.
    """

    def complete(self, *, system: str, user: str, max_tokens: int, json_schema=None) -> ModelResponse:
        content = user.split("Content:\n", 1)[-1]
        # strip the trailing extraction instruction, not document text
        content = content.split("\n\nExtract all candidate", 1)[0]
        page_start = 1
        m = re.search(r"Pages:\s*(\d+)-(\d+)", user)
        if m:
            page_start = int(m.group(1))
        section = "Section"
        sm = re.search(r"^Section:\s*(.+)$", user, re.M)
        if sm:
            section = sm.group(1).strip()

        items: list[dict[str, Any]] = []
        current_page = page_start
        for raw_line in content.splitlines():
            line = raw_line.strip()
            marker = _PAGE_MARKER_RE.match(line)
            if marker:
                current_page = int(marker.group(1))
                continue
            if len(line) < 12:
                continue
            is_req = bool(_MODAL_RE.search(line))
            clause = ""
            cm = _CLAUSE_RE.search(line)
            if cm:
                clause = cm.group(1)
            items.append(
                {
                    "clause_id": clause or f"AUTO.{len(items) + 1}",
                    "section": section,
                    "page": current_page,
                    "text": line[:2000],
                    "excerpt": line[:200],
                    "is_requirement": is_req,
                }
            )
        payload = json.dumps({"requirements": items})
        tokens_out = len(payload) // 4
        return ModelResponse(
            text=payload,
            tokens_in=len(user) // 4 + len(system) // 4,
            tokens_out=tokens_out,
            finish_reason="stop",
        )


# ---------------------------------------------------------------------------
# HTTP adapters (lazy httpx; raise ProviderUnavailable on timeout/5xx/429)
# ---------------------------------------------------------------------------

class _HttpAdapter:
    base_url: str = ""
    name: str = "http"

    def __init__(self, api_key: str, model: str, timeout_s: float = 120.0) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s

    def _post(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        import httpx

        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                resp = client.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:  # network/timeout
            raise ProviderUnavailable(f"{self.name} unreachable: {exc}") from exc
        if resp.status_code == 429:
            raise ProviderRateLimited(f"{self.name} rate limited (429)")
        if resp.status_code >= 500:
            raise ProviderUnavailable(f"{self.name} server error {resp.status_code}")
        if resp.status_code >= 400:
            # FR-006 retries target 429/5xx only — a 401/403/400 is a permanent
            # client error, not a transient one. Raising ProviderClientError
            # (still a ProviderUnavailable for the run-extraction catch) lets
            # _call_with_retries fail fast instead of burning the retry budget.
            raise ProviderClientError(
                f"{self.name} client error {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json()


class OpenAIAdapter(_HttpAdapter):
    name = "openai"

    def __init__(self, api_key: str, model: str, timeout_s: float = 120.0, base_url: str | None = None) -> None:
        super().__init__(api_key, model, timeout_s)
        self.base_url = base_url or "https://api.openai.com"

    def complete(self, *, system: str, user: str, max_tokens: int, json_schema=None) -> ModelResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "extraction", "schema": json_schema, "strict": True},
            }
        data = self._post(
            f"{self.base_url}/v1/chat/completions",
            {"Authorization": f"Bearer {self.api_key}"},
            body,
        )
        choice = (data.get("choices") or [{}])[0]
        usage = data.get("usage") or {}
        return ModelResponse(
            text=(choice.get("message") or {}).get("content") or "",
            tokens_in=int(usage.get("prompt_tokens") or 0),
            tokens_out=int(usage.get("completion_tokens") or 0),
            finish_reason=choice.get("finish_reason") or "stop",
        )


class AnthropicAdapter(_HttpAdapter):
    name = "anthropic"

    def __init__(self, api_key: str, model: str, timeout_s: float = 120.0, base_url: str | None = None) -> None:
        super().__init__(api_key, model, timeout_s)
        self.base_url = base_url or "https://api.anthropic.com"

    def complete(self, *, system: str, user: str, max_tokens: int, json_schema=None) -> ModelResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if json_schema is not None:
            # FR-007 strict-schema parity with the OpenAI adapter: Anthropic
            # has no response_format; the equivalent hard constraint is a
            # forced tool whose input_schema IS the strict schema. The
            # model's tool_use input is the JSON payload itself.
            body["tools"] = [
                {
                    "name": "extraction",
                    "description": "Emit the extraction payload conforming to the strict JSON schema.",
                    "input_schema": json_schema,
                }
            ]
            body["tool_choice"] = {"type": "tool", "name": "extraction"}
        data = self._post(
            f"{self.base_url}/v1/messages",
            {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            body,
        )
        parts = data.get("content") or []
        tool_inputs = [
            p["input"]
            for p in parts
            if isinstance(p, dict) and p.get("type") == "tool_use" and isinstance(p.get("input"), dict)
        ]
        if tool_inputs:
            text = json.dumps(tool_inputs[0])
        else:
            text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        usage = data.get("usage") or {}
        stop = data.get("stop_reason") or "end_turn"
        return ModelResponse(
            text=text,
            tokens_in=int(usage.get("input_tokens") or 0),
            tokens_out=int(usage.get("output_tokens") or 0),
            finish_reason="length" if stop == "max_tokens" else "stop",
        )


def get_model_client(settings: Settings) -> ModelClient:
    provider = (settings.model_provider or "mock").lower()
    if provider == "openai":
        return OpenAIAdapter(settings.openai_api_key, settings.model_name)
    if provider == "anthropic":
        return AnthropicAdapter(settings.anthropic_api_key, settings.model_name)
    return MockModelClient()


def estimate_cost_usd(settings: Settings, tokens_in: int, tokens_out: int) -> float:
    """FR-039 cost estimation formula."""
    return (tokens_in / 1_000_000) * settings.model_input_price_per_m + (
        tokens_out / 1_000_000
    ) * settings.model_output_price_per_m
