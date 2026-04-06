"""
Phase 5 — Inference Path Validation.

Purpose:
    Verify that the character knowledge narrative does not contain information
    that would allow the persona LLM to infer discovery items through reasoning.

Design rationale:
    Knowledge gating is the core architectural principle: the persona LLM cannot
    reveal what it cannot see. But if the character knowledge (which the LLM
    always sees) contains enough context to infer a discovery item, the gating
    is architecturally compromised — the LLM can reason its way to the answer.

    Example of an inference path:
    - Character knowledge: "The acceptance workspace handles testing, dashboards,
      and some production work"
    - Discovery item: "Production jobs run on acceptance"
    → The LLM can directly infer the discovery item from character knowledge.

    The fix is to make the character knowledge vaguer on the specific detail while
    preserving the experiential context:
    - "The team feels like the acceptance workspace is carrying more load than it
      should — Levi keeps saying they need to sort out what runs where"

    This phase is iterative: run it, fix flagged passages, run again until clean.

Input:  Phase 4 narrative + Phase 3 discovery items.
Output: List of flagged inference paths with suggested rewrites.
"""

from .config import llm_call, save_phase_output, load_phase_output, load_markdown, save_markdown, DEFAULT_MODEL
import json

_SONNET_MODEL = "claude-sonnet-4-6"

REWRITE_SYSTEM_PROMPT = """\
You are editing a character knowledge narrative for a synthetic client persona. \
Your task is to apply a set of targeted rewrites to specific passages. \
Preserve every word of the narrative that is not being rewritten. \
Do not add, remove, or rephrase anything outside the listed passages."""

REWRITE_PROMPT = """\
Below is a character knowledge narrative followed by a list of passages that \
need to be rewritten to remove inference paths to gated discovery items.

For each passage, replace it in the narrative with the suggested rewrite. \
The goal is to preserve the experiential context (the persona's feelings, \
frustrations, and general awareness) while removing the specific detail that \
would let an LLM reason its way to the hidden fact.

Do not change anything else in the narrative.

NARRATIVE:
{narrative}

REWRITES TO APPLY:
{rewrites}

Return the complete updated narrative with all rewrites applied. \
Start directly with the narrative content — no preamble."""

_OPUS_MODEL = "claude-opus-4-6"

VALIDATION_SYSTEM_PROMPT = """\
You are a validation system for a knowledge-gated AI persona. Your task is to \
detect inference paths — places where the character knowledge narrative contains \
enough information for an LLM to reason its way to a specific discovery item.

You are thorough and conservative: flag anything where an LLM could plausibly \
make the inference, even if a human might not. LLMs are aggressive pattern \
matchers and will exploit any inferential connection."""

VALIDATION_PROMPT = """\
Below is a character knowledge narrative for a synthetic client persona, followed \
by the discovery items that should be structurally hidden from the persona LLM.

Your task: For each discovery item, read the character knowledge narrative and \
determine whether the narrative contains enough information for an LLM to infer \
the discovery item through reasoning.

CHARACTER KNOWLEDGE NARRATIVE:
{narrative}

DISCOVERY ITEMS:
{discovery_items}

For EACH discovery item, evaluate:
1. Is there a direct statement in the narrative that reveals this item?
2. Is there a combination of statements that, together, allow the LLM to reason \
   to this conclusion?
3. Is the narrative vague enough that the LLM cannot be confident about the \
   specific fact?

Respond with valid JSON:
{{
  "validation_results": [
    {{
      "discovery_item_id": "DI-01",
      "discovery_item_content": "...",
      "inference_risk": "NONE" | "LOW" | "MEDIUM" | "HIGH",
      "problematic_passage": "..." or null,
      "explanation": "...",
      "suggested_rewrite": "..." or null
    }}
  ],
  "summary": {{
    "total_items": ...,
    "no_risk": ...,
    "low_risk": ...,
    "medium_risk": ...,
    "high_risk": ...
  }}
}}

Be precise about which passage creates the inference path and why. For MEDIUM \
and HIGH risk items, provide a specific suggested rewrite of the problematic \
passage that preserves the experiential context while removing the inferential \
connection.
"""


