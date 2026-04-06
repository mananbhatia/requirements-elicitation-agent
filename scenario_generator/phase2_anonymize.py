"""
Phase 2 — Anonymization (Optional).

Purpose:
    Replace identifying information (company name, real person names, vendor names,
    specific locations) with fictional equivalents while preserving the relational
    structure and domain realism.

Design rationale:
    Anonymization is not find-and-replace. It must preserve:
    - Team dynamics and reporting structures
    - Industry domain (essential for scenario realism)
    - Technical platform details (Databricks, Azure, AWS — these are the consulting domain)
    - Business context and organizational pressures

    This phase is optional. The human may choose to skip it entirely (e.g., when
    team members already know the source engagement, or when the scenario is
    fully fictional from Phase 0).

    The human MUST review the output — automated anonymization cannot guarantee
    consistency or catch all identifying details.

Input:  Phase 1 extraction output (scenario_parameters + extracted_facts).
Output: Same structure with anonymized content + a mapping table for reference.
"""

from .config import llm_call, save_phase_output, load_phase_output
import json

ANONYMIZATION_SYSTEM_PROMPT = """\
You are an anonymization system for consulting engagement data. Your task is to \
replace identifying information with fictional equivalents while preserving the \
relational structure, domain context, and technical details.

You are consistent — the same real entity always maps to the same fictional name. \
You preserve the industry, team structure, and technical platform details."""

ANONYMIZATION_PROMPT = """\
Below is a structured extraction from consultant engagement notes. Anonymize it \
by replacing identifying information with fictional equivalents.

WHAT TO REPLACE:
- Company name → fictional company name (keep the same industry)
- External vendor/partner/contractor company names → fictional equivalents
- Specific office locations or addresses
- Any identifiers that could trace back to the real engagement (subscription IDs, \
tenant IDs, workspace IDs, email addresses, URLs)

WHAT TO PRESERVE (do NOT change):
- Internal person names (team members, client staff) — keep these as-is
- Industry/sector (a waste management company stays a waste management company)
- Technical platform names (Databricks, Azure, AWS, Unity Catalog, etc.)
- Technical tool names (Terraform, PowerBI, DBT, ADF, MuleSoft, etc.)
- Team size and structure
- Technical findings and their specifics
- Business context, pressures, and dynamics
- Role titles and job functions

CONSISTENCY RULES:
- Each real entity maps to exactly one fictional entity throughout
- Maintain plausible names for the industry and geography

Respond with valid JSON containing:
1. "mapping": a dict mapping each real name/entity to its fictional replacement
2. "scenario_parameters": the anonymized scenario parameters
3. "extracted_facts": the anonymized extracted facts (same structure as input)

INPUT:
{extraction_json}
"""


def run_phase2(scenario_name: str) -> dict:
    """
    Anonymize the Phase 1 extraction output.

    Loads Phase 1 output, applies anonymization, saves the result.
    The human should review the mapping and anonymized output for consistency.

    Args:
        scenario_name: Identifier for this scenario run.

    Returns:
        Anonymized extraction dict with mapping table.
    """
    extraction = load_phase_output(scenario_name, "phase1_extraction")
    extraction_json = json.dumps(extraction, indent=2, ensure_ascii=False)

    prompt = ANONYMIZATION_PROMPT.format(extraction_json=extraction_json)
    raw_response = llm_call(
        prompt,
        system=ANONYMIZATION_SYSTEM_PROMPT,
        max_tokens=16384,
        temperature=0.2,
    )

    # Parse response
    cleaned = raw_response.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1]
    if cleaned.endswith("```"):
        cleaned = cleaned.rsplit("```", 1)[0]
    cleaned = cleaned.strip()

    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}") + 1
        if start >= 0 and end > start:
            result = json.loads(cleaned[start:end])
        else:
            raise ValueError("Could not parse anonymization response as JSON.")

    output_path = save_phase_output(scenario_name, "phase2_anonymized", result)
    print(f"Phase 2 complete. Output saved to: {output_path}")
    print(f"  Name mapping: {json.dumps(result.get('mapping', {}), indent=2)}")
    print(f"  Review the mapping and anonymized facts, then proceed to Phase 3.")

    return result
