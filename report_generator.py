"""
Report generator node — node 3 of the evaluation pipeline.

Receives turn_annotations (node 1) and simulated_alternatives (node 2) and
produces a structured JSON report stored in EvaluationState["report"].

Report shape:
  {
    "summary": "One sentence overall impression.",
    "continue": [{"point": "...", "turns": [2, 3]}, ...],
    "stop":     [{"point": "...", "turns": [5]}, ...],
    "start":    [{"point": "...", "turns": [5]}, ...]
  }

Statistics are computed in Python (_compute_stats) and passed as hard facts —
the LLM must not recalculate them.
"""

import os
import re
import json
import warnings
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, HumanMessage as LCHumanMessage

from evaluation_state import EvaluationState
from evaluator_core import _get_databricks_base_url, _get_databricks_token, _extract_content

_REPORT_PROMPT = """\
You are giving structured feedback to a consultant after reviewing their requirements \
discovery interview. Be direct and specific — write like a senior colleague.

## Statistics (use these exact numbers — do not recalculate)

{stats_text}

## Topic coverage (use these exact numbers — do not recalculate)

{coverage_text}

## Full conversation transcript

{transcript}

## Turn-by-turn evaluation

{annotations_text}

## Simulated alternatives for ineffective turns

{alternatives_text}

## Your task

Write structured feedback in three sections. Each section answers a fundamentally different \
question — they must not overlap.

**CONTINUE — What specific skill did the consultant demonstrate?**
Identify 1-2 specific TECHNIQUES the consultant used effectively. Not "asked good questions" — \
that is obvious. What was the underlying skill? Did they build on the client's previous answer \
to go deeper? Did they rephrase technical concepts in the client's terms? Did they use a solution \
proposal to test an assumption? Name the technique, say briefly why it worked, cite 1-2 turns. \
If only one genuine technique stands out, write one point.

**STOP — What specific habit hurt the consultant?**
Identify 1-2 BEHAVIOR PATTERNS that caused problems. The evidence is in the alternatives: if the \
alternative worked better, that proves the original approach failed. Name the habit, reference 1 \
turn as the clearest example, briefly note what the alternative achieved instead. If only one \
pattern recurred, write one point.

**START — What did the consultant leave unexplored?**
Identify 1-2 GAPS — things the consultant did NOT do. Focus on:
- Subtopics from the coverage data that were never explored, naming the specific subtopics
- Areas where the consultant touched a topic superficially but deeper issues existed
This section is grounded in the coverage stats and missed subtopics provided above. Do not invent \
abstract technique recommendations.

**CRITICAL — Non-redundancy check:**
Before outputting, read all your points across Continue, Stop, and Start together. For each point, \
ask: does any other point across any section say essentially the same thing, just phrased differently \
or from the opposite angle? If yes, delete the weaker one. The total output should contain 3-6 points \
where every single point tells the consultant something the other points do not. It is better to have \
3 strong unique points than 6 where half are redundant. If a section has zero unique insights not \
already covered by another section, output an empty list for that section.

**Format rules:**
- Each point is one sentence. Maximum 25 words per point (turn references do not count).
- Turn references go in the "turns" array. Maximum 2 turn numbers per point.
- Maximum 2 points per section. Minimum 0 — an empty list is valid.
- The summary is 1-2 sentences stating turn counts, mistake counts, and subtopic coverage using \
the exact numbers from the Statistics section.
- Output ONLY valid JSON. No markdown formatting, no code fences, no explanation before or after.

**Example of expected output format (content is illustrative, not from this interview):**

{{"summary": "Across 14 turns you asked 11 questions (8 well-formed, 3 with mistakes), elicited information in 9, and covered 8 of 12 subtopics.", "continue": [{{"point": "Built follow-up chains where each question narrowed based on the client's previous answer", "turns": [4, 5]}}, {{"point": "Rephrased technical proposals in the client's business language, drawing out practical concerns", "turns": [9]}}], "stop": [{{"point": "Used technical acronyms the client couldn't understand, stalling the conversation", "turns": [7]}}, {{"point": "Made reactive comments instead of asking questions, wasting discovery opportunities", "turns": [11]}}], "start": [{{"point": "Explore the 3 missed subtopics: storage configuration, compliance readiness, and workload placement", "turns": []}}, {{"point": "Probe deeper on identity management — only surface-level facts were uncovered despite multiple items existing", "turns": []}}]}}

Now write the actual feedback for this interview:

{{"summary": "...", "continue": [...], "stop": [...], "start": [...]}}
"""


