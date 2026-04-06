"""
Phase 6 — Assembly.

Two functions:

run_phase6(scenario_name, persona_name, persona_role):
    Per-persona assembly. Generates briefing, identity, maturity, personality,
    team members, and assembles them with the character knowledge narrative and
    discovery items into a single-persona scenario file. Called once per persona.
    Output: scenario_assembled_{persona}.md (workspace) +
            docs/scenarios/{name}_{persona}.md (draft, overwritten by Phase 7).

run_phase6_combine(scenario_name, personas):
    Multi-persona combine. Reads each persona's Phase 7 reviewed output
    (or Phase 6 assembled output as fallback) and combines them into a single
    file with shared header sections (Scenario Parameters, Topics, Consultant
    Briefing) followed by ## Persona: {name} sections for each persona.
    Called once, after all per-persona Phase 7 runs complete.
    Output: scenario_combined.md (workspace) +
            docs/scenarios/{scenario_name}.md (combined runtime file).

Design rationale:
    The single-persona assembly (run_phase6) generates LLM content specific to
    each persona's context — identity, personality, team members — and structures
    it as a self-contained scenario. The combine step (run_phase6_combine) then
    merges these into one file for review and eventual multi-persona runtime support.

    The briefing is persona-agnostic: it describes the engagement, not the persona.
    It belongs in the shared header of the combined file. Identity, maturity, and
    personality are persona-specific: they belong in each ## Persona section.
"""

from pathlib import Path
import re

from .config import (
    llm_call, save_phase_output, load_phase_output,
    load_markdown, save_markdown, WORKSPACE,
)
import json

# Project root is one level above this package.
_SCENARIOS_DIR = Path(__file__).resolve().parent.parent / "docs" / "scenarios"

BRIEFING_SYSTEM_PROMPT = """\
You are writing a consultant briefing for a training scenario. The briefing \
tells the consultant what they know going into the meeting — the engagement \
context, the client, and the expected outcome.

The briefing presents facts. It does NOT instruct the consultant on interview \
technique, question style, or what to do or avoid. The training system evaluates \
technique separately — the briefing's job is to set the scene."""

BRIEFING_PROMPT = """\
Write a consultant briefing for the following scenario. The briefing is what a \
project manager would tell a consultant before sending them into a client meeting \
for the first time.

SCENARIO PARAMETERS:
{scenario_params}

KEY CONTEXT FROM THE ENGAGEMENT (use only to understand the client's situation — \
ignore any names of consulting firms, individual consultants, or references to \
prior sessions that appear here):
{context_summary}

Write the briefing with these fields (keep each concise — 1-3 sentences):
- Engagement: What type of engagement this is
- Client context: The company, their industry, their platform, their team size
- What they asked for: Why they engaged external help
- Meeting type: What kind of meeting this is (derive from interview_stage in \
  scenario parameters)
- What is known going in: For initial_discovery — only high-level intake context: \
  the client's industry, general platform type, and the category of problem they \
  need help with. Do NOT include specific project names, initiative names, internal \
  team names, or any organisational detail from the context above — the consultant \
  discovers those through questioning.
- Expected outcome: What a successful meeting would produce (in terms of \
  understanding, not in terms of specific actions)

Do NOT include:
- Any consulting firm name or individual consultant name (the trainee reading this \
  briefing is unnamed — do not invent or reference a consultant name)
- References to previous sessions, engagement history, or prior interactions
- Commercial arrangements, contract terms, or pricing
- Specific internal project names, initiative names, or organisational terminology \
  from the client context — these belong in the discovery layer
- Instructions on how to conduct the interview
- Advice on question types or techniques
- Information that would reveal discovery items

Output as plain text (no markdown headers), each field on its own line with \
the field name followed by a colon.
"""

IDENTITY_PROMPT = """\
Write a brief identity description for a synthetic client persona. This is \
written in second person ("You are...") and establishes who the persona is and \
what this meeting is about. Keep it to 3-4 sentences.

Persona name: {persona_name}
Role: {persona_role}
Company: {company_name}
Industry: {industry}
Engagement context: {engagement_type}
Meeting type: {interview_stage}

Output as plain text, no headers.
"""

