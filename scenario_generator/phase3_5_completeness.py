"""
Phase 3.5 — Completeness Check and Gap Fill.

Purpose:
    Assess whether classified character knowledge provides enough material for
    each narrative section, and generate plausible facts to fill gaps.

Design rationale:
    Phase 4 narrates whatever character knowledge it receives. If the source
    notes were thin, Phase 4 produces a thin narrative — and the persona feels
    hollow. This phase exists to decouple input quality from output quality.

    Two-paragraph source notes should still produce a usable scenario draft.
    This phase is what makes that possible: it identifies which narrative
    dimensions are underpopulated and generates grounded, consistent content
    to fill them before Phase 4 runs.

    The generation uses Opus with all existing facts as context — including
    discovery items as negative constraints — so generated content is
    consistent, non-contradictory, and cannot be used to reason to gated items.

    All generated facts are marked source_passage: "[generated]" so the human
    reviewer can identify and validate them before Phase 4 runs.

Step 1 — Assessment (Sonnet): evaluate per-section sufficiency against concrete
    criteria. Each section needs enough material for a 3-4 turn natural
    conversation. Output: per-section gap descriptions.

Step 2 — Generation (Opus): for each insufficient section, generate plausible
    facts grounded in established context. Output: new character_knowledge items
    with source_passage: "[generated]".

Input:  Phase 3 classification (character_knowledge, discovery_items,
        scenario_parameters, topic_taxonomy).
Output: Enriched classification + completeness report.
        Saved as phase3_5_completeness_output.json.
"""

from .config import llm_call, save_phase_output, load_phase_output
import json

_SONNET_MODEL = "claude-sonnet-4-6"
_OPUS_MODEL = "claude-opus-4-6"


ASSESSMENT_SYSTEM_PROMPT = """\
You are assessing whether classified character knowledge provides sufficient \
material for a synthetic client persona to hold a natural conversation on each \
narrative dimension. You are checking for gaps, not quality — the content will \
be generated separately."""

ASSESSMENT_PROMPT = """\
Below are classified character knowledge facts, grouped by narrative section. \
Assess whether each section has enough material for the persona to hold a \
natural 3-4 turn conversation on the topic without repeating themselves.

SCENARIO PARAMETERS:
{scenario_params}

CHARACTER KNOWLEDGE FACTS (grouped by section):
{facts_by_section}

SUFFICIENCY CRITERIA PER SECTION:

organizational_history:
  Sufficient if: the persona can tell a coherent story of how things evolved —
  when the platform was adopted, how the team grew, what the key turning points
  were. Not just "we use Databricks" but a narrative arc.
  Insufficient if: only current state is described, with no sense of how the
  company got here or what decisions shaped the current situation.

strategic_context:
  Sufficient if: there is tension — competing priorities, business expectations
  vs technical reality, resource constraints vs ambitions. The persona should
  have something to say about what the organisation is trying to do and why it's
  hard, not just list business goals.
  Insufficient if: only generic business goals or vague strategy mentions exist,
  with no concrete trade-offs or pressures the persona has to navigate.

current_platform_state:
  Sufficient if: the persona can give a conversational-level account of their
  setup appropriate to their maturity level — what they understand, what they're
  uncertain about, what they know the team struggles with.
  Insufficient if: fewer than 3 facts about the current platform exist, or the
  facts are too technical for the maturity level to articulate.

team_dynamics:
  Sufficient if: each named team member has at least one distinct concern,
  frustration, or characteristic the persona can relay. There is at least one
  interpersonal dynamic — who disagrees, who is overwhelmed, who the persona
  trusts more.
  Insufficient if: team members are only listed by role with no personality,
  or fewer than 2 team members have individual characteristics.

attempted_solutions:
  Sufficient if: at least 2 distinct attempts exist, each revealing something
  different about the organisation (e.g., one showing resource constraints,
  another showing organisational politics or skill gaps). Attempts should feel
  like real inertia, not contrived failures.
  Insufficient if: only one attempt exists, or all attempts failed for the same
  reason, or there are no attempts at all.

mental_model:
  Sufficient if: the persona has a distinct internal perspective — what
  frustrates them specifically, what they're pragmatic about, what they
  privately worry about. This should feel like internal monologue, not an
  executive summary.
  Insufficient if: only factual descriptions exist with no emotional or
  perspective content, or the content is generic ("things are difficult").

For each section, respond with whether it is sufficient and, if not, a specific
description of what is missing. Be precise — name the gap, not just "more needed."

Respond ONLY with valid JSON:
{{
  "organizational_history": {{
    "sufficient": true,
    "fact_count": 3,
    "gap_description": ""
  }},
  "strategic_context": {{
    "sufficient": false,
    "fact_count": 1,
    "gap_description": "Only one business goal stated (cost reduction). No trade-offs, pressures, or competing priorities. The persona has nothing to say about why strategy is hard to execute."
  }},
  "current_platform_state": {{ ... }},
  "team_dynamics": {{ ... }},
  "attempted_solutions": {{ ... }},
  "mental_model": {{ ... }}
}}
"""


