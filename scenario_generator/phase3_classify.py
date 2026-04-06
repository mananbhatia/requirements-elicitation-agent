"""
Phase 3 — Knowledge Classification.

Purpose:
    Classify each extracted fact into one of three buckets:
    - Character knowledge: Goes into the persona's rich narrative, always visible
    - Discovery item: Gated behind the retrieval system, revealed only when earned
    - Drop: Too granular, duplicative, or irrelevant for training value

Design rationale:
    This classification is the most consequential step in scenario construction.
    It directly determines the training experience: too many discovery items and
    the persona feels empty (the problem with GreenCycle v1); too few and there's
    nothing to discover.

    Two tests drive classification:

    1. The "aha" test: Would a consultant say "aha, I didn't know that" upon
       learning this? If yes → discovery item. This test identifies facts where
       premature disclosure would undermine training value.

    2. The "natural disclosure" test: Would a real manager at this maturity level
       share this naturally when the relevant topic comes up? If yes → character
       knowledge. A manager would naturally mention "we have three workspaces" when
       discussing infrastructure. They would NOT naturally blurt out "production
       jobs are running on acceptance."

    The default should be character knowledge. Too many discovery items leaves the
    persona feeling empty — no freely available knowledge for natural conversation.
    The persona should have generous character knowledge and a focused set of
    12-15 genuine discovery items.

    Per Grice's Cooperative Principle: a real client cooperates in conversation.
    They share context naturally. They only withhold specific details they
    wouldn't think to mention unless directly asked about them.

Input:  Extraction output (from Phase 1 or Phase 2).
Output: Classified facts with rationale for each classification.
"""

from .config import llm_call, save_phase_output, load_phase_output, NARRATIVE_SECTIONS, WORKSPACE, ensure_workspace
import json

_OPUS_MODEL = "claude-opus-4-6"
_SONNET_MODEL = "claude-sonnet-4-6"

REWRITE_SYSTEM_PROMPT = """\
You are rewriting discovery items for a synthetic client persona in a consultant \
training simulation. Discovery items are facts the persona knows but only reveals \
when asked a specific question. They must be written in plain language — the way \
the persona would think about and describe their situation, not how a consultant \
or technical document would describe it."""

REWRITE_PROMPT = """\
Process each discovery item below in two steps: (1) rewrite the content into \
plain client language, then (2) check whether a person at this maturity level \
would actually know this specific detail.

PERSONA MATURITY: {persona_maturity}

STEP 1 — LANGUAGE REWRITE RULES:

1. Remove recommendation language: "is recommended", "should be", "best practice \
   is", "migration to X is recommended", "should consider", "needs to be". \
   State what currently exists or is happening — not what should change.

2. Remove technical jargon above the persona's maturity level (persona_maturity above):
   - LOW: remove implementation-level acronyms and product names the persona \
     wouldn't know (CMK, SCIM, NCC, DABs, SRA, NSG, route tables, domain \
     controllers). Replace with plain descriptions of the situation or symptom.
   - MEDIUM: keep common platform concept names but remove implementation details.
   - HIGH: keep technical terms but remove consultant/vendor framing.

3. Write from the client's perspective — what they experience or what their team \
   has told them, not a technical audit finding.

Example rewrite:
  Before: "Encryption at rest is not enabled on the platform; migration to \
  Customer Managed Keys is recommended."
  After: "Nobody has set up encryption on the data stored in the platform."

STEP 2 — MATURITY AUDIT:

After rewriting, ask: would a real person at this maturity level actually know \
this specific fact — not just the symptom, but the specific detail stated?

Maturity epistemic access:
- LOW: knows symptoms and team complaints. Knows things are broken or painful. \
  Does NOT know infrastructure configurations, network topology, authentication \
  protocols, specific product settings, or implementation-level decisions. \
  Example of what LOW knows: "the team is always fighting over who can access \
  what." Example of what LOW does NOT know: "AD-integrated DNS with domain \
  controllers in Azure", "route tables force traffic through firewall".
- MEDIUM: knows named platform concepts and can describe specific symptoms with \
  some precision. Knows what the team has told them about root causes. Does NOT \
  know deep implementation details or specific configuration values.
- HIGH: knows most technical specifics. Maturity mismatch is rare at this level.

If the item describes a fact the persona at this maturity level would not know, \
set maturity_mismatch to true and write a maturity_note explaining:
- What the persona would actually know at their level (the symptom or complaint \
  version of this fact), OR
- Whether this item is purely engineering knowledge that cannot be expressed \
  in client terms — in which case note "Move to technical persona (e.g., Sajith)".

Do NOT remove or modify items that fail the maturity check — flag them for the \
human reviewer. The rewritten content should still be the best plain-language \
version of the fact; maturity_mismatch is a separate flag, not a reason to \
skip the rewrite.

If the item passes the maturity check, set maturity_mismatch to false and omit \
maturity_note (or set it to an empty string).

DISCOVERY ITEMS:
{items_json}

Return ONLY valid JSON — a list of objects. For each item, include all original \
fields plus: rewrite the "content" field, add "maturity_mismatch" (bool), and \
add "maturity_note" (string, empty if no mismatch). Preserve "id", \
"original_fact_id", "topic", and "aha_rationale" exactly as given.
"""