TEAM_MEMBERS_SYSTEM_PROMPT = """\
You are extracting team member information from consulting engagement facts. \
Your output will be used in a training scenario — it must be accurate, concise, \
and formatted consistently."""

TEAM_MEMBERS_PROMPT = """\
Below are classified facts from a client engagement. Extract all named team \
members (internal staff, not external vendors) and their roles.

ENGAGEMENT FACTS:
{facts_json}

For each internal team member found, output one bullet in this format:
- **Name**: brief role description (1 sentence max)

Rules:
- Include only people who work at the client organisation
- Do not include external vendors, consultants, or contractors
- If the same person appears in multiple facts, merge into one entry
- Role descriptions should be concrete (what they do, what they own, what they've raised)
- Do not include {persona_name} — they are the persona being interviewed

Output ONLY the bullet list, no preamble.
"""

PERSONALITY_SYSTEM_PROMPT = """\
You are writing a personality and communication style description for a synthetic \
client persona in a consultant training simulation. This section defines HOW the \
persona speaks and behaves — not what they know."""

PERSONALITY_PROMPT = """\
Write a personality and communication style description for {persona_name}, a \
{persona_role} at a {industry} company.

MATURITY LEVEL: {maturity_level}
{maturity_description}

ENGAGEMENT CONTEXT (for tone calibration — do not repeat these facts):
{context_sample}

Write 4-6 bullet points describing:
- Their general communication register (formal/informal, direct/cautious, etc.)
- How they frame problems (business terms, team complaints, operational impact)
- A natural quirk or conversational habit (hedging, analogies, self-deprecation, etc.)
- How they react when they don't understand something technical
- What makes them visibly engaged or frustrated in conversation

Rules:
- Describe HOW they speak, not WHAT they know
- Do not repeat maturity level rules — those are already defined separately
- Do not invent facts about the company or platform
- Keep each bullet to 1-2 sentences

Output ONLY the bullet list, no preamble.
"""


