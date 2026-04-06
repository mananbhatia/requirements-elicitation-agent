"""
Phase 0 — From-Scratch Scenario Generation.

Purpose:
    Generate a structured scenario extraction from parameters alone, without
    any source engagement notes. The output matches Phase 1's format so that
    Phases 3-6 work identically regardless of whether the scenario came from
    real notes or was generated.

Design rationale:
    Real engagement notes produce the most realistic scenarios because they
    contain the specific, textured details that make a persona feel authentic
    (e.g., "€10k damage in a single week from nitrous oxide bottles on the
    sorting line"). Pure LLM generation will produce technically plausible but
    potentially generic scenarios.

    The mitigation is twofold:
    1. The generation prompt encodes patterns from real engagements — what
       typical problems look like at each maturity level, what team dynamics
       emerge, what attempted solutions commonly fail.
    2. The human reviews and enriches the output. A 30-minute conversation
       with a consultant who has done similar engagements provides texture
       that no prompt can fully replicate.

    The scenario parameters (dimensions) that drive generation are informed by
    analysis of real Revodata engagements across different industries, maturity
    levels, and engagement types.

Input:  Scenario parameters dict.
Output: Structured extraction (same format as Phase 1) for downstream phases.
"""

from .config import llm_call, save_phase_output
import json

_OPUS_MODEL = "claude-opus-4-6"

GENERATION_SYSTEM_PROMPT = """\
You are generating a realistic consulting engagement scenario for a client \
training simulation system. The scenario must feel like a real client engagement \
— with specific people, real frustrations, organizational politics, attempted \
solutions that didn't work, and technical details grounded in actual platform \
configurations.

You draw on knowledge of typical data platform engagements: what problems \
companies face at different maturity levels, how teams grow organically, what \
misconfigurations are common, and what organizational dynamics create blockers.

Generate specific, concrete details — not generic descriptions. Name team members, \
give them distinct concerns, create interpersonal dynamics. Include technical \
specifics that a consultant would actually encounter."""

GENERATION_PROMPT = """\
Generate a realistic consulting engagement scenario based on these parameters:

SCENARIO PARAMETERS:
- Company name: {company_name}
- Industry: {industry}
- Platform maturity: {platform_maturity}
  (LOW = manual processes, ad-hoc infrastructure, organic growth, no platform strategy
   MEDIUM = some structure and tooling, partial automation, emerging governance
   HIGH = well-defined processes, automation, clear strategy, mature platform engineering)
- Engagement type: {engagement_type}
  (platform_review = broad review of existing platform
   architecture_design = designing new platform or major component
   migration = migrating from legacy system or between platforms
   implementation = implementing specific feature or capability
   bi_analytics = analytics, dashboards, reporting setup
   managed_onboarding = onboarding to a managed platform service)
- Cloud platform: {cloud_platform}
- Primary problem clusters: {problem_clusters}
- Team size: {team_size}
- Interview persona role: {persona_role}

Generate a complete set of extracted facts as if they came from real engagement \
notes. The facts should include:

1. ORGANIZATIONAL CONTEXT (5-8 facts):
   Company background, business units, digital transformation initiatives, \
   company size, industry-specific context.

2. STRATEGIC CONTEXT (4-6 facts):
   Business priorities, budget constraints, strategic decisions pending, \
   leadership expectations, pressure points between business and IT.

3. TEAM DYNAMICS (5-8 facts):
   Named team members (4-7 people) with specific roles, who raised which \
   concerns, interpersonal dynamics, skill gaps, who is overwhelmed.

4. TECHNICAL FINDINGS (8-12 facts):
   Specific platform issues appropriate to the platform maturity and problem \
   clusters. For a {cloud_platform} + Databricks platform at {platform_maturity} \
   platform maturity, include realistic misconfigurations and gaps. Be specific about \
   configurations, not generic about problems.

5. ATTEMPTED SOLUTIONS (3-5 facts):
   What the team has tried to fix problems, why it didn't stick, what was \
   abandoned. These should feel like real organizational inertia — not \
   contrived failures.

6. EXTERNAL RELATIONSHIPS (2-4 facts):
   Vendors, previous consultancies, contractors, technology partners. Include \
   at least one relationship that ended or didn't deliver.

7. BUSINESS USE CASES (2-4 facts):
   What the business wants from the platform — specific use cases relevant \
   to the industry. Include at least one that's aspirational (e.g., AI/ML) \
   and one that's operational.

CONSISTENCY REQUIREMENTS:
- All facts must be internally consistent
- Technical details must be realistic for {cloud_platform} + Databricks
- Team dynamics must be plausible for a team of ~{team_size}
- The platform maturity level must be reflected consistently across all facts
- The engagement type shapes what the consultant would encounter

REALISM REQUIREMENTS:
- Include industry-specific details (a {industry} company would have specific \
  data types, regulatory concerns, operational patterns)
- Team members should have names appropriate to the company's geography
- Budget and resource constraints should be realistic
- Include at least one "surprise" finding that wouldn't be obvious from the \
  engagement description

Respond ONLY with valid JSON in this structure:
{{
  "scenario_parameters": {{
    "company_name": "{company_name}",
    "industry": "{industry}",
    "platform_maturity": "{platform_maturity}",
    "engagement_type": "{engagement_type}",
    "cloud_platform": "{cloud_platform}",
    "primary_problem_clusters": {problem_clusters_json},
    "team_size": "{team_size}"
  }},
  "extracted_facts": [
    {{
      "id": "F001",
      "content": "...",
      "category": "...",
      "source_passage": "generated"
    }}
  ]
}}
"""