REFINEMENT_SYSTEM_PROMPT = """\
You are reviewing a list of proposed discovery items for a synthetic client persona \
in a consultant training system. Discovery items are facts gated behind the retrieval \
system — the persona cannot share them until a consultant asks a specific enough question.

Your job is quality control: apply the "aha test" strictly and move items back to \
character knowledge if they don't pass. Err on the side of fewer, stronger discovery items."""

REFINEMENT_PROMPT = """\
A first-pass classification has proposed the discovery items listed below. \
Review each one and decide whether it genuinely belongs behind the retrieval gate.

THE CLASSIFICATION PRINCIPLE:
An item is a discovery item if and only if BOTH conditions hold:
1. A real client in this role would NOT mention this naturally when the topic \
   comes up — it requires a specific, targeted question to surface.
2. Premature disclosure would meaningfully reduce training value — the consultant \
   should have to earn this fact, not receive it for free.

Move an item back to CHARACTER_KNOWLEDGE if either condition fails:
- The client would share it naturally when discussing the relevant topic
- It's organisational or infrastructure context the client knows openly in their role
- Withholding it would make the persona feel unrealistically guarded or unhelpful
- A consultant would reasonably assume this without being told

Additionally, if any discovery item describes the consulting engagement itself \
(staffing plans, commercial terms, named consultants from the consulting firm, \
deliverables, timelines, solution accelerators, partner rates), move it to a \
separate "drop" list with move_reason "engagement_logistics". These describe the \
consulting relationship, not the client's situation.

Keep as DISCOVERY_ITEM only when both conditions hold strongly:
- It's a specific misconfiguration, gap, or buried problem
- A consultant cannot assume it — they must ask precisely to surface it
- Revealing it early would let the consultant skip real discovery work

Do not move items based on quantity — move them based on whether they pass both \
conditions. The right number of discovery items is whatever the engagement warrants. \
An item that passes both conditions stays. An item that fails either condition moves.

SCENARIO PARAMETERS:
{scenario_params}

PROPOSED DISCOVERY ITEMS:
{discovery_items}

CHARACTER KNOWLEDGE (for context — do not modify these):
{char_summary}

Respond ONLY with valid JSON. Items moved to character knowledge need a \
narrative_section assigned. Items that stay keep their existing fields unchanged.

{{
  "keep_as_discovery": [
    {{
      "id": "...",
      "original_fact_id": "...",
      "content": "...",
      "topic": "...",
      "aha_rationale": "..."
    }}
  ],
  "move_to_character": [
    {{
      "id": "...",
      "content": "...",
      "narrative_section": "...",
      "topic": "...",
      "move_reason": "..."
    }}
  ]
}}
"""

CLASSIFICATION_SYSTEM_PROMPT = """\
You are classifying facts for a synthetic client persona in a consultant training \
system. The persona simulates a specific person — with a defined role, seniority, \
and technical knowledge level — in a requirements elicitation interview.

The system uses knowledge gating: the persona LLM only sees character knowledge \
(always visible) and whatever discovery items the consultant has earned through \
specific questioning. Discovery items are structurally excluded from the LLM's \
context until matched by the retrieval system.

Your classification must be persona-specific: a manager without deep technical \
knowledge would not know the same facts as an engineer who built the platform. \
Classify generously toward character knowledge — the persona should feel \
knowledgeable about their own situation. Reserve discovery items for genuine \
surprises that reward specific, targeted questioning."""

