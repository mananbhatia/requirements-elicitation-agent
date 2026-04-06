"""
Phase 1 — Structured Extraction from Raw Engagement Notes.

Purpose:
    Transform unstructured consultant engagement notes into a structured set of
    categorized facts. This is the grounding step — every fact in the final
    scenario traces back to an extraction from source material.

Design rationale:
    Real engagement notes are a mix of technical findings, organizational context,
    meeting logistics, code snippets, and vendor details. The extraction must
    separate signal from noise and categorize each fact for downstream processing.

    The extraction also captures scenario parameters (company profile, maturity,
    engagement type) which inform all subsequent phases.

    Per "Using AI for User Representation" (grounding principle): personas should
    be grounded in primary data. This extraction creates the audit trail from
    source material to scenario content.

Input:  Raw text of engagement notes (one or more documents concatenated).
Output: JSON with scenario_parameters and extracted_facts array.
"""

from .config import llm_call, save_phase_output, load_phase_output, FACT_CATEGORIES

EXTRACTION_SYSTEM_PROMPT = """\
You are a structured information extraction system. Your task is to extract \
every distinct factual claim from consultant engagement notes and categorize \
each one. You also infer high-level scenario parameters from the notes.

You are precise, exhaustive, and conservative — you extract what is stated or \
clearly implied, never what is speculated. Each extracted fact should be a \
single, atomic claim that could stand alone."""

EXTRACTION_PROMPT = """\
Below are raw consultant engagement notes from a client engagement. These may \
include meeting notes, technical findings, email threads, architecture reviews, \
and action items — possibly from multiple sessions.

Your task has two parts:

PART 1 — SCENARIO PARAMETERS
Infer the following from the notes. If a parameter cannot be determined, use "unknown".

- company_name: The client company name
- industry: The client's industry/sector
- platform_maturity: LOW (manual processes, ad-hoc infrastructure, organic growth, \
no clear platform strategy) | MEDIUM (some structure and tooling, partial automation, \
emerging governance) | HIGH (well-defined processes, automation, clear strategy, \
mature platform engineering)
- engagement_type: platform_review | architecture_design | migration | \
implementation | bi_analytics | managed_onboarding
- cloud_platform: azure | aws | gcp | multi_cloud
- primary_problem_clusters: list of main problem areas (e.g., ["iam", "governance", \
"security", "workspace_architecture"])
- team_size: approximate FTE count for the data/platform team

PART 2 — FACT EXTRACTION
Extract every distinct factual claim and categorize it. Categories:

- technical_finding: Specific platform issues, misconfigurations, gaps, current \
technical setup details (e.g., "Key Vaults have public access enabled", \
"Production workloads running on a non-production workspace")

- organizational_context: Company structure, business units, legal entities, \
company size, industry position, digital transformation initiatives \
(e.g., "Three legal entities under a single holding company, each with separate reporting requirements")

- strategic_context: Business priorities, budget constraints, strategic decisions \
pending, vision/roadmap items, pressure points \
(e.g., "Platform manager needs a clear roadmap to justify headcount to the CFO")

- team_dynamics: Who does what, team roles, interpersonal signals, who raised \
which concerns, team frustrations, skill gaps \
(e.g., "Data engineer wants a self-service access model so they stop being a ticket-handling bottleneck")

- attempted_solutions: What they have tried, what failed, what was abandoned, \
what is in progress but not complete \
(e.g., "External partner set up the initial infrastructure; no handover documentation was provided")

- external_relationships: Vendors, partners, contractors, external dependencies \
(e.g., "Cloud networking managed by a separate IT team with a long change-request lead time")

- business_use_cases: What the business wants from the platform — specific use \
cases, reporting needs, AI ambitions \
(e.g., "Operations team wants automated anomaly detection on production line sensor data")

SKIP entirely: meeting logistics, calendar invites, email signatures, portal \
URLs, code snippets, Outlook links, action items that are purely administrative.

For each fact, provide:
- id: Sequential identifier (F001, F002, ...)
- content: The fact as a clear, self-contained statement
- category: One of the seven categories above
- source_passage: The original text passage this was extracted from (brief quote, \
max 30 words)

Respond ONLY with valid JSON in this exact structure:
{{
  "scenario_parameters": {{
    "company_name": "...",
    "industry": "...",
    "platform_maturity": "...",
    "engagement_type": "...",
    "cloud_platform": "...",
    "primary_problem_clusters": [...],
    "team_size": "..."
  }},
  "extracted_facts": [
    {{
      "id": "F001",
      "content": "...",
      "category": "...",
      "source_passage": "..."
    }}
  ]
}}

---
RAW ENGAGEMENT NOTES:

{notes}
"""


def run_phase1(
    notes_text: str,
    scenario_name: str,
    interview_stage: str = "initial_discovery",
) -> dict:
    """
    Extract structured facts from raw engagement notes.

    Args:
        notes_text: Raw text of engagement notes (concatenated if multiple docs).
        scenario_name: Identifier for this scenario run (used for file paths).
        interview_stage: The consulting session type — initial_discovery, follow_up,
                         or ongoing. Pipeline-provided, not extracted from notes.

    Returns:
        Structured extraction dict with scenario_parameters and extracted_facts.
    """
    prompt = EXTRACTION_PROMPT.format(notes=notes_text)
    raw_response = llm_call(
        prompt,
        system=EXTRACTION_SYSTEM_PROMPT,
        max_tokens=16384,
        temperature=0.2,
    )

    # Parse JSON from response — strip markdown fences if present
    cleaned = raw_response.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1]
    if cleaned.endswith("```"):
        cleaned = cleaned.rsplit("```", 1)[0]
    cleaned = cleaned.strip()

    extraction = _parse_json_safe(cleaned)

    # Inject pipeline-provided parameters — not derivable from notes
    extraction["scenario_parameters"]["interview_stage"] = interview_stage

    # Validate structure
    _validate_extraction(extraction)

    # Save for human review
    output_path = save_phase_output(scenario_name, "phase1_extraction", extraction)
    print(f"Phase 1 complete. Output saved to: {output_path}")
    print(f"  Extracted {len(extraction['extracted_facts'])} facts.")
    print(f"  Review and edit the output file, then proceed to Phase 2 or 3.")

    return extraction


def _parse_json_safe(text: str) -> dict:
    """Parse JSON with fallback error handling."""
    import json
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        # Try to find JSON block in response
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
        raise ValueError(
            f"Could not parse LLM response as JSON.\n"
            f"Parse error: {e}\n"
            f"Response (first 500 chars): {text[:500]}"
        )


def _validate_extraction(data: dict) -> None:
    """Basic structural validation of extraction output."""
    if "scenario_parameters" not in data:
        raise ValueError("Missing 'scenario_parameters' in extraction output.")
    if "extracted_facts" not in data:
        raise ValueError("Missing 'extracted_facts' in extraction output.")

    for fact in data["extracted_facts"]:
        if "id" not in fact or "content" not in fact or "category" not in fact:
            raise ValueError(
                f"Fact missing required fields (id, content, category): {fact}"
            )
        if fact["category"] not in FACT_CATEGORIES:
            print(
                f"  WARNING: Fact {fact['id']} has unexpected category "
                f"'{fact['category']}'. Expected one of: {FACT_CATEGORIES}"
            )