def _format_transcript(messages: list) -> str:
    lines = []
    for m in messages:
        if isinstance(m, HumanMessage):
            lines.append(f"Consultant: {m.content}")
        elif isinstance(m, AIMessage):
            lines.append(f"Client: {m.content}")
        elif isinstance(m, dict):
            role = m.get("type", m.get("role", "unknown"))
            content = m.get("content", "")
            label = "Consultant" if role == "human" else "Client"
            lines.append(f"{label}: {content}")
    return "\n".join(lines)


def _format_annotations(annotations: list) -> str:
    if not annotations:
        return "(no annotations)"
    parts = []
    for ann in annotations:
        idx = ann.get("turn_index", "?")
        question = ann.get("question", "")
        mistakes = ann.get("mistakes", [])
        well_formed = ann.get("is_well_formed", True)
        info_elicited = ann.get("information_elicited", True)

        lines = [
            f"Turn {idx}: \"{question}\"",
            f"Well-formed: {'yes' if well_formed else 'no'} | Information elicited: {'yes' if info_elicited else 'no'}",
        ]
        if mistakes:
            lines.append("Mistakes:")
            for m in mistakes:
                lines.append(f"  [{m['mistake_type']}] {m['explanation']}")
        else:
            lines.append("Mistakes: none")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _format_alternatives(alternatives: list) -> str:
    if not alternatives:
        return "(no simulated alternatives — all evaluated turns were well-formed and elicited information)"
    parts = []
    for alt in alternatives:
        idx = alt.get("turn_index", "?")
        original = alt.get("original_question", "")
        original_response = alt.get("original_response", "")
        alternative = alt.get("alternative_question", "")
        simulated = alt.get("simulated_response", "")
        alt_well_formed = alt.get("alt_is_well_formed", True)
        alt_info_elicited = alt.get("alt_information_elicited", True)
        verdict = alt.get("improvement_verdict", "")

        parts.append(
            f"Turn {idx}:\n"
            f"  Original question:              \"{original}\"\n"
            f"  Client's actual response:       {original_response}\n"
            f"  Alternative question:           \"{alternative}\"\n"
            f"  Alternative well-formed:        {'yes' if alt_well_formed else 'no'}\n"
            f"  Simulated client response:      {simulated}\n"
            f"  Alternative info elicited:      {'yes' if alt_info_elicited else 'no'}\n"
            f"  Improvement verdict:            {verdict}"
        )
    return "\n\n".join(parts)


def _compute_coverage(topic_taxonomy: dict, scenario_items: list, revealed_items: list) -> dict:
    """
    Compute topic coverage from revealed items.

    A subtopic is covered if at least one item with that subtopic code was revealed.
    A top-level topic is covered (partially or fully) if any of its subtopics were covered.
    Items with an empty topic field are excluded.

    Returns a dict suitable for both LLM formatting and Streamlit rendering.
    """
    taxonomy_subtopics = {code for code in topic_taxonomy if "/" in code}

    subtopics_in_scenario = {
        item["topic"] for item in scenario_items
        if item.get("topic") and "/" in item["topic"]
    } & taxonomy_subtopics

    revealed_subtopic_codes = {
        item["topic"] for item in revealed_items
        if item.get("topic") and "/" in item.get("topic", "")
    }
    subtopics_covered = subtopics_in_scenario & revealed_subtopic_codes

    parent_to_subtopics: dict[str, list] = {}
    for code in subtopics_in_scenario:
        parent = code.split("/")[0]
        parent_to_subtopics.setdefault(parent, [])
        if code not in parent_to_subtopics[parent]:
            parent_to_subtopics[parent].append(code)
    for parent in parent_to_subtopics:
        parent_to_subtopics[parent].sort()

    topics_in_scenario = set(parent_to_subtopics.keys())
    topics_fully_covered = {
        p for p, subs in parent_to_subtopics.items()
        if set(subs) <= subtopics_covered
    }
    topics_partially_covered = {
        p for p, subs in parent_to_subtopics.items()
        if set(subs) & subtopics_covered and p not in topics_fully_covered
    }
    topics_not_covered = topics_in_scenario - topics_fully_covered - topics_partially_covered

    return {
        "subtopics_total": len(subtopics_in_scenario),
        "subtopics_covered": len(subtopics_covered),
        "topics_total": len(topics_in_scenario),
        "topics_covered": len(topics_fully_covered | topics_partially_covered),
        "subtopics_covered_list": sorted(subtopics_covered),
        "subtopics_in_scenario_list": sorted(subtopics_in_scenario),
        "parent_to_subtopics": parent_to_subtopics,
        "topics_fully_covered": sorted(topics_fully_covered),
        "topics_partially_covered": sorted(topics_partially_covered),
        "topics_not_covered": sorted(topics_not_covered),
    }