def run_phase6(
    scenario_name: str,
    persona_name: str,
    persona_role: str,
) -> str:
    """
    Assemble the final scenario file from all phase outputs.

    Args:
        scenario_name: Identifier for this scenario run.
        persona_name: Name of the persona (e.g., "Danny").
        persona_role: Role description (e.g., "manager of the data platform team").

    Returns:
        The complete scenario file as a markdown string.
    """
    # Load all inputs — prefer Phase 3.5 completeness output, fall back to Phase 3 classified
    try:
        classification = load_phase_output(scenario_name, "phase3_5_completeness", persona_name=persona_name)
    except FileNotFoundError:
        classification = load_phase_output(scenario_name, "phase3_classified", persona_name=persona_name)
    narrative_filename = f"phase4_narrative_{persona_name.lower()}.md"
    narrative = load_markdown(scenario_name, narrative_filename)
    params = classification["scenario_parameters"]
    discovery_items = classification["discovery_items"]
    char_knowledge = classification["character_knowledge"]

    # --- Generate briefing ---
    context_summary = _build_context_summary(char_knowledge, params)
    briefing_prompt = BRIEFING_PROMPT.format(
        scenario_params=json.dumps(params, indent=2),
        context_summary=context_summary,
        persona_name=persona_name,
        persona_role=persona_role,
    )
    briefing = llm_call(
        briefing_prompt,
        system=BRIEFING_SYSTEM_PROMPT,
        max_tokens=1024,
        temperature=0.3,
    )

    # --- Generate identity ---
    identity_prompt = IDENTITY_PROMPT.format(
        persona_name=persona_name,
        persona_role=persona_role,
        company_name=params.get("company_name", "Unknown"),
        industry=params.get("industry", "Unknown"),
        engagement_type=params.get("engagement_type", "platform_review"),
        interview_stage=params.get("interview_stage", "initial_discovery"),
    )
    identity = llm_call(identity_prompt, max_tokens=512, temperature=0.3)

    # --- Build topic taxonomy ---
    # Preference order: scenario_taxonomy.json (shared, generated once before Phase 3)
    # → phase3 stored taxonomy → fallback (derive from item tags)
    scenario_taxonomy_path = WORKSPACE / scenario_name / "scenario_taxonomy.json"
    if scenario_taxonomy_path.exists():
        taxonomy_dict = json.loads(scenario_taxonomy_path.read_text())
    else:
        taxonomy_dict = classification.get("topic_taxonomy")
    if taxonomy_dict:
        taxonomy_dict = _prune_taxonomy(taxonomy_dict, discovery_items + char_knowledge)
        # Write back pruned taxonomy so downstream steps and evaluation use the same set
        scenario_taxonomy_path.write_text(json.dumps(taxonomy_dict, indent=2, ensure_ascii=False))
        topics = "\n".join(f"{code}: {name}" for code, name in taxonomy_dict.items())
    else:
        topics = _build_topic_taxonomy(discovery_items, char_knowledge)

    # --- Build maturity section ---
    maturity = _build_maturity_section(params)

    # --- Format discovery items ---
    formatted_discoveries = _format_discovery_items(discovery_items)

    # --- Generate team members from character knowledge facts ---
    team_members = _generate_team_members(char_knowledge, persona_name)

    # --- Generate personality from maturity + role + engagement context ---
    personality = _generate_personality(params, persona_name, persona_role, char_knowledge)

    # --- Assemble ---
    scenario = _assemble_scenario(
        company_name=params.get("company_name", "Unknown Company"),
        industry=params.get("industry", "Unknown Industry"),
        params=params,
        topics=topics,
        briefing=briefing,
        persona_name=persona_name,
        identity=identity,
        maturity=maturity,
        personality=personality,
        narrative=narrative,
        team_members=team_members,
        discoveries=formatted_discoveries,
    )

    # Workspace: persona-scoped name for traceability and Phase 7 loading.
    assembled_filename = f"scenario_assembled_{persona_name.lower()}.md"
    output_path = save_markdown(scenario_name, assembled_filename, scenario)

    # docs/scenarios/: runtime-facing name for knowledge.py. Phase 7 will
    # overwrite this with the reviewed version once it runs.
    _SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
    runtime_dest = _SCENARIOS_DIR / f"{scenario_name}_{persona_name.lower()}.md"
    runtime_dest.write_text(scenario)

    print(f"Phase 6 complete. Assembled scenario saved to: {output_path} (workspace)")
    print(f"  Also written to: {runtime_dest} (draft — will be overwritten by Phase 7)")
    print(f"  Total length: ~{len(scenario.split())} words")
    print(f"  Discovery items: {len(discovery_items)}")
    print(f"\n  Final review checklist:")
    print(f"    - Does the briefing present facts without prescribing technique?")
    print(f"    - Does the identity feel natural and grounded?")
    print(f"    - Is the maturity level description accurate?")
    print(f"    - Are topic tags consistent across narrative and discovery items?")
    print(f"    - Would you want to interview this persona?")

    return scenario


def _build_context_summary(char_facts: list, params: dict) -> str:
    """Build a brief context summary for the briefing prompt."""
    sections = {}
    for fact in char_facts:
        section = fact.get("narrative_section", "general")
        if section not in sections:
            sections[section] = []
        sections[section].append(fact["content"])

    summary_parts = []
    for section, facts in sections.items():
        # Take first 3 facts per section for brevity
        summary_parts.append(f"{section}: " + "; ".join(facts[:3]))

    return "\n".join(summary_parts)