CLASSIFICATION_PROMPT = """\
Below are extracted facts from a client engagement. You are classifying them for \
a specific persona. Apply a three-step filter to each fact.

PERSONA:
- Name: {persona_name}
- Role: {persona_role}
- Maturity: {persona_maturity} (describes their technical knowledge level)

STEP 1 — PERSONA FILTER (apply first, before CK vs DI):

Ask: given this persona's role and technical maturity, would they plausibly have \
access to this knowledge?

- KNOWS FULLY: The persona would know this fact in the form stated (or close to it). \
  → Proceed to Step 2 (CK vs DI classification).
- KNOWS VAGUER: The persona would know a less precise version — the symptom or \
  team complaint, not the underlying configuration or root cause. \
  → Classify as CHARACTER_KNOWLEDGE. Translate content to what they'd actually know.
- WOULD NOT KNOW: This is engineering-level detail, a decision made above their \
  pay grade, or information their role simply wouldn't surface. \
  → DROP with drop_reason "persona_would_not_know". Write a brief note on what \
  persona would know instead (or "move to technical persona").

Maturity epistemic access (use this to calibrate KNOWS vs WOULD NOT KNOW):
- LOW: knows symptoms, team complaints, and general pain points. Does NOT know \
  infrastructure configs, network topology, authentication protocols, product-specific \
  settings, or implementation decisions. E.g., LOW knows "the team is always fighting \
  about access", NOT "AD-integrated DNS with domain controllers in Azure".
- MEDIUM: knows named platform concepts and can describe specific symptoms with \
  some precision. Knows what the team told them about root causes. Does NOT know \
  deep configuration details or specific values.
- HIGH: knows most technical specifics. Persona filter rarely rejects at this level.

STEP 2 — CHARACTER KNOWLEDGE vs DISCOVERY ITEM (only for facts that passed Step 1):

CHARACTER_KNOWLEDGE — The persona knows this and will share it naturally when \
the relevant topic comes up.

Classify as character knowledge when:
- A real client in this role would mention this naturally as context
- It describes their experience, frustrations, or perspective
- It's general infrastructure or organizational context
- Withholding it would make the persona feel unrealistically uninformed

DISCOVERY_ITEM — Gated behind the retrieval system. The persona cannot share this \
until a consultant asks a specific enough question.

Classify as discovery item when:
- A consultant would say "aha, I didn't know that" upon learning this
- Premature disclosure would undermine training value
- It's a specific technical finding or buried problem requiring targeted questioning
- It represents a security issue, misconfiguration, or governance gap

STEP 3 — DROP (engagement logistics or irrelevant):

Classify as drop when (use drop_reason below):
- "duplicate": duplicates another extracted fact
- "irrelevant": irrelevant to the training scenario or purely administrative
- "engagement_logistics": describes the consulting engagement itself rather than \
  the client's situation. Drop ANY fact that contains: the consulting firm's name, \
  individual consultant names (people from the consulting firm), references to prior \
  consulting sessions or meetings that have already occurred, engagement history, \
  staffing plans, commercial terms, deliverables, timelines, solution accelerator \
  offerings, partner day rates. \
  Note: generic references to "working with external consultants" or "a third party \
  helped set this up" can remain as CK if they describe the client's situation — \
  only drop when the fact is specifically about the consulting engagement.
- "persona_would_not_know": engineering detail outside this persona's epistemic access.

IMPORTANT: The default should be CHARACTER_KNOWLEDGE. Only classify as \
DISCOVERY_ITEM when you have a strong reason — the "aha" test. The persona should \
be rich and knowledgeable, not sparse and empty.

For character knowledge items, also assign a narrative_section:
- organizational_history: How they got to the current state
- strategic_context: Business priorities, strategy, pressures
- current_platform_state: What the platform looks like now
- team_dynamics: Who does what, interpersonal dynamics
- attempted_solutions: What they've tried and why it didn't work
- external_relationships: Vendors, partners, contractors
- mental_model: How the persona understands their own situation

For discovery items, also assign a topic tag (e.g., "iam/provisioning", \
"workspace/usage", "security/network") and provide a brief rationale for why \
this is an "aha moment."

SCENARIO PARAMETERS:
{scenario_params}

EXTRACTED FACTS:
{facts_json}

Respond ONLY with valid JSON:
{{
  "character_knowledge": [
    {{
      "id": "F001",
      "content": "...",
      "narrative_section": "...",
      "topic": "..."
    }}
  ],
  "discovery_items": [
    {{
      "id": "DI-01",
      "original_fact_id": "F005",
      "content": "...",
      "topic": "...",
      "aha_rationale": "..."
    }}
  ],
  "dropped": [
    {{
      "id": "F003",
      "content": "...",
      "reason": "...",
      "drop_reason": "..."
    }}
  ]
}}
"""