GENERATION_SYSTEM_PROMPT = """\
You are generating plausible character knowledge facts for a synthetic client \
persona in a consultant training simulation. These facts will fill identified \
gaps in the persona's narrative.

Your job is to invent facts that feel real — grounded in the industry, the \
team's situation, and the company's history as established. Every fact you \
generate must be internally consistent with all existing facts."""

GENERATION_PROMPT = """\
The assessment below identified gaps in a synthetic client persona's character \
knowledge. Generate plausible facts to fill each gap.

SCENARIO PARAMETERS:
{scenario_params}

EXISTING CHARACTER KNOWLEDGE (all established facts — do not contradict these):
{existing_char_knowledge}

DISCOVERY ITEMS (gated facts — do NOT generate content that lets the persona \
reason toward these without being asked. Treat these as forbidden inference paths.):
{discovery_items}

GAPS TO FILL:
{gaps}

GENERATION RULES:

1. Generate facts that are consistent with the industry, maturity level, team
   size, and cloud platform from scenario parameters.

2. Do NOT contradict any established fact. If a fact says the team has 5 people,
   don't invent a 20-person team.

3. Do NOT create inference paths to discovery items. If a discovery item is
   "production jobs run on acceptance", do not generate "the team is unclear
   which environment handles live workloads" — that scents the answer. Instead,
   generate facts about operational confusion in general terms, or unrelated
   team dynamics.

4. Generate facts that feel earned — things a real person in this role at this
   type of company would experience. Avoid corporate clichés ("alignment" issues,
   "communication gaps"). Instead: a specific argument in a meeting, a budget
   decision that didn't land, a team member who joined and immediately flagged
   something.

5. For organizational_history: invent plausible turning points (a migration
   decision, a team restructure, a failed initiative that led to the current
   state). Ground in the industry and company size.

6. For strategic_context: invent specific tensions — a business unit that wants
   something the platform can't deliver, a cost constraint that prevented a
   proper solution, a management expectation that the team thinks is unrealistic.

7. For team_dynamics: invent distinct characteristics for named team members —
   one is cautious and documents everything, another moves fast and skips
   process. Give them opinions that occasionally conflict.

8. For attempted_solutions: invent 2-3 distinct attempts that each failed for
   a different reason (budget killed, team capacity ran out, solution worked but
   wasn't adopted, brought in an external party who left before finishing).

9. For mental_model: invent how the persona privately frames the situation —
   what analogy they'd use, what keeps them up at night, what they've stopped
   worrying about because it's too far beyond them.

10. Mark every generated fact with source_passage: "[generated]".

Generate 3-6 facts per gap section. For sections with no gap, generate nothing.
Use IDs in the format "GEN-01", "GEN-02", etc. (continuing from where existing
generated IDs leave off, or starting at GEN-01 if none exist).

Respond ONLY with valid JSON — a list of character knowledge fact objects:
[
  {{
    "id": "GEN-01",
    "content": "...",
    "narrative_section": "organizational_history",
    "topic": "governance",
    "source_passage": "[generated]"
  }},
  ...
]
"""


def run_phase3_5(scenario_name: str, persona_name: str = None) -> dict:
    """
    Assess character knowledge coverage and fill gaps with generated content.

    Loads from phase3_classified_{persona}.json (if persona_name given) or
    phase3_classified_output.json. Saves enriched output with the same persona suffix.

    Args:
        scenario_name: Identifier for this scenario run.
        persona_name: Optional persona name for persona-scoped filenames.

    Returns:
        Enriched classification dict (character_knowledge, discovery_items,
        scenario_parameters, topic_taxonomy, completeness_report).
    """
    classification = load_phase_output(scenario_name, "phase3_classified", persona_name=persona_name)
    params = classification["scenario_parameters"]
    char_knowledge = classification.get("character_knowledge", [])
    discovery_items = classification.get("discovery_items", [])

    print(f"  Loaded {len(char_knowledge)} character knowledge facts, "
          f"{len(discovery_items)} discovery items.")

    # Step 1: Assess coverage per section
    print(f"\n  Step 1: Assessing per-section coverage (Sonnet)...")
    assessment = _assess_coverage(char_knowledge, params)

    insufficient = {
        section: info
        for section, info in assessment.items()
        if not info.get("sufficient", True)
    }

    if not insufficient:
        print(f"  All sections sufficient. No generation needed.")
        enriched = dict(classification)
        enriched["completeness_report"] = {
            "sections_assessed": assessment,
            "generated_count": 0,
            "generated_ids": [],
        }
        output_path = save_phase_output(scenario_name, "phase3_5_completeness", enriched, persona_name=persona_name)
        print(f"\nPhase 3.5 complete. Output saved to: {output_path}")
        return enriched

    insufficient_sections = list(insufficient.keys())
    print(f"  {len(insufficient_sections)} section(s) need filling: "
          f"{', '.join(insufficient_sections)}")
    for section, info in insufficient.items():
        print(f"    [{section}] {info.get('gap_description', '(no detail)')}")

    # Step 2: Generate facts for insufficient sections
    print(f"\n  Step 2: Generating gap-fill content (Opus)...")
    generated_facts = _generate_gap_facts(
        insufficient, char_knowledge, discovery_items, params
    )

    print(f"  Generated {len(generated_facts)} new facts:")
    sections_filled = {}
    for fact in generated_facts:
        s = fact.get("narrative_section", "unknown")
        sections_filled[s] = sections_filled.get(s, 0) + 1
    for section, count in sections_filled.items():
        print(f"    {section}: {count} fact(s)")

    # Step 3: Merge and save
    enriched_char_knowledge = char_knowledge + generated_facts
    generated_ids = [f["id"] for f in generated_facts]

    enriched = dict(classification)
    enriched["character_knowledge"] = enriched_char_knowledge
    enriched["completeness_report"] = {
        "sections_assessed": assessment,
        "generated_count": len(generated_facts),
        "generated_ids": generated_ids,
    }

    output_path = save_phase_output(scenario_name, "phase3_5_completeness", enriched, persona_name=persona_name)
    print(f"\nPhase 3.5 complete. Output saved to: {output_path}")
    print(f"  Total character knowledge facts: {len(enriched_char_knowledge)} "
          f"({len(generated_facts)} generated)")
    print(f"\n  Review generated facts in the completeness output file.")
    print(f"  Look for entries with source_passage: \"[generated]\".")
    print(f"  Edit or remove any that feel implausible before proceeding to Phase 4.")

    return enriched


