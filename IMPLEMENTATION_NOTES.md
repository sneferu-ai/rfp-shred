# IMPLEMENTATION_NOTES — Round 1 (initial implementation)

## What was built

A from-scratch implementation of the B13 contract for **RFP Shred** in the
canonical §6 layout (one package tree, `app/` + `scripts/` + `alembic/` +
`deploy/` + `tests/`). 120 tests, all green via `python3 -m pytest -q`.

- **Data model (§5):** all 20 tables in `app/core/models.py` — VARCHAR+CHECK
  (no PG enums), explicit `ondelete` on every FK, JSONB with a SQLite variant
  for the test-suite. Alembic `0001` creates everything from the metadata
  (no model/migration drift) and adds the PG-only append-only trigger on
  `audit_events` + the GIN index on `sam_notices.naics`, dialect-guarded.
- **Pipeline (FR-004..FR-010):** intake validation (signature sniffing on
  FULL bytes — ZIP central directories live at the tail; encrypted-PDF probe
  via pikepdf→pypdf; ZIP entry/type/count/bomb checks), page map with
  sub-PDF batch OCR and original-page mapping (pypdf fallback when
  pdfplumber/ocrmypdf are absent — OCR absences degrade to honestly-counted
  `ocr_failed` pages), section-aware chunking (6000/500, 60-cap with the
  spec's exact message, context summaries, halving), mining with the FR-007
  schema/prompts (malformed→retry→count, truncation→halve depth≤2, >25→halve,
  dynamic sizing), FR-008 citation audit (normalization incl. ligatures +
  hyphen breaks, banded Levenshtein with anchor alignment, ratio ≤0.10),
  FR-009 filter (full text preserved), transform with `clause_id`
  normalization + raw-JSONB-first persistence.
- **Queue/worker (FR-006):** DB-backed claim (`FOR UPDATE SKIP LOCKED` on PG;
  SQLite fallback), priority 10 user / 1 sweep / 0 parked, heartbeat thread
  with dedicated NullPool engine, transactional stale re-queue with attempt
  increment + partial cleanup, max-3 attempts with the exact spec cause
  string, PG advisory locks (no-op on SQLite).
- **Billing (FR-012..FR-015):** preview gate (locked rows never leave the
  query), entitlement resolution incl. ≤10-row free clause, checkout guards
  (409 zero/≤10/already), stdlib Stripe webhook signature verify,
  event-id idempotency, single-transaction entitlement+unlocked_at, plan
  lifecycle (extend/fail/delete/update), dead-letter after 3 attempts,
  dev stub checkout endpoint (refused when a real secret is set).
- **Web (S1–S11):** all surfaces server-rendered (auth, library with
  watchlist banner/amendment badges/re-process, intake, progress with
  polling, workbench with preview gate/filtered+audit-drop panels/tags/
  annotation mode, checkout handoff, account with watchlist editor + usage
  widget + deletes, ops console, health with dynamic checks).
- **Security:** argon2id (PBKDF2 fallback, self-describing hashes) — see
  Environment fallbacks; server-side 7-day sliding sessions with anonymous
  pre-auth sessions carrying the CSRF token; CSRF on every state-changing
  route except the four pre-auth bootstrap POSTs (JSON clients acquire their
  first token from the login/signup JSON body per FR-029 — this is the only
  consistent reading of the bootstrap path); uniform 404 wording; FR-016's
  exact authorization order; in-memory rate limits with Retry-After.
- **Sweep (FR-021/022/043/044):** throttled client with per-call isolation,
  tolerant parser (`unrecognized_field` warnings), watchlist matcher, jobs
  enqueued priority=1 (never synchronous), per-run cap with
  `excess_matches_skipped`, attachment size/type validation, dedup on
  re-sweep, digest email via the shared mailer (outbox fallback), provider
  health probe + queued-degradation gate, cron scheduler (sweep/cleanup/
  quality/health).
- **Maintenance:** FR-020 24h grace + 7-day failed expiry + retention_log,
  FR-039 reprioritize after month reset, SAM notice purge.
- **Scripts:** all eight (evaluate_extraction with per-subset metrics +
  lower-bound labeling + exit codes, create_staff, download_corpus with
  `--mock-corpus` generating real reportlab PDFs + real DOCX zips,
  trigger_sweep, backfill_sam, export_annotations, export_corrections,
  smoke.sh). Committed `tests/fixtures/corpus/smoke_fixture.pdf` (5 pages,
  known L/M content) + ground truth + manifest; a test pins that the
  pipeline extracts `L.1/L.2/M.1` + "shall" from it.
- **Deploy:** multi-stage Dockerfile (spec §6 + one fix: `COPY alembic.ini`,
  without which `alembic upgrade head` cannot run), compose trio, Caddyfile,
  `.env.example` with every §7 name.

## Environment fallbacks (deliberate, load-bearing for `python3 -m pytest`)

The sandbox lacks argon2-cffi, stripe, python-docx, pdfplumber, pikepdf,
email_validator. Design: production packages in `pyproject.toml` (installed
in the Docker image); code lazy-imports them and falls back honestly:

| Missing | Fallback | Production path |
|---|---|---|
| argon2-cffi | PBKDF2-HMAC-SHA256 (self-describing hash prefix) | argon2id |
| stripe | StubStripeClient + dev completion endpoint | real adapter |
| python-docx | stdlib OOXML zip writer | python-docx |
| pdfplumber | pypdf text extraction | pdfplumber |
| pikepdf | pypdf empty-password decrypt probe | pikepdf |
| EmailStr | str + server-side validation | str (unchanged) |

## Known gaps / deliberate deferrals (for later rounds)

1. **~~uv.lock is absent~~ — RESOLVED:** `uv.lock` is now committed (330 KB,
   compiled from `pyproject.toml`); the Dockerfile's `uv sync --frozen` is
   build-ready.
2. **~~htmx.min.js is a placeholder~~ — RESOLVED:** the real vendored asset
   (~50 KB) ships at `app/web/static/js/htmx.min.js` per §6. Templates carry
   htmx attributes per DIS-1; an inline vanilla-JS fallback drives the
   identical PATCH/GET calls when `window.htmx` is undefined, so behavior
   works with or without the vendored asset.
3. **Pages-done progress** in `/api/jobs/{id}` is coarse (0 → total at the
   auditing stage); per-page progress reporting is a later-round refinement.
4. **Backfill dedup across chunks** re-fetches windows; dedup happens at the
   `sam_notices` upsert + `_already_matched` level, so it's correct, just
   not network-minimal.
5. **OCR path** (`ocrmypdf` subprocess) is unit-shaped but not exercised
   end-to-end here (no tesseract binary); the sub-PDF→original-page mapping
   is coded and the absence degrades to counted `ocr_failed` pages. First
   real OCR proof is the Docker host.
6. **DOCX conversion** (LibreOffice advisory-lock path) is likewise
   host-dependent; DOCX intake/validation is fully tested, conversion is
   injectable (`pagemap.convert_docx_to_pdf` monkeypatch seam).
7. **README.md replaced** the orchestrator's problem-statement README (the
   statement lives on in `problem_statement.md`); §6 requires a product
   README at the root.