def _format_coverage_text(coverage: dict, topic_taxonomy: dict) -> str:
    """Format coverage stats as plain text for the LLM prompt."""
    lines = [
        f"Topics in scenario: {coverage['topics_total']}",
        f"Topics with at least one subtopic covered: {coverage['topics_covered']}",
        f"Topics with no subtopics covered: {coverage['topics_total'] - coverage['topics_covered']}",
        f"Subtopics in scenario: {coverage['subtopics_total']}",
        f"Subtopics covered: {coverage['subtopics_covered']}",
        f"Subtopics missed: {coverage['subtopics_total'] - coverage['subtopics_covered']}",
        "",
        "Topic-by-topic breakdown:",
    ]
    covered_set = set(coverage["subtopics_covered_list"])
    for parent_code in sorted(coverage["parent_to_subtopics"]):
        subtopics = coverage["parent_to_subtopics"][parent_code]
        parent_display = topic_taxonomy.get(parent_code, parent_code)
        n_covered = sum(1 for s in subtopics if s in covered_set)
        if n_covered == len(subtopics):
            status = "fully covered"
        elif n_covered > 0:
            status = f"partially covered ({n_covered}/{len(subtopics)} subtopics)"
        else:
            status = "not covered"
        lines.append(f"  {parent_display} — {status}:")
        for sub_code in subtopics:
            sub_display = topic_taxonomy.get(sub_code, sub_code)
            mark = "covered" if sub_code in covered_set else "MISSED"
            lines.append(f"    - {sub_display}: {mark}")
    return "\n".join(lines)


def _compute_stats(annotations: list) -> dict:
    """
    Compute turn and quality statistics from annotations.
    Returns a dict consumed by both _format_stats_text (for the LLM) and Streamlit (for display).
    """
    questions = [a for a in annotations if a.get("turn_type", "question") == "question"]
    proposals = [a for a in annotations if a.get("turn_type") == "solution_proposal"]
    unproductive = [a for a in annotations if a.get("turn_type") == "unproductive_statement"]
    skipped = [a for a in annotations if a.get("turn_type") in ("explanation", "acknowledgment")]

    total = len(annotations)
    q_total = len(questions)
    q_with_mistakes = sum(1 for a in questions if a.get("is_well_formed") is False)
    q_no_mistakes = q_total - q_with_mistakes
    q_elicited = sum(1 for a in questions if a.get("information_elicited") is True)
    q_not_elicited = q_total - q_elicited
    q_well_formed_no_info = sum(
        1 for a in questions
        if a.get("is_well_formed") is True and a.get("information_elicited") is False
    )
    sp_elicited = sum(1 for a in proposals if a.get("information_elicited") is True)
    sp_not_elicited = len(proposals) - sp_elicited

    mistake_counts: dict[str, int] = {}
    for ann in questions:
        for m in ann.get("mistakes", []):
            mt = m.get("mistake_type", "unknown")
            mistake_counts[mt] = mistake_counts.get(mt, 0) + 1

    return {
        "total_turns": total,
        "questions_total": q_total,
        "questions_well_formed": q_no_mistakes,
        "questions_with_mistakes": q_with_mistakes,
        "questions_information_elicited": q_elicited,
        "questions_no_information_elicited": q_not_elicited,
        "questions_well_formed_no_info": q_well_formed_no_info,
        "solution_proposals_total": len(proposals),
        "solution_proposals_information_elicited": sp_elicited,
        "solution_proposals_no_information_elicited": sp_not_elicited,
        "unproductive_statements": len(unproductive),
        "explanations_acknowledgments": len(skipped),
        "mistake_type_frequencies": dict(sorted(mistake_counts.items(), key=lambda x: -x[1])),
    }


