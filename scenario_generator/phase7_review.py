"""
Phase 7 — Final Review.

Purpose:
    Quality checks that can only run on the complete assembled scenario:
    deduplication, inference path re-validation, taxonomy tag consistency,
    and a human-readable review checklist.

Design rationale:
    Each upstream phase can introduce content that wasn't there when the
    previous phase ran. Phase 3.5 generates new facts; deduplication
    or gap-fill may create inference paths that Phase 5 didn't see.
    Phase 7 is the safety net that catches cross-phase interactions.

    Four independent functions run sequentially on the assembled scenario.
    Each returns the (possibly modified) scenario text and a log of changes,
    so `run_phase7()` can chain them cleanly.

    Deduplication (Opus): judgment about best-fit placement, coherent
        rewrites of surrounding text after removal.
    Revalidation (Opus): same prompts as Phase 5 — consistency matters;
        changes to Phase 5 validation criteria automatically apply here.
    Retag (Sonnet): straightforward code-to-code mapping; no invention.
    Checklist (Python + file I/O): all counting and flagging is deterministic;
        no LLM needed.

Input:  Phase 6 assembled scenario file ({scenario_name}_{persona_name}.md).
Output: Cleaned scenario ({scenario_name}_{persona_name}_reviewed.md) +
        phase7_review_checklist.md in the workspace.
"""

import re
import json
from pathlib import Path

from .config import llm_call, save_phase_output, load_phase_output, load_markdown, save_markdown, WORKSPACE
from .phase5_validate import VALIDATION_SYSTEM_PROMPT, VALIDATION_PROMPT

_OPUS_MODEL = "claude-opus-4-6"
_SONNET_MODEL = "claude-sonnet-4-6"

_SCENARIOS_DIR = Path(__file__).resolve().parent.parent / "docs" / "scenarios"


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

DEDUP_SYSTEM_PROMPT = """\
You are editing a character knowledge narrative for a synthetic client persona. \
Your task is to identify and remove factual duplicates — specific facts that \
appear in more than one sub-section of the narrative — and rewrite the surrounding \
sentences in sections where content was removed to maintain narrative flow.

Be conservative: only remove facts that describe the same specific situation, \
event, or detail in multiple places. Do not merge sub-sections, do not reorganize \
content, do not rewrite content that is not directly adjacent to a removed fact."""

DEDUP_PROMPT = """\
Below is the Character Knowledge narrative for a synthetic client persona. \
Read it and identify facts that appear in more than one sub-section.

A fact is a duplicate if it describes the same specific situation, event, person, \
or decision — not merely the same general theme. Example of a duplicate: \
"The team has 5 people" appearing in both Organizational History and Team Dynamics. \
Example of NOT a duplicate: Organizational History mentions slow adoption and Team \
Dynamics mentions team frustration — these are related but distinct facts.

For each duplicate:
1. Decide which sub-section is the best fit for this fact (where it provides
   the most context or flows most naturally).
2. Keep it in that sub-section unchanged.
3. Remove it from the other sub-section(s).
4. Minimally rewrite the sentences immediately surrounding the removal point
   to maintain narrative flow. Do not change anything else.

NARRATIVE:
{narrative}

Return valid JSON:
{{
  "narrative": "...the full cleaned narrative markdown, exactly as it should appear...",
  "changes": [
    {{
      "fact_summary": "brief description of the duplicate fact",
      "kept_in_section": "section name",
      "removed_from_sections": ["section name"],
      "rewrite_note": "what was rewritten and why"
    }}
  ]
}}

If no duplicates are found, return the narrative unchanged with an empty changes list.
"""

RETAG_SYSTEM_PROMPT = """\
You are a taxonomy quality checker. You receive a list of invalid topic codes \
used in a scenario document and the valid taxonomy. Map each invalid code to the \
closest valid taxonomy entry."""