def run_phase0(
    company_name: str,
    industry: str,
    platform_maturity: str,
    engagement_type: str,
    cloud_platform: str,
    problem_clusters: list[str],
    team_size: str,
    persona_role: str,
    scenario_name: str,
    persona_maturity: str = "LOW",
    interview_stage: str = "initial_discovery",
    model: str = _OPUS_MODEL,
) -> dict:
    """
    Generate a scenario from parameters without source notes.

    Args:
        company_name: Fictional company name.
        industry: Industry/sector (e.g., "waste management", "fintech").
        platform_maturity: LOW, MEDIUM, MEDIUM_HIGH, or HIGH — the org's data platform state.
        engagement_type: Type of engagement.
        cloud_platform: azure, aws, or gcp.
        problem_clusters: List of problem areas (e.g., ["iam", "governance"]).
        team_size: Approximate team size (e.g., "5-6 FTE").
        persona_role: Role of the interview persona.
        scenario_name: Identifier for this scenario run.

    Returns:
        Generated extraction dict (same format as Phase 1).
    """
    prompt = GENERATION_PROMPT.format(
        company_name=company_name,
        industry=industry,
        platform_maturity=platform_maturity,
        engagement_type=engagement_type,
        cloud_platform=cloud_platform,
        problem_clusters=", ".join(problem_clusters),
        problem_clusters_json=json.dumps(problem_clusters),
        team_size=team_size,
        persona_role=persona_role,
    )

    raw_response = llm_call(
        prompt,
        system=GENERATION_SYSTEM_PROMPT,
        model=model,
        max_tokens=16384,
        temperature=0.5,  # higher for creative generation
    )

    cleaned = raw_response.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1]
    if cleaned.endswith("```"):
        cleaned = cleaned.rsplit("```", 1)[0]
    cleaned = cleaned.strip()

    try:
        extraction = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}") + 1
        if start >= 0 and end > start:
            extraction = json.loads(cleaned[start:end])
        else:
            raise ValueError("Could not parse generation response as JSON.")

    # Inject pipeline parameters — not derivable from generated content.
    extraction.setdefault("scenario_parameters", {})["persona_maturity"] = persona_maturity
    extraction["scenario_parameters"]["interview_stage"] = interview_stage

    n_facts = len(extraction.get("extracted_facts", []))
    output_path = save_phase_output(scenario_name, "phase1_extraction", extraction)

    print(f"Phase 0 complete. Generated scenario saved to: {output_path}")
    print(f"  Generated {n_facts} facts for '{company_name}' ({industry}).")
    print(f"  All facts marked as source_passage='generated'.")
    print(f"\n  Review carefully — LLM-generated scenarios need enrichment:")
    print(f"    - Are technical details realistic for {cloud_platform}?")
    print(f"    - Do team dynamics feel plausible, not contrived?")
    print(f"    - Are there industry-specific details that feel authentic?")
    print(f"    - Consider adding texture from a consultant conversation.")
    print(f"\n  Edit the output, then proceed to Phase 3 (skip Phase 2).")

    return extraction