TAXONOMY_SYSTEM_PROMPT = """\
You are designing a topic taxonomy for a consultant training scenario. The taxonomy \
defines the domains a consultant would explore during a requirements elicitation \
interview with a client about their data platform."""

TAXONOMY_PROMPT = """\
Below are classified facts from a client engagement. Design a hierarchical topic \
taxonomy that covers the domains these facts represent.

RULES:
- 4-6 parent topics, each with 2-4 subtopics
- Topics represent domains a consultant would explore — not document sections or \
  audit categories
- Exclude administrative categories: hiring plans, deliverables, engagement timelines, \
  business cases, planning activities, consultant staffing
- Parent topic codes: short lowercase identifiers (e.g. "iam", "workspace", "governance")
- Subtopic codes: parent/child format (e.g. "iam/provisioning", "iam/rbac")
- Display names: human-readable, consultant-facing (e.g. "Identity & Access Management")
- Every fact below should map to at least one subtopic; design the taxonomy to cover \
  the actual content, not generic data platform categories

ALL FACTS (character knowledge + discovery items):
{all_facts}

SCENARIO PARAMETERS:
{scenario_params}

Respond ONLY with valid JSON — a flat dict mapping each code to its display name. \
Parent topics and subtopics in the same dict, ordered parent-first.

Example structure:
{{
  "iam": "Identity & Access Management",
  "iam/provisioning": "User Provisioning",
  "iam/rbac": "Role-Based Access Control",
  "workspace": "Workspace Architecture",
  "workspace/isolation": "Environment Isolation"
}}
"""

RETAG_SYSTEM_PROMPT = """\
You are updating topic tags on classified facts for a synthetic client training \
scenario. You have a defined taxonomy — map each fact to the most specific \
matching subtopic."""

RETAG_PROMPT = """\
Update the "topic" field on each fact below to use the correct code from the \
provided taxonomy. Use the most specific subtopic that fits (e.g. "iam/provisioning" \
not "iam"). If no subtopic fits well, use the parent topic code. If the fact \
genuinely doesn't fit any topic, use an empty string.

TAXONOMY:
{taxonomy}

CHARACTER KNOWLEDGE FACTS:
{char_facts}

DISCOVERY ITEMS:
{discovery_items}

Respond ONLY with valid JSON:
{{
  "character_knowledge": [ ...same structure as input, with updated "topic" fields... ],
  "discovery_items": [ ...same structure as input, with updated "topic" fields... ]
}}
"""

DEDUP_SYSTEM_PROMPT = """\
You are checking for duplicates between two lists of facts in a knowledge-gated \
training scenario. Character knowledge facts are always visible to the persona. \
Discovery items are structurally hidden until earned. If both lists contain the \
same underlying information, the character knowledge fact must be removed — \
the discovery item takes priority because gating is architectural."""