def _assess_coverage(char_knowledge: list, params: dict) -> dict:
    """
    Sonnet: assess per-section sufficiency of character knowledge.
    Returns a dict mapping section name to assessment result.
    """
    # Group facts by section
    by_section = {}
    for fact in char_knowledge:
        section = fact.get("narrative_section", "uncategorized")
        if section not in by_section:
            by_section[section] = []
        by_section[section].append(fact["content"])

    facts_by_section_str = json.dumps(
        {s: facts for s, facts in by_section.items()},
        indent=2,
        ensure_ascii=False,
    )

    prompt = ASSESSMENT_PROMPT.format(
        scenario_params=json.dumps(params, indent=2),
        facts_by_section=facts_by_section_str,
    )

    raw_response = llm_call(
        prompt,
        system=ASSESSMENT_SYSTEM_PROMPT,
        model=_SONNET_MODEL,
        max_tokens=4096,
        temperature=0.0,
    )

    cleaned = _clean_json(raw_response)
    try:
        assessment = json.loads(cleaned)
    except json.JSONDecodeError:
        print("  WARNING: Could not parse assessment response. Assuming all sections sufficient.")
        return {
            s: {"sufficient": True, "fact_count": len(facts), "gap_description": ""}
            for s, facts in by_section.items()
        }

    # Fill in any sections the LLM omitted
    required_sections = [
        "organizational_history", "strategic_context", "current_platform_state",
        "team_dynamics", "attempted_solutions", "mental_model",
    ]
    for section in required_sections:
        if section not in assessment:
            count = len(by_section.get(section, []))
            assessment[section] = {
                "sufficient": count >= 2,
                "fact_count": count,
                "gap_description": "" if count >= 2 else f"No facts classified under {section}.",
            }

    return assessment


def _generate_gap_facts(
    insufficient: dict,
    char_knowledge: list,
    discovery_items: list,
    params: dict,
) -> list:
    """
    Opus: generate plausible facts to fill identified gaps.
    Returns a list of new character_knowledge fact dicts.
    """
    gaps_str = "\n".join(
        f"- {section}: {info.get('gap_description', 'insufficient content')}"
        for section, info in insufficient.items()
    )

    prompt = GENERATION_PROMPT.format(
        scenario_params=json.dumps(params, indent=2),
        existing_char_knowledge=json.dumps(char_knowledge, indent=2, ensure_ascii=False),
        discovery_items=json.dumps(
            [{"id": d["id"], "content": d["content"]} for d in discovery_items],
            indent=2,
            ensure_ascii=False,
        ),
        gaps=gaps_str,
    )

    raw_response = llm_call(
        prompt,
        system=GENERATION_SYSTEM_PROMPT,
        model=_OPUS_MODEL,
        max_tokens=8192,
        temperature=0.5,
    )

    cleaned = _clean_json(raw_response)
    try:
        generated = json.loads(cleaned)
        if not isinstance(generated, list):
            print("  WARNING: Generation response was not a list. Returning empty.")
            return []
        # Ensure all generated facts have source_passage: "[generated]"
        for fact in generated:
            fact.setdefault("source_passage", "[generated]")
        return generated
    except json.JSONDecodeError:
        print("  WARNING: Could not parse generation response. No facts generated.")
        return []


def _clean_json(raw: str) -> str:
    """Strip markdown code fences from LLM response."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1]
    if cleaned.endswith("```"):
        cleaned = cleaned.rsplit("```", 1)[0]
    return cleaned.strip()