def _format_stats_text(stats: dict) -> str:
    """Format a stats dict as plain text for the LLM prompt."""
    lines = [
        f"Total consultant turns: {stats['total_turns']}",
        f"  Questions: {stats['questions_total']}",
        f"  Solution proposals: {stats['solution_proposals_total']}",
        f"  Unproductive statements: {stats['unproductive_statements']}",
        f"  Explanations / acknowledgments (skipped): {stats['explanations_acknowledgments']}",
        "",
        "Question quality:",
        f"  Well-formed (no mistakes): {stats['questions_well_formed']}",
        f"  With mistakes: {stats['questions_with_mistakes']}",
        f"  Elicited information: {stats['questions_information_elicited']}",
        f"  Did not elicit information: {stats['questions_no_information_elicited']}",
        f"  Well-formed but no information elicited: {stats['questions_well_formed_no_info']}",
    ]
    if stats.get("mistake_type_frequencies"):
        freq = ", ".join(
            f"{mt}: {count}"
            for mt, count in stats["mistake_type_frequencies"].items()
        )
        lines.append(f"  Mistake type frequencies: {freq}")
    else:
        lines.append("  Mistake type frequencies: (none)")
    if stats.get("solution_proposals_total", 0) > 0:
        lines += [
            "",
            "Solution proposals:",
            f"  Elicited information: {stats['solution_proposals_information_elicited']}",
            f"  Did not elicit information: {stats['solution_proposals_no_information_elicited']}",
        ]
    if stats.get("unproductive_statements", 0) > 0:
        lines.append(f"\nUnproductive statements: {stats['unproductive_statements']} (missed opportunities)")
    return "\n".join(lines)


def report_generator(state: EvaluationState) -> dict:
    transcript = state["transcript"]
    annotations = state.get("turn_annotations", [])
    alternatives = state.get("simulated_alternatives", [])
    topic_taxonomy = state.get("topic_taxonomy", {})
    scenario_items = state.get("scenario_items", [])
    revealed_items = state.get("revealed_items", [])

    print("[REPORT] Generating feedback report...")

    coverage = _compute_coverage(topic_taxonomy, scenario_items, revealed_items)
    stats = _compute_stats(annotations)
    stats_text = _format_stats_text(stats)
    coverage_text = _format_coverage_text(coverage, topic_taxonomy)
    transcript_text = _format_transcript(transcript)
    annotations_text = _format_annotations(annotations)
    alternatives_text = _format_alternatives(alternatives)

    prompt = _REPORT_PROMPT.format(
        stats_text=stats_text,
        coverage_text=coverage_text,
        transcript=transcript_text,
        annotations_text=annotations_text,
        alternatives_text=alternatives_text,
    )

    llm = ChatOpenAI(
        model="databricks-gpt-oss-120b",
        base_url=_get_databricks_base_url(),
        api_key=_get_databricks_token(),
        temperature=0.3,
        extra_body={"reasoning_effort": "high"},
    )
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")
        response = llm.invoke([LCHumanMessage(content=prompt)])
    raw = _extract_content(response)

    # Parse JSON — strip code fences, then locate the report JSON by its known first key.
    # Using rfind('{"summary":') avoids greedy-regex capturing reasoning-block text that
    # contains '{' before the actual JSON, which caused json.loads to fail.
    try:
        if "```" in raw:
            raw = re.sub(r"```[a-z]*\n?", "", raw).replace("```", "").strip()
        start = raw.rfind('{"summary":')
        if start != -1:
            raw = raw[start:]
        else:
            json_match = re.search(r"\{.*\}", raw, re.DOTALL)
            if json_match:
                raw = json_match.group(0)
        report_dict = json.loads(raw)
    except (json.JSONDecodeError, Exception) as e:
        print(f"[REPORT] Failed to parse JSON report: {e}. Raw output:\n{raw[:500]}")
        report_dict = {"summary": "Report generation failed — see terminal for raw output.", "continue": [], "stop": [], "start": []}

    print("[REPORT] Report generation complete.")
    return {"report": report_dict, "topic_coverage": coverage, "stats": stats}