DEDUP_PROMPT = """\
Below are character knowledge facts and discovery items for a synthetic client persona.

For each character knowledge fact, determine whether any discovery item states \
the same underlying information — even if worded differently. The test is: \
would a consultant learn the same thing from this character knowledge fact as \
they would from the discovery item? If yes, the character knowledge fact is a \
duplicate and must be removed.

This is NOT about topical overlap. Facts can cover the same topic without being \
duplicates. The question is whether the specific information is the same.

CHARACTER KNOWLEDGE FACTS:
{char_facts}

DISCOVERY ITEMS:
{discovery_items}

Respond ONLY with valid JSON:
{{
  "duplicates": [
    {{
      "ck_id": "CK-01",
      "ck_content": "...",
      "matching_di_id": "DI-03",
      "reason": "Both state that production workloads run on the acceptance workspace."
    }}
  ]
}}

If there are no duplicates, return: {{"duplicates": []}}
"""


def run_phase3(
    scenario_name: str,
    source_phase: str = "phase1_extraction",
    model: str = "claude-sonnet-4-6",
    persona_name: str = "Danny",
    persona_role: str = "manager of the data platform team",
    persona_maturity: str = "LOW",
) -> dict:
    """
    Classify extracted facts into character knowledge, discovery items, or drop.

    Classification is persona-specific: facts are filtered through the lens of
    what this particular persona (role + maturity) would plausibly know.

    Args:
        scenario_name: Identifier for this scenario run.
        source_phase: Which phase output to use as input. Defaults to phase1.
                      Use "phase2_anonymized" if anonymization was applied.
        persona_name: Name of the persona (used for output filename).
        persona_role: Role description (used in classification prompt).
        persona_maturity: LOW, MEDIUM, MEDIUM_HIGH, or HIGH — persona's technical knowledge level.

    Returns:
        Classification dict with character_knowledge, discovery_items, dropped.
    """
    source = load_phase_output(scenario_name, source_phase)

    # Extract the facts and parameters from whichever source phase
    if "extracted_facts" in source:
        facts = source["extracted_facts"]
        params = source["scenario_parameters"]
    else:
        raise ValueError(
            f"Source phase output missing 'extracted_facts'. Keys: {list(source.keys())}"
        )

    print(f"  Persona: {persona_name} ({persona_role}, maturity: {persona_maturity})")

    prompt = CLASSIFICATION_PROMPT.format(
        persona_name=persona_name,
        persona_role=persona_role,
        persona_maturity=persona_maturity,
        scenario_params=json.dumps(params, indent=2),
        facts_json=json.dumps(facts, indent=2, ensure_ascii=False),
    )

    raw_response = llm_call(
        prompt,
        system=CLASSIFICATION_SYSTEM_PROMPT,
        model=model,
        max_tokens=16384,
        temperature=0.2,
    )

    cleaned = raw_response.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1]
    if cleaned.endswith("```"):
        cleaned = cleaned.rsplit("```", 1)[0]
    cleaned = cleaned.strip()

    try:
        classification = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}") + 1
        if start >= 0 and end > start:
            classification = json.loads(cleaned[start:end])
        else:
            raise ValueError("Could not parse classification response as JSON.")

    # Attach scenario parameters for downstream use.
    # Inject persona_maturity — it's a pipeline parameter, not derivable from notes.
    classification["scenario_parameters"] = params
    classification["scenario_parameters"]["persona_maturity"] = persona_maturity

    n_char = len(classification.get("character_knowledge", []))
    n_disc = len(classification.get("discovery_items", []))
    n_drop = len(classification.get("dropped", []))
    print(f"  First pass — Character knowledge: {n_char}, Discovery items: {n_disc}, Dropped: {n_drop}")

    # Refinement pass: Opus reviews discovery items against strict aha-test criteria
    print(f"\n  Running Opus refinement pass on {n_disc} discovery items...")
    classification = _refine_discovery_items(classification, params)

    n_char_final = len(classification.get("character_knowledge", []))
    n_disc_final = len(classification.get("discovery_items", []))
    n_moved = n_disc - n_disc_final
    print(f"  After refinement — Character knowledge: {n_char_final}, Discovery items: {n_disc_final} ({n_moved} moved)")

    # Rewrite discovery items into plain client language
    print(f"\n  Rewriting {n_disc_final} discovery items into plain client language...")
    classification = _rewrite_discovery_items(classification, params)

    # Dedup: remove CK facts that state the same information as a discovery item
    print(f"\n  Checking for CK/DI duplicates (Sonnet)...")
    classification = _dedup_character_knowledge(classification)
    n_char_dedup = len(classification.get("character_knowledge", []))
    if n_char_dedup < n_char_final:
        print(f"  After dedup — Character knowledge: {n_char_dedup} "
              f"({n_char_final - n_char_dedup} removed)")

    # Load scenario-level taxonomy (shared across all personas, generated once before
    # Phase 3 runs). Falls back to per-persona generation if the file is missing.
    taxonomy = _load_scenario_taxonomy(scenario_name)
    if taxonomy:
        classification["topic_taxonomy"] = taxonomy
        print(f"\n  Loaded scenario taxonomy ({len(taxonomy)} entries). Retagging all facts...")
        classification = _retag_facts(classification, taxonomy)
    else:
        print(f"\n  No scenario_taxonomy.json found. Generating per-persona taxonomy (fallback)...")
        taxonomy = _generate_taxonomy(classification, params)
        classification["topic_taxonomy"] = taxonomy
        print(f"  Generated {len(taxonomy)} taxonomy entries. Retagging all facts...")
        classification = _retag_facts(classification, taxonomy)

    output_path = save_phase_output(scenario_name, "phase3_classified", classification, persona_name=persona_name)
    print(f"\nPhase 3 complete. Output saved to: {output_path}")
    print(f"\n  Review the classification, then proceed to Phase 3.5.")
    return classification


