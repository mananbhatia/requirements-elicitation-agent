"""
Phase 4 — Character Narrative Generation.

Purpose:
    Transform classified character knowledge facts into a cohesive, multi-page
    narrative that describes the persona's experience and understanding of their
    situation. This is what makes the persona feel like a real person with context,
    history, and opinions — not a fact-dispensing machine.

Design rationale:
    The narrative serves two functions:

    1. Conversational grounding: The persona LLM uses this as its knowledge base
       for free-form conversation. A consultant can ask about organizational
       history, team dynamics, or business pressures and get natural, contextual
       responses — without triggering the retrieval gate.

    2. Training realism: Sander's feedback ("In reality Danny can tell about and
       discuss strategies. The absence of one would make me not even care about
       their issues") directly motivated this. A persona without narrative context
       produces flat, uninformative conversations that don't feel like real client
       interactions.

    The narrative is written in third person (what the persona knows/thinks/feels)
    because it's a system prompt component, not dialogue. It uses topic markers
    for coverage tracking.

    Critical constraint: The narrative must NOT contain specific facts classified
    as discovery items, and must NOT create inference paths to discovery items.
    This is verified in Phase 5, but the generation prompt encodes the constraint.

    Per Grice's Cooperative Principle: the narrative should make the persona
    a cooperative conversational partner who shares context naturally. The gated
    discovery items are the exception — specific facts that require specific
    questions to surface.

Input:  Phase 3 classification output (character_knowledge + discovery_items).
Output: Rich narrative markdown (~1500-2500 words) with topic markers.
"""

from .config import llm_call, save_phase_output, load_phase_output, save_markdown, DEFAULT_MODEL
import json

_OPUS_MODEL = "claude-opus-4-6"

NARRATIVE_SYSTEM_PROMPT = """\
You are writing the character knowledge narrative for a synthetic client persona \
in a consultant training simulation. This narrative becomes part of the persona \
LLM's system prompt — it defines what the persona knows and how they experience \
their situation.

Write in third person (describing what the persona knows, thinks, and feels). \
The narrative should read as a coherent story of someone's professional experience, \
not as a list of facts. Use natural language appropriate to the persona's level of \
technical understanding."""

NARRATIVE_PROMPT = """\
Write a rich character knowledge narrative for a synthetic client persona based on \
the classified facts below.

PERSONA CONTEXT:
{scenario_params}

CHARACTER KNOWLEDGE FACTS (these must ALL be incorporated into the narrative):
{character_facts}

DISCOVERY ITEMS (these must NOT appear in the narrative — listed here so you can \
avoid creating inference paths to them):
{discovery_items}

INSTRUCTIONS:

1. Structure the narrative with these sub-sections (use #### headings):
   - Organizational History: How the company/team got to the current state
   - Strategic Context: Business priorities, strategy (or lack thereof), pressures
   - Current Platform State: What the persona knows about the current setup
   - Team Dynamics: Who does what, relationships, frustrations, trust
   - Attempted Solutions: What has been tried, why it didn't work
   - External Relationships: Vendors, partners, how the persona experiences them
   - {persona_name}'s Mental Model: How the persona frames their situation, what \
     worries them, what they're pragmatic about, how they'd describe the situation \
     to someone new

2. Tag each paragraph or section with topic markers for coverage tracking:
   [topic: iam], [topic: workspace], [topic: governance], [topic: security], etc.
   Use the same topic codes that appear in the discovery items.

3. Write from the persona's EXPERIENCE, not as a technical audit:
   - Instead of: "The workspaces lack proper isolation controls"
   - Write: "{persona_name} knows things feel disorganized — the team keeps mentioning \
     that environments aren't properly separated, though they can't articulate exactly \
     what that means technically"

4. Where source material has gaps (organizational history, emotional context, \
   attempted solutions that aren't in the facts), generate plausible content \
   consistent with all known facts. Mark generated content with [generated] tags \
   so the human reviewer can verify it.

5. CRITICAL — Avoid inference paths to discovery items:
   - Do NOT include specific facts that would let the LLM reason its way to a \
     discovery item
   - Example: If a discovery item is "production jobs run on acceptance", the \
     narrative can say the persona senses things aren't running where they should, \
     but must NOT say "the acceptance workspace handles production workloads"
   - When in doubt, keep the narrative vague on the specific detail while preserving \
     the experiential context

6. Length: Write as much as needed to incorporate every character knowledge fact \
   naturally and completely. Do not pad, but do not truncate. Coverage of all \
   {n_facts} facts is the goal — length is a consequence, not a target.

7. Do NOT use bullet points, numbered lists, or markdown formatting within sections. \
   Write in natural prose paragraphs.

Output the narrative as markdown starting with:
### Character Knowledge
"""


def run_phase4(
    scenario_name: str,
    persona_name: str = "Danny",
    model: str = _OPUS_MODEL,
) -> str:
    """
    Generate the character knowledge narrative from classified facts.

    Loads from phase3_5_completeness_{persona}.json if it exists, falling back
    to phase3_classified_{persona}.json. Saves as phase4_narrative_{persona}.md.

    Args:
        scenario_name: Identifier for this scenario run.
        persona_name: Name of the persona (used in prompt, section headings, and filenames).
        model: LLM model to use. Defaults to Opus for higher narrative quality.

    Returns:
        The generated narrative as a markdown string.
    """
    # Prefer Phase 3.5 completeness output if it exists (has generated gap-fill facts)
    try:
        classification = load_phase_output(scenario_name, "phase3_5_completeness", persona_name=persona_name)
    except (FileNotFoundError, KeyError):
        classification = load_phase_output(scenario_name, "phase3_classified", persona_name=persona_name)

    params = classification["scenario_parameters"]
    char_facts = classification["character_knowledge"]
    disc_items = classification["discovery_items"]

    n_facts = len(char_facts)

    prompt = NARRATIVE_PROMPT.format(
        scenario_params=json.dumps(params, indent=2),
        character_facts=json.dumps(char_facts, indent=2, ensure_ascii=False),
        discovery_items=json.dumps(disc_items, indent=2, ensure_ascii=False),
        persona_name=persona_name,
        n_facts=n_facts,
    )

    narrative = llm_call(
        prompt,
        system=NARRATIVE_SYSTEM_PROMPT,
        model=model,
        max_tokens=16384,
        temperature=0.4,  # slightly higher for natural prose
    )

    # Save as markdown for easy human review, persona-scoped filename
    narrative_filename = f"phase4_narrative_{persona_name.lower()}.md"
    output_path = save_markdown(scenario_name, narrative_filename, narrative)
    print(f"Phase 4 complete. Narrative saved to: {output_path}")
    print(f"  Word count: ~{len(narrative.split())}")
    print(f"\n  Review the narrative for:")
    print(f"    - Are all character knowledge facts incorporated?")
    print(f"    - Does the persona feel knowledgeable and realistic?")
    print(f"    - Are [generated] sections plausible?")
    print(f"    - Are there inference paths to discovery items? (Phase 5 checks this)")
    print(f"\n  Edit the narrative file directly, then proceed to Phase 5.")

    return narrative