RETAG_PROMPT = """\
The following topic codes appear in the scenario document but are not in the \
defined taxonomy. Map each to the closest valid taxonomy code.

VALID TAXONOMY:
{taxonomy}

INVALID CODES FOUND:
{invalid_codes}

Rules:
- Map to the most specific subtopic that fits semantically (e.g. prefer \
  "iam/provisioning" over "iam").
- If the invalid code is a parent topic that exists in the taxonomy, map it \
  to itself — it may just need normalisation.
- If no subtopic fits, use the closest parent topic.
- If no topic fits at all, map to "" (empty string — the item will be untagged).

Return ONLY valid JSON — a flat dict mapping each invalid code to its replacement:
{{
  "old_code": "new_code",
  ...
}}
"""


# ---------------------------------------------------------------------------
# Section parsing helpers
# ---------------------------------------------------------------------------

def _extract_section(scenario_text: str, header: str) -> str:
    """
    Extract the content of a top-level ## section from the scenario markdown.
    Returns the content between the matching header and the next ## header,
    or an empty string if the section is not found.
    """
    pattern = rf"(?m)^## {re.escape(header)}\s*\n(.*?)(?=\n## |\Z)"
    match = re.search(pattern, scenario_text, re.DOTALL)
    return match.group(1).strip() if match else ""


def _replace_section(scenario_text: str, header: str, new_content: str) -> str:
    """
    Replace the content of a top-level ## section in the scenario markdown.
    Preserves the header line itself.
    """
    pattern = rf"(^## {re.escape(header)}\s*\n)(.*?)(?=\n## |\Z)"
    # Use a callable replacement to avoid re.sub interpreting backslashes
    # or \g<> sequences in LLM-generated content as backreferences.
    def _replacer(m: re.Match) -> str:
        return m.group(1) + new_content + "\n"
    return re.sub(pattern, _replacer, scenario_text, flags=re.DOTALL | re.MULTILINE)


def _parse_taxonomy(topics_text: str) -> dict:
    """Parse the ## Topics section into a {code: display_name} dict."""
    taxonomy = {}
    for line in topics_text.splitlines():
        line = line.strip()
        if ": " in line and not line.startswith("#"):
            code, _, name = line.partition(": ")
            taxonomy[code.strip()] = name.strip()
    return taxonomy


def _parse_discovery_items(discoveries_text: str) -> list:
    """
    Parse discovery items from the ## Discovery Items section.
    Format: - [DI-01] [topic: X] content
    Returns list of {id, topic, content} dicts.
    """
    items = []
    pattern = re.compile(r"-\s+\[([^\]]+)\]\s+(?:\[topic:\s*([^\]]*)\]\s+)?(.+)")
    for line in discoveries_text.splitlines():
        m = pattern.match(line.strip())
        if m:
            items.append({
                "id": m.group(1),
                "topic": (m.group(2) or "").strip(),
                "content": m.group(3).strip(),
            })
    return items


def _clean_json(raw: str) -> str:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1]
    if cleaned.endswith("```"):
        cleaned = cleaned.rsplit("```", 1)[0]
    return cleaned.strip()


# ---------------------------------------------------------------------------
# Function 1: Deduplicate narrative
# ---------------------------------------------------------------------------

def deduplicate_narrative(scenario_text: str) -> tuple[str, list]:
    """
    Use Opus to identify and remove factual duplicates from the Character
    Knowledge narrative. Rewrites surrounding sentences in sections where
    content was removed to maintain narrative flow.

    Args:
        scenario_text: Full assembled scenario markdown string.

    Returns:
        (modified_scenario_text, changes_log)
        changes_log is a list of dicts describing each removal.
    """
    narrative = _extract_section(scenario_text, "Character Knowledge")
    if not narrative:
        print("  WARNING: No 'Character Knowledge' section found. Skipping deduplication.")
        return scenario_text, []

    prompt = DEDUP_PROMPT.format(narrative=narrative)
    raw_response = llm_call(
        prompt,
        system=DEDUP_SYSTEM_PROMPT,
        model=_OPUS_MODEL,
        max_tokens=16384,
        temperature=0.2,
    )

    try:
        result = json.loads(_clean_json(raw_response))
    except json.JSONDecodeError:
        print("  WARNING: Could not parse deduplication response. Skipping.")
        return scenario_text, []

    cleaned_narrative = result.get("narrative", narrative)
    changes = result.get("changes", [])

    if not changes:
        print("  No duplicates found.")
        return scenario_text, []

    modified_text = _replace_section(scenario_text, "Character Knowledge", cleaned_narrative)
    print(f"  Removed {len(changes)} duplicate(s):")
    for c in changes:
        removed_from = ", ".join(c.get("removed_from_sections", []))
        print(f"    '{c.get('fact_summary', '?')}' — kept in {c.get('kept_in_section', '?')}, "
              f"removed from {removed_from}")

    return modified_text, changes