def _refine_discovery_items(classification: dict, params: dict) -> dict:
    """
    Opus second pass: applies strict aha-test to proposed discovery items and
    moves any that don't pass back to character knowledge.
    """
    discovery_items = classification.get("discovery_items", [])
    char_knowledge = classification.get("character_knowledge", [])

    if not discovery_items:
        return classification

    # Build a brief summary of existing character knowledge for context
    char_summary = "\n".join(
        f"- [{item.get('narrative_section', '')}] {item['content']}"
        for item in char_knowledge[:30]  # cap to avoid token bloat
    )

    prompt = REFINEMENT_PROMPT.format(
        scenario_params=json.dumps(params, indent=2),
        discovery_items=json.dumps(discovery_items, indent=2, ensure_ascii=False),
        char_summary=char_summary,
    )

    raw_response = llm_call(
        prompt,
        system=REFINEMENT_SYSTEM_PROMPT,
        model=_OPUS_MODEL,
        max_tokens=8192,
        temperature=0.1,
    )

    cleaned = raw_response.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1]
    if cleaned.endswith("```"):
        cleaned = cleaned.rsplit("```", 1)[0]
    cleaned = cleaned.strip()

    try:
        refinement = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}") + 1
        if start >= 0 and end > start:
            refinement = json.loads(cleaned[start:end])
        else:
            print("  WARNING: Could not parse refinement response. Keeping original classification.")
            return classification

    kept = refinement.get("keep_as_discovery", [])
    moved = refinement.get("move_to_character", [])

    # Merge moved items into character knowledge (strip move_reason, keep content/topic/section)
    for item in moved:
        char_knowledge.append({
            "id": item["id"],
            "content": item["content"],
            "narrative_section": item.get("narrative_section", "current_platform_state"),
            "topic": item.get("topic", ""),
        })

    classification["discovery_items"] = kept
    classification["character_knowledge"] = char_knowledge
    return classification