8. **In-memory rate limiter** resets on restart (FR-030's own documented
   limitation; `--workers 1` pinned in the image CMD).

## Round 2 — reviewer blocker fixes

1. **Billing plan lifecycle (blocker 1, real):** `checkout.session.completed`
   now keys plan entitlements on `obj.subscription` (the subscription id),
   not the checkout session id — `invoice.paid`, `invoice.payment_failed`,
   `customer.subscription.updated/deleted` all look plans up by subscription
   id, so the pre-fix plan was orphaned after 30 days. Dedup matches either
   ref (cross-event replay still cannot double-grant). `test_plan_lifecycle`
   now uses DISTINCT session/subscription ids + asserts `stripe_ref == sub_id`
   (it previously set session_id == sub_id, masking the bug), and a new test
   covers payment_failed + subscription.updated with distinct ids.
2. **Anthropic strict schema (blocker 2, real):** FR-007 demands the strict
   JSON schema on every chunk for every provider. Anthropic has no
   `response_format`; the adapter now sends the schema as a forced tool
   (`tools` + `tool_choice`) and serializes the `tool_use` input back to JSON
   text for the mine layer. New `tests/test_model_client.py` pins both
   adapters' wire bodies (stubbed `_post`, no network) + the no-schema IFF.
3. **Provider retries (blocker 3, disputed):** FR-006's "three backed-off
   retries on provider 429/5xx" was already implemented in
   `app/pipeline/mine.py::_call_with_retries` (the layer that encounters
   provider errors), pinned by `test_mine.py` (calls == 3). The spec
   lifecycle is retries → `failed` + manual Retry on S5 — NOT silent
   re-enqueue, which the reviewer's remedy would have contradicted. Removed
   the pointless sleep after the final attempt; added worker-level tests
   proving flaky-provider recovery completes the job and a dead provider
   fails with cause after exactly the 3-attempt budget per chunk.
4. **Strict config (secondary 6):** `strict=True` now refuses only on ABSENT
   names (the `.env.example` documented contract) — present-but-empty dev-stub
   values (STRIPE_*/SAM_API_KEY/SMTP_URL) no longer refuse a strict boot.
5. **DB default (secondary 7):** bare `database_url` fallback is now
   `postgresql://rfp:rfp@localhost:5432/rfp` (DIS-3) — a missing variable
   fails loudly at connect instead of silently booting SQLite.
6. **Dockerfile (secondary 8):** venv at `/app/.venv` per spec §6 verbatim.
7. **Health probe (secondary 9):** `< 400` = ok — a 401/403 bad key now
   honestly reports `down` so FR-043 degradation can trigger. Pinned by
   `test_probe_reports_down_on_4xx` / `test_probe_reports_down_on_network_error`
   / `test_probe_unknown_when_no_key`.