# ---------------------------------------------------------------------------
# Function 2: Revalidate inference paths
# ---------------------------------------------------------------------------

def revalidate_inference_paths(scenario_text: str) -> tuple[str, dict]:
    """
    Re-run Phase 5 inference path validation on the assembled scenario.
    Reports HIGH and MEDIUM risks. Does NOT apply fixes — flags for human review.

    Uses identical prompts to Phase 5 so validation criteria stay consistent.

    Args:
        scenario_text: Full assembled scenario markdown string.

    Returns:
        (scenario_text_unchanged, validation_results_dict)
    """
    narrative = _extract_section(scenario_text, "Character Knowledge")
    discoveries_text = _extract_section(scenario_text, "Discovery Items")
    discovery_items = _parse_discovery_items(discoveries_text)

    if not narrative:
        print("  WARNING: No 'Character Knowledge' section found. Skipping revalidation.")
        return scenario_text, {}

    if not discovery_items:
        print("  WARNING: No discovery items found. Skipping revalidation.")
        return scenario_text, {}

    prompt = VALIDATION_PROMPT.format(
        narrative=narrative,
        discovery_items=json.dumps(discovery_items, indent=2, ensure_ascii=False),
    )

    raw_response = llm_call(
        prompt,
        system=VALIDATION_SYSTEM_PROMPT,
        model=_OPUS_MODEL,
        max_tokens=8192,
        temperature=0.2,
    )

    try:
        validation = json.loads(_clean_json(raw_response))
    except json.JSONDecodeError:
        start = raw_response.find("{")
        end = raw_response.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                validation = json.loads(raw_response[start:end])
            except json.JSONDecodeError:
                print("  WARNING: Could not parse revalidation response.")
                return scenario_text, {}
        else:
            print("  WARNING: Could not parse revalidation response.")
            return scenario_text, {}

    summary = validation.get("summary", {})
    print(f"  Revalidation results — "
          f"HIGH: {summary.get('high_risk', 0)}, "
          f"MEDIUM: {summary.get('medium_risk', 0)}, "
          f"LOW: {summary.get('low_risk', 0)}, "
          f"NONE: {summary.get('no_risk', 0)}")

    high_medium = [
        r for r in validation.get("validation_results", [])
        if r.get("inference_risk") in ("HIGH", "MEDIUM")
    ]
    if high_medium:
        print(f"  {len(high_medium)} item(s) need manual review (see checklist).")
    else:
        print(f"  No inference path risks found.")

    return scenario_text, validation


# ---------------------------------------------------------------------------
# Function 3: Retag taxonomy
# ---------------------------------------------------------------------------