def _rewrite_discovery_items(classification: dict, params: dict) -> dict:
    """
    Sonnet pass: rewrites each discovery item's content into plain client language.
    Removes recommendation language and jargon above the persona's maturity level.
    Preserves id, original_fact_id, topic, and aha_rationale unchanged.
    """
    discovery_items = classification.get("discovery_items", [])
    if not discovery_items:
        return classification

    # Use persona_maturity (injected by run_phase3) with fallback for old scenarios
    persona_maturity = params.get("persona_maturity", params.get("maturity_level", "MEDIUM"))

    prompt = REWRITE_PROMPT.format(
        persona_maturity=persona_maturity,
        items_json=json.dumps(discovery_items, indent=2, ensure_ascii=False),
    )

    raw_response = llm_call(
        prompt,
        system=REWRITE_SYSTEM_PROMPT,
        model=_SONNET_MODEL,
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
        rewritten = json.loads(cleaned)
        if isinstance(rewritten, list) and len(rewritten) == len(discovery_items):
            classification["discovery_items"] = rewritten
            mismatches = [
                item for item in rewritten
                if item.get("maturity_mismatch") is True
            ]
            if mismatches:
                print(f"  Maturity audit flagged {len(mismatches)} item(s) for human review:")
                for item in mismatches:
                    print(f"    [{item['id']}] {item['maturity_note']}")
            else:
                print(f"  Maturity audit: all items appropriate for {persona_maturity} persona maturity.")
        else:
            print("  WARNING: Rewrite response had unexpected structure. Keeping originals.")
    except json.JSONDecodeError:
        print("  WARNING: Could not parse rewrite response. Keeping original item content.")

    return classification


def _dedup_character_knowledge(classification: dict) -> dict:
    """
    Sonnet: removes character knowledge facts that state the same information
    as a discovery item. Discovery items take priority — gating is architectural.

    Returns updated classification with duplicates removed from character_knowledge.
    """
    char_knowledge = classification.get("character_knowledge", [])
    discovery_items = classification.get("discovery_items", [])

    if not char_knowledge or not discovery_items:
        return classification

    prompt = DEDUP_PROMPT.format(
        char_facts=json.dumps(char_knowledge, indent=2, ensure_ascii=False),
        discovery_items=json.dumps(discovery_items, indent=2, ensure_ascii=False),
    )

    raw_response = llm_call(
        prompt,
        system=DEDUP_SYSTEM_PROMPT,
        model=_SONNET_MODEL,
        max_tokens=4096,
        temperature=0.0,
    )

    cleaned = raw_response.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1]
    if cleaned.endswith("```"):
        cleaned = cleaned.rsplit("```", 1)[0]
    cleaned = cleaned.strip()

    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        print("  WARNING: Could not parse dedup response. Skipping dedup.")
        return classification

    duplicates = result.get("duplicates", [])
    if not duplicates:
        print(f"  No CK/DI duplicates found.")
        return classification

    duplicate_ck_ids = {d["ck_id"] for d in duplicates}
    for d in duplicates:
        print(f"    Removing [{d['ck_id']}]: matches [{d['matching_di_id']}] — {d['reason']}")

    classification["character_knowledge"] = [
        f for f in char_knowledge if f.get("id") not in duplicate_ck_ids
    ]
    return classification


def run_scenario_taxonomy(
    scenario_name: str,
    source_phase: str = "phase1_extraction",
) -> dict:
    """
    Generate and save a scenario-level topic taxonomy from the full Phase 1/2 extraction.

    Runs once per scenario, before any per-persona Phase 3. All personas share this
    taxonomy so topic codes are consistent across the combined scenario file.

    If scenario_taxonomy.json already exists in the workspace, it is loaded and
    returned without re-generating.

    Args:
        scenario_name: Identifier for this scenario run.
        source_phase: Which phase output to read facts from. Defaults to phase1_extraction.
                      Use "phase2_anonymized" if anonymization was applied.

    Returns:
        Taxonomy dict mapping topic codes to display names.
    """
    ws = ensure_workspace(scenario_name)
    taxonomy_path = ws / "scenario_taxonomy.json"

    if taxonomy_path.exists():
        print(f"  Scenario taxonomy already exists. Loading from: {taxonomy_path}")
        return json.loads(taxonomy_path.read_text())

    source = load_phase_output(scenario_name, source_phase)
    facts = source["extracted_facts"]
    params = source["scenario_parameters"]

    print(f"  Generating taxonomy from {len(facts)} extracted facts...")
    taxonomy = _generate_taxonomy_from_facts(facts, params)

    taxonomy_path.write_text(json.dumps(taxonomy, indent=2, ensure_ascii=False))
    print(f"  Generated {len(taxonomy)} taxonomy entries. Saved to: {taxonomy_path}")
    return taxonomy


def _load_scenario_taxonomy(scenario_name: str) -> dict:
    """
    Load scenario_taxonomy.json from the workspace.
    Returns empty dict if the file does not exist.
    """
    taxonomy_path = WORKSPACE / scenario_name / "scenario_taxonomy.json"
    if taxonomy_path.exists():
        return json.loads(taxonomy_path.read_text())
    return {}