def _prune_taxonomy(taxonomy_dict: dict, items: list) -> dict:
    """
    Remove topic codes from taxonomy_dict that have no items tagged with them.

    Parent codes (e.g. "iam") are kept if any of their child codes
    (e.g. "iam/provisioning") are kept — they're needed as section headers
    in the topic coverage UI.

    Args:
        taxonomy_dict: Full taxonomy mapping code → display name.
        items: All character_knowledge + discovery_items across all personas.

    Returns:
        Pruned taxonomy dict preserving original ordering.
    """
    used_codes = {item.get("topic", "") for item in items if item.get("topic")}

    kept = {}
    for code, name in taxonomy_dict.items():
        # Keep if directly used, or if it's a parent of any used code
        if code in used_codes or any(c.startswith(code + "/") for c in used_codes):
            kept[code] = name

    removed = set(taxonomy_dict) - set(kept)
    if removed:
        print(f"  Pruned {len(removed)} unused taxonomy code(s): {', '.join(sorted(removed))}")

    return kept


def _build_topic_taxonomy(discoveries: list, char_knowledge: list) -> str:
    """Build topic taxonomy from all tagged items."""
    topics = {}
    for item in discoveries + char_knowledge:
        topic = item.get("topic", "")
        if topic and topic not in topics:
            # Generate display name from topic code
            display = topic.replace("/", " — ").replace("_", " ").title()
            topics[topic] = display

    lines = []
    # Sort by topic code for consistency
    for code in sorted(topics.keys()):
        lines.append(f"{code}: {topics[code]}")
    return "\n".join(lines)


def _build_maturity_section(params: dict) -> str:
    """Generate maturity level description based on parameters."""
    # persona_maturity drives behavior; fall back to legacy maturity_level for old scenarios
    level = params.get("persona_maturity", params.get("maturity_level", "MEDIUM"))

    descriptions = {
        "LOW": (
            "Level: LOW\n\n"
            "**Technical knowledge:** Has heard of common platform concepts and can "
            "use basic names in conversation, but cannot explain how they work or "
            "evaluate whether a proposed approach is correct. If the consultant uses "
            "implementation-level terms or acronyms, asks them to explain in plain "
            "language before continuing.\n\n"
            "**Self-awareness of problems:** Knows what is painful from what the team "
            "tells them, but does not know root causes or solutions. Describes symptoms "
            "and team frustrations — not diagnoses. When a question is too technical, "
            "tries to engage with what they do understand — reframes in their own terms, "
            "connects it to something the team has mentioned, or asks the consultant to "
            "explain simpler. Only says they don't know as a last resort after genuinely "
            "trying. Candid about the mess — not defensive or protective.\n\n"
            "**Response to proposals:** When the consultant proposes something concrete, "
            "engages from lived experience — connects it to a known pain point, flags "
            "a concern about how it would land with the team, or notes it sounds like "
            "something a team member has raised. May ask one clarifying question about "
            "practical implications. Cannot evaluate whether the approach is technically "
            "correct or commit to it."
        ),
        "MEDIUM": (
            "Level: MEDIUM\n\n"
            "**Technical knowledge:** Understands common platform concepts well enough "
            "to discuss them and has opinions about what works and what doesn't. Can "
            "follow technical proposals and ask relevant clarifying questions, but "
            "relies on the team for deep implementation details.\n\n"
            "**Self-awareness of problems:** Has a good sense of what's broken and "
            "some understanding of why. Can articulate problems in semi-technical "
            "terms and connect symptoms to likely causes based on team discussions.\n\n"
            "**Response to proposals:** Can engage substantively with proposals — "
            "evaluates feasibility from an operational perspective, raises concerns "
            "about team capacity or organizational constraints, and can discuss "
            "trade-offs at a conceptual level."
        ),
        "MEDIUM_HIGH": (
            "Level: MEDIUM_HIGH\n\n"
            "**Technical knowledge:** Deeply familiar with the platform's architecture "
            "from an operational perspective. Can discuss technical details about what "
            "exists and why it was built that way. Understands infrastructure "
            "configurations, networking, and access patterns within the current "
            "environment. Does NOT have deep platform-specific consulting expertise — "
            "engages with proposals based on operational feasibility, team constraints, "
            "and lived experience rather than vendor best practices.\n\n"
            "**Self-awareness of problems:** Understands root causes from an operational "
            "perspective and has opinions about what needs to change. Can articulate both "
            "symptoms and diagnoses with technical precision. May have attempted fixes "
            "that didn't work and can explain why.\n\n"
            "**Response to proposals:** Evaluates proposals based on operational impact — "
            "will push back on feasibility, ask about dependencies, flag team capacity "
            "constraints, and challenge assumptions about the current environment's "
            "readiness. Engages in architecture discussions from a practitioner's "
            "perspective. Does not evaluate whether a proposed pattern is the "
            "vendor-recommended best practice — that's what the consultant brings."
        ),
        "HIGH": (
            "Level: HIGH\n\n"
            "**Technical knowledge:** Deeply familiar with the platform and its "
            "architecture. Can discuss technical details, evaluate proposals on their "
            "merits, and push back with informed reasoning. May have implemented "
            "solutions themselves.\n\n"
            "**Self-awareness of problems:** Understands root causes and has opinions "
            "about solutions. Can articulate both symptoms and diagnoses with precision.\n\n"
            "**Response to proposals:** Can evaluate proposals technically, compare "
            "with approaches they've considered, and engage in detailed architecture "
            "discussions. May challenge the consultant's assumptions."
        ),
    }
    return descriptions.get(level, descriptions["MEDIUM"])