def retag_taxonomy(scenario_text: str) -> tuple[str, list]:
    """
    Verify that every [topic: X] tag in the scenario uses a code from the
    Topics taxonomy. Retags invalid codes to the closest valid entry via Sonnet.

    Args:
        scenario_text: Full assembled scenario markdown string.

    Returns:
        (modified_scenario_text, changes_log)
        changes_log is a list of {old_code, new_code, occurrences} dicts.
    """
    topics_text = _extract_section(scenario_text, "Topics")
    taxonomy = _parse_taxonomy(topics_text)

    if not taxonomy:
        print("  WARNING: No taxonomy found in Topics section. Skipping retag.")
        return scenario_text, []

    # Find all [topic: X] codes used in the scenario
    all_codes_used = re.findall(r"\[topic:\s*([^\]]+)\]", scenario_text)
    unique_codes = set(c.strip() for c in all_codes_used if c.strip())

    invalid_codes = [c for c in unique_codes if c not in taxonomy]
    if not invalid_codes:
        print(f"  All {len(unique_codes)} topic tag(s) are valid.")
        return scenario_text, []

    print(f"  {len(invalid_codes)} invalid tag(s) found: {', '.join(invalid_codes)}")

    # Ask Sonnet to map each invalid code to the closest valid taxonomy code
    taxonomy_lines = "\n".join(f"{code}: {name}" for code, name in taxonomy.items())
    prompt = RETAG_PROMPT.format(
        taxonomy=taxonomy_lines,
        invalid_codes="\n".join(invalid_codes),
    )

    raw_response = llm_call(
        prompt,
        system=RETAG_SYSTEM_PROMPT,
        model=_SONNET_MODEL,
        max_tokens=1024,
        temperature=0.0,
    )

    try:
        mapping = json.loads(_clean_json(raw_response))
    except json.JSONDecodeError:
        print("  WARNING: Could not parse retag mapping. Skipping.")
        return scenario_text, []

    changes = []
    modified_text = scenario_text
    for old_code, new_code in mapping.items():
        old_tag = f"[topic: {old_code}]"
        new_tag = f"[topic: {new_code}]" if new_code else ""
        count = modified_text.count(old_tag)
        if count > 0:
            modified_text = modified_text.replace(old_tag, new_tag)
            changes.append({"old_code": old_code, "new_code": new_code, "occurrences": count})
            print(f"    {old_code} → {new_code or '(untagged)'} ({count} occurrence(s))")

    return modified_text, changes


# ---------------------------------------------------------------------------
# Function 4: Generate review checklist
# ---------------------------------------------------------------------------