def run_phase5(scenario_name: str, model: str = _OPUS_MODEL, persona_name: str = None) -> dict:
    """
    Validate the narrative against discovery items for inference paths.

    Args:
        scenario_name: Identifier for this scenario run.
        model: LLM model to use. Defaults to Opus for thorough inference detection.
        persona_name: Optional persona name for persona-scoped filenames.

    Returns:
        Validation results dict with flagged inference paths.
    """
    narrative_filename = f"phase4_narrative_{persona_name.lower()}.md" if persona_name else "phase4_narrative.md"
    narrative = load_markdown(scenario_name, narrative_filename)
    try:
        classification = load_phase_output(scenario_name, "phase3_5_completeness", persona_name=persona_name)
    except FileNotFoundError:
        classification = load_phase_output(scenario_name, "phase3_classified", persona_name=persona_name)
    discovery_items = classification["discovery_items"]

    prompt = VALIDATION_PROMPT.format(
        narrative=narrative,
        discovery_items=json.dumps(discovery_items, indent=2, ensure_ascii=False),
    )

    raw_response = llm_call(
        prompt,
        system=VALIDATION_SYSTEM_PROMPT,
        model=model,
        max_tokens=8192,
        temperature=0.2,
    )

    cleaned = raw_response.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1]
    if cleaned.endswith("```"):
        cleaned = cleaned.rsplit("```", 1)[0]
    cleaned = cleaned.strip()

    try:
        validation = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}") + 1
        if start >= 0 and end > start:
            validation = json.loads(cleaned[start:end])
        else:
            raise ValueError("Could not parse validation response as JSON.")

    output_path = save_phase_output(scenario_name, "phase5_validation", validation, persona_name=persona_name)

    summary = validation.get("summary", {})
    print(f"Phase 5 complete. Validation saved to: {output_path}")
    print(f"  No risk:    {summary.get('no_risk', '?')}")
    print(f"  Low risk:   {summary.get('low_risk', '?')}")
    print(f"  Medium risk: {summary.get('medium_risk', '?')}")
    print(f"  High risk:  {summary.get('high_risk', '?')}")

    # Flag items needing attention
    high_medium = [
        r for r in validation.get("validation_results", [])
        if r.get("inference_risk") in ("HIGH", "MEDIUM")
    ]

    if high_medium:
        print(f"\n  {len(high_medium)} items need attention:")
        for r in high_medium:
            print(f"    [{r['inference_risk']}] {r['discovery_item_id']}: "
                  f"{r['discovery_item_content'][:60]}...")
            if r.get("problematic_passage"):
                print(f"      Problem: \"{r['problematic_passage'][:80]}...\"")
    else:
        print(f"\n  No medium/high inference risks found. Proceed to Phase 6.")

    return validation


def run_phase5_with_autofix(
    scenario_name: str,
    validation_model: str = _OPUS_MODEL,
    rewrite_model: str = _SONNET_MODEL,
    max_iterations: int = 3,
    persona_name: str = None,
) -> dict:
    """
    Run Phase 5 with an automated fix loop.

    Each iteration:
      1. Validates the narrative (Opus).
      2. If HIGH/MEDIUM items remain, applies suggested rewrites (Sonnet).
      3. Saves the patched narrative and re-validates.

    Stops when clean or max_iterations is reached.

    Args:
        scenario_name: Identifier for this scenario run.
        validation_model: Model for inference path detection. Defaults to Opus.
        rewrite_model: Model for applying rewrites. Defaults to Sonnet.
        max_iterations: Maximum fix-and-revalidate cycles before giving up.
        persona_name: Optional persona name for persona-scoped filenames.

    Returns:
        Final validation results dict.
    """
    for attempt in range(1, max_iterations + 1):
        print(f"\n  --- Validation attempt {attempt}/{max_iterations} ---")
        validation = run_phase5(scenario_name, model=validation_model, persona_name=persona_name)

        high_medium = [
            r for r in validation.get("validation_results", [])
            if r.get("inference_risk") in ("HIGH", "MEDIUM")
            and r.get("suggested_rewrite")
        ]

        if not high_medium:
            print(f"\n  Narrative is clean. Proceed to Phase 6.")
            return validation

        if attempt == max_iterations:
            remaining = len(high_medium)
            print(f"\n  Max iterations reached. {remaining} item(s) still flagged.")
            print(f"  Proceeding — review phase5_validation output if needed.")
            return validation

        print(f"\n  Applying {len(high_medium)} rewrite(s) and re-validating...")
        _apply_rewrites(scenario_name, high_medium, rewrite_model, persona_name=persona_name)

    return validation


def _apply_rewrites(scenario_name: str, flagged_items: list, model: str, persona_name: str = None) -> None:
    """
    Apply suggested rewrites from validation results to the narrative.
    Uses an LLM so inexact passage quotes are handled gracefully.
    """
    narrative_filename = f"phase4_narrative_{persona_name.lower()}.md" if persona_name else "phase4_narrative.md"
    narrative = load_markdown(scenario_name, narrative_filename)

    rewrites_text = "\n\n".join(
        f"Item {r['discovery_item_id']}:\n"
        f"  Original passage: {r['problematic_passage']}\n"
        f"  Suggested rewrite: {r['suggested_rewrite']}"
        for r in flagged_items
        if r.get("problematic_passage") and r.get("suggested_rewrite")
    )

    if not rewrites_text:
        return

    prompt = REWRITE_PROMPT.format(narrative=narrative, rewrites=rewrites_text)
    updated_narrative = llm_call(
        prompt,
        system=REWRITE_SYSTEM_PROMPT,
        model=model,
        max_tokens=16384,
        temperature=0.1,
    )

    save_markdown(scenario_name, narrative_filename, updated_narrative)
    print(f"  Narrative updated with {len(flagged_items)} rewrite(s).")