def _format_discovery_items(items: list) -> str:
    """Format discovery items for the scenario file."""
    lines = []
    for i, item in enumerate(items, 1):
        topic = item.get("topic", "general")
        content = item["content"]
        item_id = item.get("id", f"DI-{i:02d}")
        lines.append(f"- [{item_id}] [topic: {topic}] {content}")
    return "\n".join(lines)


def _generate_team_members(char_knowledge: list, persona_name: str) -> str:
    """
    Generate team member list from character knowledge facts using an LLM.
    Extracts named internal staff and their roles.
    """
    prompt = TEAM_MEMBERS_PROMPT.format(
        facts_json=json.dumps(char_knowledge, indent=2, ensure_ascii=False),
        persona_name=persona_name,
    )
    return llm_call(
        prompt,
        system=TEAM_MEMBERS_SYSTEM_PROMPT,
        max_tokens=1024,
        temperature=0.1,
    )


def _sample_for_personality(char_knowledge: list, facts_per_section: int = 3) -> list:
    """
    Sample character knowledge facts evenly across narrative sections.

    Takes up to `facts_per_section` facts from each narrative_section so that
    personality generation is calibrated against all dimensions of the persona's
    situation (history, team dynamics, mental model, etc.) rather than whichever
    section Phase 3 happened to output first.
    """
    by_section: dict[str, list] = {}
    for fact in char_knowledge:
        section = fact.get("narrative_section", "uncategorized")
        by_section.setdefault(section, []).append(fact)

    sampled = []
    for section_facts in by_section.values():
        sampled.extend(section_facts[:facts_per_section])
    return sampled


def _generate_personality(
    params: dict,
    persona_name: str,
    persona_role: str,
    char_knowledge: list,
) -> str:
    """
    Generate personality and communication style from maturity level,
    role, industry, and a cross-section of the engagement context.
    """
    maturity_level = params.get("persona_maturity", params.get("maturity_level", "MEDIUM"))
    maturity_description = _build_maturity_section(params)

    # Sample evenly across narrative sections (3 per section, ~18 total).
    # This ensures personality is calibrated against team dynamics, mental model,
    # and attempted solutions — not just organizational history facts that happen
    # to appear first in Phase 3 output.
    context_sample = "\n".join(
        f"- {item['content']}"
        for item in _sample_for_personality(char_knowledge)
    )

    prompt = PERSONALITY_PROMPT.format(
        persona_name=persona_name,
        persona_role=persona_role,
        industry=params.get("industry", "unknown"),
        maturity_level=maturity_level,
        maturity_description=maturity_description,
        context_sample=context_sample,
    )
    return llm_call(
        prompt,
        system=PERSONALITY_SYSTEM_PROMPT,
        max_tokens=1024,
        temperature=0.4,
    )