def generate_review_checklist(
    scenario_text: str,
    validation_results: dict,
    scenario_name: str,
    persona_name: str = None,
) -> str:
    """
    Parse the assembled scenario and produce a human-readable review checklist.
    Saves the checklist as phase7_review_checklist.md in the workspace.

    Args:
        scenario_text: Full assembled (and possibly modified) scenario markdown.
        validation_results: Output of revalidate_inference_paths().
        scenario_name: Used to load phase3 output for maturity_mismatch flags.

    Returns:
        The checklist as a markdown string.
    """
    lines = ["# Phase 7 Review Checklist\n"]

    # --- Character Knowledge ---
    narrative = _extract_section(scenario_text, "Character Knowledge")
    word_count = len(narrative.split()) if narrative else 0
    generated_count = narrative.count("[generated]") if narrative else 0

    sub_sections = re.findall(r"^#{3,4}\s+(.+)$", narrative, re.MULTILINE)
    generated_sections = []
    if narrative and "[generated]" in narrative:
        for m in re.finditer(r"(?:^#{3,4}\s+(.+)$)(.*?)(?=^#{3,4}|\Z)", narrative, re.MULTILINE | re.DOTALL):
            if "[generated]" in m.group(2):
                generated_sections.append(m.group(1).strip())

    lines.append("## Character Knowledge")
    lines.append(f"- Word count: ~{word_count}")
    lines.append(f"- Sub-sections: {len(sub_sections)} ({', '.join(sub_sections) if sub_sections else 'none detected'})")
    if generated_count > 0:
        lines.append(f"- **[generated] tags: {generated_count}** — verify these facts are plausible")
        if generated_sections:
            lines.append(f"  - Sections with generated content: {', '.join(set(generated_sections))}")
    else:
        lines.append(f"- No [generated] tags (all facts from source notes)")
    lines.append("")

    # --- Discovery Items ---
    discoveries_text = _extract_section(scenario_text, "Discovery Items")
    discovery_items = _parse_discovery_items(discoveries_text)

    topic_distribution: dict[str, int] = {}
    for item in discovery_items:
        topic = item.get("topic") or "(untagged)"
        topic_distribution[topic] = topic_distribution.get(topic, 0) + 1

    lines.append("## Discovery Items")
    lines.append(f"- Count: {len(discovery_items)}")
    if topic_distribution:
        lines.append("- Topic distribution:")
        for topic, count in sorted(topic_distribution.items(), key=lambda x: -x[1]):
            lines.append(f"  - {topic}: {count}")
    lines.append("")

    # --- Maturity mismatch flags (from Phase 3 output) ---
    mismatch_items = []
    try:
        try:
            phase3 = load_phase_output(scenario_name, "phase3_5_completeness", persona_name=persona_name)
        except (FileNotFoundError, KeyError):
            phase3 = load_phase_output(scenario_name, "phase3_classified", persona_name=persona_name)
        mismatch_items = [
            item for item in phase3.get("discovery_items", [])
            if item.get("maturity_mismatch") is True
        ]
    except Exception:
        pass

    lines.append("## Maturity Mismatch Flags")
    if mismatch_items:
        lines.append(f"- **{len(mismatch_items)} item(s) flagged as potentially inappropriate "
                     f"for the persona's maturity level:**")
        for item in mismatch_items:
            lines.append(f"  - [{item['id']}] {item['content'][:80]}...")
            if item.get("maturity_note"):
                lines.append(f"    Note: {item['maturity_note']}")
    else:
        lines.append("- No maturity mismatch flags.")
    lines.append("")

    # --- Inference path risks ---
    val_results = validation_results.get("validation_results", [])
    val_summary = validation_results.get("summary", {})

    lines.append("## Inference Path Risks")
    if val_summary:
        lines.append(f"- HIGH: {val_summary.get('high_risk', 0)}  "
                     f"MEDIUM: {val_summary.get('medium_risk', 0)}  "
                     f"LOW: {val_summary.get('low_risk', 0)}  "
                     f"NONE: {val_summary.get('no_risk', 0)}")
    else:
        lines.append("- Revalidation did not run or returned no results.")

    high_medium = [r for r in val_results if r.get("inference_risk") in ("HIGH", "MEDIUM")]
    if high_medium:
        lines.append(f"\n**{len(high_medium)} item(s) need manual attention:**")
        for r in high_medium:
            lines.append(f"- [{r['inference_risk']}] {r['discovery_item_id']}: "
                         f"{r.get('discovery_item_content', '')[:70]}...")
            if r.get("problematic_passage"):
                lines.append(f"  Problem passage: \"{r['problematic_passage'][:100]}...\"")
            if r.get("suggested_rewrite"):
                lines.append(f"  Suggested rewrite: \"{r['suggested_rewrite'][:100]}...\"")
    lines.append("")

    # --- Topics coverage ---
    topics_text = _extract_section(scenario_text, "Topics")
    taxonomy = _parse_taxonomy(topics_text)

    lines.append("## Topic Coverage")
    if taxonomy:
        # Count items per taxonomy entry
        full_scenario_tags = re.findall(r"\[topic:\s*([^\]]+)\]", scenario_text)
        tag_counts: dict[str, int] = {}
        for tag in full_scenario_tags:
            tag = tag.strip()
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

        parent_topics = [c for c in taxonomy if "/" not in c]
        for parent in parent_topics:
            parent_count = tag_counts.get(parent, 0)
            subtopics = [c for c in taxonomy if c.startswith(parent + "/")]
            sub_counts = [(s, tag_counts.get(s, 0)) for s in subtopics]
            total = parent_count + sum(c for _, c in sub_counts)
            flag = " ⚠ (empty)" if total == 0 else ""
            lines.append(f"- **{taxonomy[parent]}** ({parent}): {total} item(s){flag}")
            for sub_code, count in sub_counts:
                flag_sub = " ⚠ (empty)" if count == 0 else ""
                lines.append(f"  - {taxonomy[sub_code]} ({sub_code}): {count}{flag_sub}")
    else:
        lines.append("- No taxonomy found.")
    lines.append("")

    # --- Items needing human attention summary ---
    attention_items = []
    if high_medium:
        attention_items.append(f"{len(high_medium)} inference path risk(s) — see Inference Path Risks above")
    if mismatch_items:
        attention_items.append(f"{len(mismatch_items)} maturity mismatch flag(s) — see Maturity Mismatch Flags above")
    if generated_count > 0:
        attention_items.append(f"{generated_count} [generated] tag(s) in narrative — verify plausibility")

    lines.append("## Items Needing Human Attention")
    if attention_items:
        for item in attention_items:
            lines.append(f"- {item}")
    else:
        lines.append("- None. Scenario is ready for runtime.")
    lines.append("")

    checklist = "\n".join(lines)

    ws = WORKSPACE / scenario_name
    ws.mkdir(parents=True, exist_ok=True)
    checklist_filename = f"phase7_review_checklist_{persona_name.lower()}.md" if persona_name else "phase7_review_checklist.md"
    checklist_path = ws / checklist_filename
    checklist_path.write_text(checklist)
    print(f"  Checklist saved to: {checklist_path}")

    return checklist


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_phase7(scenario_name: str, persona_name: str = "Danny") -> str:
    """
    Run all four review functions sequentially on the Phase 6 assembled scenario.
    Saves the cleaned scenario as {scenario_name}_{persona_name}_reviewed.md.

    Args:
        scenario_name: Identifier for this scenario run.
        persona_name: Persona name used to locate the Phase 6 output file.

    Returns:
        The final reviewed scenario text.
    """
    ws = WORKSPACE / scenario_name
    assembled_filename = f"scenario_assembled_{persona_name.lower()}.md"
    source_path = ws / assembled_filename

    if not source_path.exists():
        raise FileNotFoundError(
            f"Phase 6 output not found: {source_path}\n"
            f"Run Phase 6 first: python -m scenario_generator.cli resume "
            f"--name {scenario_name} --phase 6"
        )

    scenario_text = source_path.read_text()
    print(f"  Loaded Phase 6 output: {source_path}")
    print(f"  Length: ~{len(scenario_text.split())} words\n")

    # --- Function 1: Deduplicate narrative ---
    print("  [1/4] Deduplicating character knowledge narrative (Opus)...")
    scenario_text, dedup_log = deduplicate_narrative(scenario_text)
    print()

    # --- Function 2: Revalidate inference paths ---
    print("  [2/4] Revalidating inference paths (Opus)...")
    scenario_text, validation_results = revalidate_inference_paths(scenario_text)
    print()

    # --- Function 3: Retag taxonomy ---
    print("  [3/4] Checking taxonomy tag consistency (Sonnet)...")
    scenario_text, retag_log = retag_taxonomy(scenario_text)
    print()

    # --- Function 4: Generate checklist ---
    print("  [4/4] Generating review checklist...")
    generate_review_checklist(scenario_text, validation_results, scenario_name, persona_name=persona_name)
    print()

    # --- Save reviewed scenario ---
    # Workspace: persona-scoped name alongside scenario_assembled_{persona}.md.
    reviewed_filename = f"scenario_final_reviewed_{persona_name.lower()}.md"
    reviewed_path = ws / reviewed_filename
    reviewed_path.write_text(scenario_text)

    # docs/scenarios/: overwrite the Phase 6 draft with the reviewed version.
    _SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
    runtime_dest = _SCENARIOS_DIR / f"{scenario_name}_{persona_name.lower()}.md"
    runtime_dest.write_text(scenario_text)

    checklist_filename = f"phase7_review_checklist_{persona_name.lower()}.md"
    print(f"Phase 7 complete.")
    print(f"  Reviewed scenario: {reviewed_path}")
    print(f"  Runtime scenario updated: {runtime_dest}")
    print(f"  Checklist: {ws / checklist_filename}")

    if dedup_log:
        print(f"\n  Summary of changes:")
        print(f"    Duplicates removed: {len(dedup_log)}")
    if retag_log:
        retag_changes = sum(c["occurrences"] for c in retag_log)
        print(f"    Topic tags rewritten: {retag_changes}")

    return scenario_text