def _generate_taxonomy_from_facts(facts: list, params: dict) -> dict:
    """
    Sonnet: generates a hierarchical topic taxonomy from raw extracted facts.
    Used by run_scenario_taxonomy() for the scenario-level shared taxonomy.
    All facts are labelled [FACT] since they haven't been classified yet.
    """
    all_facts_lines = [f"[FACT] {item['content']}" for item in facts]

    prompt = TAXONOMY_PROMPT.format(
        all_facts="\n".join(all_facts_lines),
        scenario_params=json.dumps(params, indent=2),
    )

    raw_response = llm_call(
        prompt,
        system=TAXONOMY_SYSTEM_PROMPT,
        model=_SONNET_MODEL,
        max_tokens=2048,
        temperature=0.2,
    )

    cleaned = raw_response.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1]
    if cleaned.endswith("```"):
        cleaned = cleaned.rsplit("```", 1)[0]
    cleaned = cleaned.strip()

    try:
        taxonomy = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}") + 1
        if start >= 0 and end > start:
            taxonomy = json.loads(cleaned[start:end])
        else:
            print("  WARNING: Could not parse taxonomy response. Returning empty taxonomy.")
            return {}

    return taxonomy


def _generate_taxonomy(classification: dict, params: dict) -> dict:
    """
    Sonnet: generates a hierarchical topic taxonomy from all classified facts.
    Returns a flat dict mapping topic codes to display names, ordered parent-first.
    """
    char_knowledge = classification.get("character_knowledge", [])
    discovery_items = classification.get("discovery_items", [])

    all_facts_lines = (
        [f"[CK] {item['content']}" for item in char_knowledge]
        + [f"[DI] {item['content']}" for item in discovery_items]
    )

    prompt = TAXONOMY_PROMPT.format(
        all_facts="\n".join(all_facts_lines),
        scenario_params=json.dumps(params, indent=2),
    )

    raw_response = llm_call(
        prompt,
        system=TAXONOMY_SYSTEM_PROMPT,
        model=_SONNET_MODEL,
        max_tokens=2048,
        temperature=0.2,
    )

    cleaned = raw_response.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1]
    if cleaned.endswith("```"):
        cleaned = cleaned.rsplit("```", 1)[0]
    cleaned = cleaned.strip()

    try:
        taxonomy = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}") + 1
        if start >= 0 and end > start:
            taxonomy = json.loads(cleaned[start:end])
        else:
            print("  WARNING: Could not parse taxonomy response. Returning empty taxonomy.")
            return {}

    return taxonomy


def _retag_facts(classification: dict, taxonomy: dict) -> dict:
    """
    Sonnet: retags all character knowledge and discovery items using the new taxonomy.
    Maps each fact's topic field to the most specific matching subtopic code.
    """
    if not taxonomy:
        return classification

    char_knowledge = classification.get("character_knowledge", [])
    discovery_items = classification.get("discovery_items", [])

    taxonomy_lines = "\n".join(f"{code}: {name}" for code, name in taxonomy.items())

    prompt = RETAG_PROMPT.format(
        taxonomy=taxonomy_lines,
        char_facts=json.dumps(char_knowledge, indent=2, ensure_ascii=False),
        discovery_items=json.dumps(discovery_items, indent=2, ensure_ascii=False),
    )

    raw_response = llm_call(
        prompt,
        system=RETAG_SYSTEM_PROMPT,
        model=_SONNET_MODEL,
        max_tokens=16384,
        temperature=0.0,
    )

    cleaned = raw_response.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1]
    if cleaned.endswith("```"):
        cleaned = cleaned.rsplit("```", 1)[0]
    cleaned = cleaned.strip()

    try:
        retagged = json.loads(cleaned)
        if "character_knowledge" in retagged:
            classification["character_knowledge"] = retagged["character_knowledge"]
        if "discovery_items" in retagged:
            classification["discovery_items"] = retagged["discovery_items"]
    except json.JSONDecodeError:
        print("  WARNING: Could not parse retag response. Keeping original tags.")

    return classification