def _assemble_scenario(
    company_name: str,
    industry: str,
    params: dict,
    topics: str,
    briefing: str,
    persona_name: str,
    identity: str,
    maturity: str,
    personality: str,
    narrative: str,
    team_members: str,
    discoveries: str,
) -> str:
    """Assemble all components into the final scenario file."""

    persona_maturity = params.get("persona_maturity", params.get("maturity_level", "MEDIUM"))
    platform_maturity = params.get("platform_maturity", params.get("maturity_level", "MEDIUM"))
    scenario_params_section = (
        f"## Scenario Parameters\n"
        f"Platform Maturity: {platform_maturity}\n"
        f"Persona Maturity: {persona_maturity}\n"
        f"Engagement Type: {params.get('engagement_type', 'platform_review')}\n"
        f"Interview Stage: {params.get('interview_stage', 'initial_discovery')}\n"
        f"Cloud Platform: {params.get('cloud_platform', 'azure')}\n"
        f"Primary Problem Clusters: {', '.join(params.get('primary_problem_clusters', []))}\n"
        f"Team Size: {params.get('team_size', 'unknown')}\n"
    )

    return f"""\
# Scenario: {company_name} — {industry}

{scenario_params_section}

## Topics
{topics}

## Consultant Briefing
{briefing}

## Identity
{identity}

## Maturity Level
{maturity}

## Personality and Communication Style
{personality}

## Character Knowledge
{narrative}

## Team Members
{team_members}

## Discovery Items
{discoveries}
"""


# ---------------------------------------------------------------------------
# Multi-persona combine
# ---------------------------------------------------------------------------

def _extract_section(text: str, header: str) -> str:
    """Extract the content of a ## section from scenario markdown."""
    pattern = rf"(?m)^## {re.escape(header)}\s*\n(.*?)(?=\n## |\Z)"
    match = re.search(pattern, text, re.DOTALL)
    return match.group(1).strip() if match else ""


