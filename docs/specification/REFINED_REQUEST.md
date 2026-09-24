Read first: B6 Concept Lock (reference file). Refused: failure_analyst (concept-viability objections to a B6-locked wedge — the BUILD contract sharpens the locked concept, it does not re-litigate it).

**Goal:** Build a web tool that turns any posted federal RFP into a source-cited draft compliance matrix within minutes. Target user: the owner or capture lead at a 5–50 person set-aside government contractor (SDVOSB, 8(a), HUBZone) bidding IT and professional-services NAICS codes — locatable because SAM.gov entity registrations are public, filterable, and include contact data. They gain a draft matrix replacing 6–12 hours of manual RFP shredding per bid, with every row anchored to source page and section. The first ten requirements are visible free as a sample; the full matrix requires a $149 per-RFP unlock or $249/mo subscription. Distribution: auto-email a free sample matrix to SAM.gov-registered vendors when a new RFP matches their NAICS and set-aside, contingent on legal clearance.

**Deliverables:**
1. Upload-to-matrix flow: user uploads a federal RFP (text-native PDF, scanned/image-only PDF via OCR, or DOCX), receives a draft compliance matrix within minutes — every requirement as a row with requirement ID, section label, source page, and response field. First ten rows visible free; full matrix gated behind payment.
2. Human confirmation checklist marking each requirement verified before export; unconfirmed matrices cannot be exported.
3. Export of confirmed matrices to Excel and Word with columns for requirement ID, section, source page, response field, and verification status.
4. Nightly SAM.gov scan matching new RFPs to registered vendors by NAICS and set-aside, auto-generating a free sample matrix (first ten requirements, due date highlighted). Automated email sending is contingent on legal clearance by week 4; if not cleared, the same generated samples are sent via direct outreach — the scan, match, and extraction pipeline runs regardless.
5. In-product payment enforcement: $149 per-RFP unlock or $249/mo unlimited during beta, full matrix gated behind payment.

**Constraints:**
- 12-week build, $5,000 budget inclusive of infrastructure, inference, and acquisition; solo-buildable, no contracted development or design.
- Upload accepts text-native PDF, scanned/image-only PDF via OCR, and DOCX; matrix generation for a typical federal RFP (~200 pages) completes within minutes.
- Draft matrix must contain only extractable requirements — not boilerplate, table-of-contents entries, or narrative prose.
- Uploaded RFPs are not retained after processing and export unless the user explicitly opts into persistent storage.
- SAM.gov scanner must respect posted API rate limits and fail gracefully on individual call failures without aborting the scan.

**Out of scope:**
- Per B6 Concept Lock: no proposal drafting, opportunity search/CRM, teaming discovery, multi-user workspaces, state/local portal ingest, third-party integrations, customer-facing API, or amendment tracking.

**Acceptance:**
- A user can upload a real federal RFP, see a free ten-row sample, unlock the full source-cited matrix, confirm requirements via checklist, and export to Excel/Word with columns for requirement ID, section, source page, response field, and verification status.
- If legal review clears auto-email by week 4: a matched vendor receives a free sample matrix email the day a matching RFP drops. If not: the founder sends generated sample matrices to matched vendors via direct outreach using the same pipeline output.
- At least one paying beta user has unlocked a full matrix for a real RFP they are actively bidding by end of week 12, regardless of channel.

**Falsification:**
- The founder manually annotates a test corpus of minimum 10 real federal RFPs (including at least 3 scanned/image-only PDFs) with their binding requirements as ground truth. If LLM extraction shows >10% omission of known binding requirements, >20% false positives (non-requirements as rows), or systematic misattribution of source pages, the core value proposition fails.
- If no paying beta user has unlocked a full matrix for a real bid by end of week 12, the revenue hypothesis is disproven.