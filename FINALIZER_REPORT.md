# RFP-Shred-to-Inbox finalizer report

**Final product path:** `/Users/orchistrator/Projects/RFP-Shred-to-Inbox`  
**Finalizer date:** 2026-08-13  
**Disposition:** showcase-ready; public-production claims remain bounded by the evidence gap below.

## What was recovered

The node contains one BUSINESS lineage for this idea:
`2026-07-25T01-59-16Z-pipeline-a5eb676f`. Its three-round cooperative code
implementation converged, but the run never produced a UI child, docs child,
runtime proof, demo artifact, or finalizer output. UI assembly stopped when the
artifact scanner encountered a pytest-created temporary symlink. A historical
delivery under a generic project name was incomplete and was not used.

This final directory was isolated from the strongest accepted source, the
parent run's `cooperative_code/wt`. Historical run artifacts were not mutated.
`LINEAGE.json` binds the source pipeline, idea, concept lock, approved spec,
and selection rationale.

## What the cleanup changed

### Extraction and evidence integrity

- Preserved page identity inside model chunks and stopped numbered requirement
  sentences from masquerading as section headings.
- Verified the full exported requirement body as well as its excerpt against
  source text; fabricated body text can no longer pass on a valid excerpt.
- Deduplicated overlap output and made reprocessing a transactional replacement
  that preserves matching human work instead of appending duplicate sequences.
- Refused a ready state when any model chunk fails, rather than silently
  publishing an incomplete matrix.
- Corrected the showcase oracle: the page-5 proposal-validity instruction is a
  sixth binding requirement, not a false positive. The deterministic fixture
  now measures 0% omissions, 0% false positives, and 0% page-attribution error.

### Security, privacy, and data safety

- Added same-origin and anonymous-CSRF protection to browser authentication.
- Added a pure-ASGI pre-parser upload byte/concurrency gate with typed 413/503
  responses, covering declared-length and streamed/chunked bodies.
- Deferred source-file deletion until database commit; rollback cannot destroy
  bytes. Cleanup failure is logged and may leave an orphan for reconciliation.
- Neutralized spreadsheet-formula injection across exported source/model/user
  text.
- Fixed retained-source cleanup so an explicit retention choice is honored.
- Disabled undeclared framework documentation surfaces and added baseline
  browser/cache/security headers.

### Billing and lifecycle correctness

- Replaced the double-extension Stripe path with canonical subscription-period
  state and a terminal cancellation tombstone; late paid/updated events cannot
  reactivate a deleted subscription.
- Made failed-run retention respect `retain_source`.

### Deployment and packaging

- Corrected the Hatch wheel target and excluded secrets, local databases,
  demo state, coverage output, and caches from build artifacts.
- Added an allowlist `.dockerignore`; the rebuilt source distribution contains
  no `.env`.
- Production now requires HTTPS/public host, a non-development signing secret,
  a real model/provider key, real Stripe price/webhook values, SAM key, and
  SMTP instead of quietly accepting showcase stubs.
- Production Compose publishes only Caddy 80/443, trusts forwarded headers only
  behind that private-network ingress, and gates API/worker/sweep startup on a
  one-shot Alembic migration.
- Repaired the deploy smoke verifier and added one-command local setup/demo,
  deterministic lineage, and source-manifest tooling.

### Showcase experience and exports

- Reworked the server-rendered UI into a coherent responsive product surface
  while preserving the accountless evaluation path and core workflow.
- The browser journey now uses an actual PDF file chooser and completes:
  signup -> upload -> background extraction -> six cited rows -> human check ->
  unlocked Excel and Word downloads.
- Excel now has a frozen, filtered, wrapped, banded matrix with readable column
  widths and no formula errors.
- Word now renders a one-page landscape compliance matrix with a clear title,
  repeated styled header, deliberate column widths, and no clipping/overlap.

## Independent evidence on the final bytes

- Full suite: **191 tests passed**.
- Coverage: **84.95%** (required floor 80%).
- Release-selected Ruff checks (`E9,F63,F7,F82`): passed.
- Live HTTP smoke: health green; actual committed PDF upload reached ready;
  **6 rows** verified by clause/content.
- Browser journey: actual file chooser; **6/6 checked**; Excel and Word exports
  unlocked and downloaded.
- Extraction falsification harness on the committed fixture: omission **0.0**,
  false-positive **0.0**, page-attribution error **0.0**.
- Excel QA: one sheet/table, six data rows, no formula-error tokens, full
  rendered range readable.
- Word QA: one landscape page, six data rows, no clipping, overlap, or missing
  glyphs in the rendered page.
- `uv build`: source distribution and wheel built successfully; archive
  inspection found no `.env` or local demo state.
- Production Compose: configuration passed with an explicit public host; API
  has no published port; Caddy alone exposes 80/443; migration gates all three
  application processes. Missing `PUBLIC_HOST` fails configuration.

## Evidence boundary

This is a strong controlled showcase, not yet evidence for arbitrary customer
solicitations. The committed quality corpus contains one synthetic digital PDF.
Before public production, independently curate and annotate at least ten real,
deidentified solicitations across PDF, scanned PDF, DOCX, ZIP/amendment, agency,
and layout families, with a held-out set and second expert completeness pass.

The Docker daemon was unavailable on this node, so image build/start and a live
Caddy/PostgreSQL production journey were not executed. Real provider, Stripe,
SAM.gov, SMTP, and webhook integrations also require operator credentials and
staging proof. Deferred file cleanup is transaction-safe but not yet a durable
outbox: a process death after commit can leave an orphan file for reconciliation.

## Demo

```bash
cd /Users/orchistrator/Projects/RFP-Shred-to-Inbox
make setup
make demo
```

Open `http://127.0.0.1:8180`, create an account with any 12+ character
password, and upload `tests/fixtures/corpus/smoke_fixture.pdf`.

## 30-second product blurb

RFP Shred turns a federal solicitation PDF, DOCX, or ZIP into a source-cited
compliance matrix for small government contractors. It extracts binding
requirements, preserves clause and page evidence, makes a human verify every
line before export, and produces clean Excel and Word working matrices. The
product is a FastAPI/PostgreSQL application with a database-backed worker,
provider-neutral structured extraction, deterministic citation auditing,
Stripe entitlement paths, and a SAM.gov opportunity-watch loop. For the
showcase, one command runs the real application locally with deterministic
extraction and no external credentials.