def run_phase6_combine(scenario_name: str, personas: list) -> str:
    """
    Combine per-persona Phase 7 outputs into a single multi-persona scenario file.

    Reads scenario_final_reviewed_{persona}.md for each persona (falls back to
    scenario_assembled_{persona}.md if Phase 7 has not run yet). Extracts shared
    sections (Scenario Parameters, Topics, Consultant Briefing) from the first
    persona's file, then assembles per-persona sections under ## Persona: {name}.

    Args:
        scenario_name: Identifier for this scenario run.
        personas: List of dicts with keys: name, role, maturity.
                  e.g. [{'name': 'Danny', 'role': '...', 'maturity': 'LOW'}, ...]

    Returns:
        The combined scenario markdown string.
    """
    ws = WORKSPACE / scenario_name

    # --- Load per-persona source files ---
    persona_texts = {}
    for p in personas:
        name = p["name"]
        reviewed = ws / f"scenario_final_reviewed_{name.lower()}.md"
        assembled = ws / f"scenario_assembled_{name.lower()}.md"
        if reviewed.exists():
            persona_texts[name] = reviewed.read_text()
            print(f"  {name}: loaded from Phase 7 output (reviewed)")
        elif assembled.exists():
            persona_texts[name] = assembled.read_text()
            print(f"  {name}: loaded from Phase 6 output (assembled, Phase 7 not run)")
        else:
            raise FileNotFoundError(
                f"No Phase 6 or Phase 7 output found for persona '{name}' "
                f"in scenario '{scenario_name}'. "
                f"Expected one of: {reviewed}, {assembled}"
            )

    first_text = persona_texts[personas[0]["name"]]

    # --- Extract title from first persona's file ---
    title_match = re.match(r"^(#[^\n]+)", first_text)
    title = title_match.group(1) if title_match else f"# Scenario: {scenario_name}"

    # --- Build shared header ---
    parts = [title, ""]

    # Scenario Parameters: strip persona-specific maturity line, keep platform-level fields
    params_content = _extract_section(first_text, "Scenario Parameters")
    if params_content:
        # Remove "Persona Maturity: ..." line — it varies per persona; goes in each persona block
        cleaned_params = "\n".join(
            line for line in params_content.splitlines()
            if not line.strip().startswith("Persona Maturity:")
        ).strip()
        parts.append("## Scenario Parameters")
        parts.append(cleaned_params)
        parts.append("")

    # Topics: prefer scenario_taxonomy.json (authoritative shared taxonomy) over
    # extracting from the first persona's file (which may be persona-scoped).
    # Prune to codes actually used across all personas before writing.
    scenario_taxonomy_path = ws / "scenario_taxonomy.json"
    if scenario_taxonomy_path.exists():
        taxonomy_dict = json.loads(scenario_taxonomy_path.read_text())
        # Collect all items across all personas to prune unused codes
        all_items = []
        for p in personas:
            for phase in ("phase3_5_completeness", "phase3_classified"):
                try:
                    cls = load_phase_output(scenario_name, phase, persona_name=p["name"])
                    all_items += cls.get("discovery_items", []) + cls.get("character_knowledge", [])
                    break
                except FileNotFoundError:
                    continue
        taxonomy_dict = _prune_taxonomy(taxonomy_dict, all_items)
        scenario_taxonomy_path.write_text(json.dumps(taxonomy_dict, indent=2, ensure_ascii=False))
        topics_content = "\n".join(f"{code}: {name}" for code, name in taxonomy_dict.items())
        parts.append("## Topics")
        parts.append(topics_content)
        parts.append("")
    else:
        topics_content = _extract_section(first_text, "Topics")
        if topics_content:
            parts.append("## Topics")
            parts.append(topics_content)
            parts.append("")

    briefing_content = _extract_section(first_text, "Consultant Briefing")
    if briefing_content:
        parts.append("## Consultant Briefing")
        parts.append(briefing_content)
        parts.append("")

    # --- Build per-persona sections ---
    # Source header → destination header in the combined file
    _PERSONA_SECTION_MAP = [
        ("Identity", "Identity"),
        ("Maturity Level", "Persona Maturity"),
        ("Personality and Communication Style", "Personality and Communication Style"),
        ("Character Knowledge", "Character Knowledge"),
        ("Team Members", "Team Members"),
        ("Discovery Items", "Discovery Items"),
    ]

    for p in personas:
        name = p["name"]
        text = persona_texts[name]

        parts.append(f"## Persona: {name}")
        parts.append("")

        for src_header, dst_header in _PERSONA_SECTION_MAP:
            content = _extract_section(text, src_header)
            if content:
                # The narrative (Character Knowledge) opens with its own ### heading
                # inside the ## section. Strip it to avoid a duplicate heading.
                first_line = content.split("\n", 1)[0].strip()
                if re.match(r"^#{2,4}\s+" + re.escape(dst_header), first_line, re.IGNORECASE):
                    content = content.split("\n", 1)[1].strip() if "\n" in content else ""
                if content:
                    parts.append(f"### {dst_header}")
                    parts.append(content)
                    parts.append("")

        parts.append("")

    combined = "\n".join(parts)

    # --- Save ---
    combined_ws_path = save_markdown(scenario_name, "scenario_combined.md", combined)

    _SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
    runtime_dest = _SCENARIOS_DIR / f"{scenario_name}.md"
    runtime_dest.write_text(combined)

    persona_names = ", ".join(p["name"] for p in personas)
    print(f"\nPhase 6 combine complete.")
    print(f"  Combined scenario: {combined_ws_path} (workspace)")
    print(f"  Also written to: {runtime_dest} (runtime)")
    print(f"  Personas: {persona_names}")
    print(f"  Total length: ~{len(combined.split())} words")

    return combined
