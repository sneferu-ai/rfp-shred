"""FR-007 prompt text and the strict extraction JSON schema.

PROMPT_VERSION is stamped onto every ``raw_model_outputs`` row so prompts
can iterate without schema churn and any run can be replayed.
"""

from __future__ import annotations

PROMPT_VERSION = "extract-v2-page-markers"

SYSTEM_PROMPT = (
    "You are a compliance matrix extraction system. Extract all "
    "requirement-like items from federal solicitations — both binding "
    "requirements and non-binding text (boilerplate, table of contents "
    "entries, narrative prose). For each item, set `is_requirement=true` if "
    "it imposes an obligation on the offeror (identified by modal verbs: "
    "shall, must, will, is required to, is responsible for, or imperative "
    "mood in instruction sections L, M, and evaluation criteria), and "
    "`is_requirement=false` otherwise. Return ALL items you identify — do "
    "not omit non-requirements; the downstream system will route non-binding "
    "items to a review panel. For each item, provide the clause identifier, "
    "section label, page number, the full text, and a short excerpt (<=200 "
    "characters) from the source page. Content uses explicit [[PAGE N]] "
    "markers; assign each item to the marker governing its source text and "
    "never return a marker as document content. Return only JSON matching the "
    "provided schema."
)

USER_PROMPT_TEMPLATE = (
    "Section: {section_heading}\n"
    "Parent section: {parent_section}\n"
    "Pages: {page_start}-{page_end}\n"
    "Section context: {section_context_summary}\n"
    "Content:\n"
    "{chunk_text}\n\n"
    "Extract all candidate requirements (both binding and non-binding) from "
    "this section. Return JSON per the schema."
)

EXTRACTION_SCHEMA: dict = {
    "type": "object",
    "required": ["requirements"],
    "properties": {
        "requirements": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["clause_id", "section", "page", "text", "excerpt", "is_requirement"],
                "properties": {
                    "clause_id": {"type": "string", "description": "Requirement identifier, e.g., L.3.2.1 or M.1.a"},
                    "section": {"type": "string", "description": "Section heading, e.g., Section L"},
                    "page": {"type": "integer", "minimum": 1},
                    "text": {"type": "string", "maxLength": 2000, "description": "Full requirement text"},
                    "excerpt": {"type": "string", "maxLength": 200, "description": "Verbatim quote from the source page containing the requirement"},
                    "is_requirement": {"type": "boolean", "description": "true if binding requirement; false if boilerplate/TOC/narrative"},
                },
                "additionalProperties": False,
            },
        }
    },
    "additionalProperties": False,
}