8. **Upload early-413 (secondary 10):** the 1 MB Content-Length buffer is
   now a 64 KiB multipart-framing allowance — a hard `> max_upload_bytes`
   check would falsely reject a boundary-legal file (Content-Length includes
   multipart boundaries/headers); the byte-exact legal limit stays
   authoritative in `intake_service.process_upload`.
9. **Permanent 4xx fail-fast (round-2 pair-coder):** FR-006 retries target
   429/5xx ONLY. A permanent 4xx (401/403/400 — `ProviderClientError`) now
   propagates on the first attempt without retrying or sleeping, instead of
   burning the 3-attempt budget on an error that cannot self-heal. Pinned by
   `test_permanent_4xx_fails_fast_no_retry`.

## Verification performed

- `python3 -m pytest -q` — 133 tests green (auth incl. AC-050 forged-token
  matrix, CSRF AC-021 a–g, intake incl. ZIP bomb/501/encrypted, real-PDF
  end-to-end extraction, preview gate content match, billing incl. webhook
  replay/plan lifecycle, exports content, JSON API, account cascade, sweep
  incl. stub 500/renamed-key/cap/attachment/digest, worker incl. stale
  re-queue + max attempts + provider-retry recovery/exhaustion, model-client
  strict-schema wire pins, provider-health 4xx/network-error probes, mine
  permanent-4xx fail-fast, harness incl. exit codes, ops, health, retention,
  annotation, smoke-fixture self-proof).
- `alembic upgrade head` against SQLite — 20 tables.
- `python3 -m compileall app scripts tests alembic` — clean.

## Open questions for the reviewer

- FR-010 `pages_done` granularity (see gap 3) — acceptable for round 1?
- The four pre-auth CSRF exemptions (signup/login/forgot/reset) — the FR-029
  JSON-bootstrap reading; confirm agreement before round 2 bakes it deeper.

## Round 3 — strict-boot APP_SECRET hardening (reviewer blocker)

`Settings.from_env(strict=True)` previously refused only ABSENT names, so a
present-but-empty `APP_SECRET=""` booted in strict mode and became the
session/reset-token HMAC signing secret — violating the section 5 contract
("signing secrets are at least 32 bytes"). Fixed in `app/core/config.py`:

- New `DEV_STUB_EMPTY_NAMES` (STRIPE_SECRET, STRIPE_WEBHOOK_SECRET,
  SAM_API_KEY, SMTP_URL) — the only names whose empty value is a documented
  dev stub per `.env.example`. Empty values for any other required name now
  refuse a strict boot.
- New `MIN_SECRET_BYTES = 32`; strict boot refuses an APP_SECRET shorter than
  32 UTF-8 bytes (byte length, not char count — multibyte-safe).
- Strict `ConfigError` now names all three defect classes (missing / empty /
  short secret). Non-strict dev boot unchanged: never refuses.

Regression coverage: `tests/test_config.py` (10 tests — empty/whitespace/
short/31-vs-32-byte/multibyte APP_SECRET, empty non-stub refusal, stub
allowlist pinned, non-strict never refuses). Full suite: 143 passed.

## Round 3 — pair-coder: strict boot WIRED into production via APP_ENV

The round-3 implementer fix hardened the strict-boot path correctly, but all
three production service entrypoints (`app/web/app.py`, `app/worker/main.py`,
`app/sweep/scheduler.py`) called `Settings.from_env(strict=False)` — so the
APP_SECRET validation, the empty-non-stub refusal, and the missing-name
refusal NEVER fired in production. The spec §8 line 558 says "app/core/config.py
refuses boot on missing names" — this is the production deployment contract,
and it was not wired in. Fixed:

- `Settings.from_env(strict: bool | None = None)` — when `strict` is `None`
  (the new default), strictness is auto-detected from `APP_ENV`: `prod` →
  strict (refuses on missing/empty/short secrets); `dev`/`test`/absent →
  non-strict (never refuses, for dev/test with placeholder values). An
  explicit `strict=True` or `strict=False` always wins.
- All three production services now call `Settings.from_env()` (no explicit
  `strict=False`), so production boots with strict validation when
  `APP_ENV=prod`.
- `.env.example` documents `APP_ENV=dev` (operator flips to `prod` in
  production `.env`).
- `deploy/compose.prod.yml` sets `APP_ENV: prod` on api/worker/sweep, so the
  production overlay enforces strict boot by default.
- Scripts (`create_staff`, `trigger_sweep`, etc.) remain `strict=False` —
  operational tools, not production services.

Verified: `.env.example` values pass strict boot (all names present, dev
stubs allowed empty, APP_SECRET is 41 bytes). The security fix (empty/short
APP_SECRET refuses) now actually fires in production via the auto-detect
path. Runtime proof: `APP_ENV=prod` + `.env.example` values → boots;
`APP_ENV=prod` + `APP_SECRET=""` → refuses with ConfigError.

Regression coverage: 6 new tests in `tests/test_config.py` (auto-detect
strict when prod, prod refuses empty APP_SECRET via auto-detect, non-strict
when dev/absent/test, explicit strict overrides auto-detect). Full suite:
149 passed (143 + 6).
