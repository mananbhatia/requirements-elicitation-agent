"""
Report generator node — node 3 of the evaluation pipeline.

Receives turn_annotations (node 1) and simulated_alternatives (node 2) and
produces a structured feedback report in Continue/Stop/Start format.

One LLM call. The full report is stored in the `report` field of EvaluationState.
"""

import os
import warnings
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, HumanMessage as LCHumanMessage

from evaluation_state import EvaluationState
from evaluator_core import _get_databricks_base_url, _get_databricks_token, _extract_content

_REPORT_PROMPT = """\
You are a senior consultant giving feedback to a junior colleague after reviewing their \
requirements discovery interview. Be direct, specific, and constructive — write like a \
senior colleague, not a formal report.

## Statistics (use these exact numbers, do not recalculate)

{stats_text}

## Topic coverage (use these exact numbers, do not recalculate)

{coverage_text}

## Full conversation transcript

{transcript}

## Turn-by-turn evaluation

{annotations_text}

## Simulated alternatives for ineffective turns

{alternatives_text}

## Your task

Write a feedback report with exactly five sections: SUMMARY, COVERAGE, CONTINUE, STOP, START.

**SUMMARY**
Two or three sentences. Use the exact numbers from the Statistics section above. Report \
the turn type breakdown naturally: how many questions were asked, how many solution \
proposals were made, and how many turns were unproductive statements. Then report question \
quality: how many questions had mistakes and how many failed to elicit information. Include \
the topic coverage numbers: how many topics and subtopics were covered out of the total. \
Add a brief overall impression.

**COVERAGE**
Report what was and was not explored, using the Topic coverage data above. Group by \
top-level topic. For each top-level topic, state whether it was fully covered, partially \
covered, or not explored at all. For partially covered or not covered topics, list the \
specific missed subtopics by name. Keep this section factual — no recommendations here, \
those belong in START. If all subtopics were covered, say so in one sentence.

**CONTINUE**
Focus on effective turns — questions where both is_well_formed and information_elicited are \
true, AND solution proposals where information_elicited is true. These are the turns that \
worked. Do not list every one — look for PATTERNS. What did the effective questions have \
in common? If there were effective solution proposals, note what made them land (they \
prompted the client to react, clarify, or reveal something new). Name the patterns and \
illustrate each with one or two concrete examples, quoting the actual turn and referencing \
the turn number.

**STOP**
Three separate analyses:

First, mistake patterns in questions (is_well_formed: false, turn_type: question). Group \
by mistake type. For each mistake type that appeared more than once: name it, describe the \
pattern, give one concrete example by quoting the original question (with its turn number), \
and use the improvement verdict from the alternatives section as evidence — quote it \
directly. Prefer turns where alt_information_elicited is true as your primary example, \
since these prove the fix worked. If the alternative also failed, note that even a \
better-formed question did not help, and use the verdict to explain why. For mistake types \
that appeared only once, mention them briefly without a full example. If no mistakes \
recurred, say so clearly.

Second, well-formed questions that did not elicit information (is_well_formed: true, \
information_elicited: false, turn_type: question). For each such turn that has a simulated \
alternative, use the improvement verdict to characterise what happened. Only report a \
pattern if multiple turns share the same root cause. A single incident does not warrant a \
general recommendation.

Third, unproductive statements (turn_type: unproductive_statement). Quote each one and \
describe it as a missed opportunity. Use the simulated alternative from the alternatives \
section to show what could have been asked instead. If there were none, omit this section.

**START**
2–4 actionable recommendations derived from the actual failures above. Draw from all \
failure types: question mistake patterns, well-formed questions that got no information, \
and unproductive statements. Where missed topics are linked to question quality failures, \
connect them. Where a simulated alternative proved the fix worked \
(alt_information_elicited: true), use it as a concrete illustration of the recommendation. \
Be concrete — tell them exactly what to do differently.

Format rules:
- Use plain text with the section headers: SUMMARY, COVERAGE, CONTINUE, STOP, START
- No bullet points inside SUMMARY or CONTINUE — write in prose
- COVERAGE, STOP and START may use short paragraphs or minimal bullets where it aids clarity
- Quote actual questions from the transcript where instructed — do not paraphrase
- Reference turn numbers when citing specific examples
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
    # Subtopic codes are taxonomy entries that contain a "/"
    taxonomy_subtopics = {code for code in topic_taxonomy if "/" in code}

    # Subtopics that have at least one item in the scenario
    subtopics_in_scenario = {
        item["topic"] for item in scenario_items
        if item.get("topic") and "/" in item["topic"]
    } & taxonomy_subtopics

    # Subtopics touched by at least one revealed item
    revealed_subtopic_codes = {
        item["topic"] for item in revealed_items
        if item.get("topic") and "/" in item.get("topic", "")
    }
    subtopics_covered = subtopics_in_scenario & revealed_subtopic_codes

    # Build parent -> sorted subtopics mapping (only subtopics present in scenario)
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


def _compute_stats(annotations: list) -> str:
    # Split by turn type.
    questions = [a for a in annotations if a.get("turn_type", "question") == "question"]
    proposals = [a for a in annotations if a.get("turn_type") == "solution_proposal"]
    unproductive = [a for a in annotations if a.get("turn_type") == "unproductive_statement"]
    skipped = [a for a in annotations if a.get("turn_type") in ("explanation", "acknowledgment")]

    total = len(annotations)

    # Question-only quality stats.
    q_total = len(questions)
    q_with_mistakes = sum(1 for a in questions if a.get("is_well_formed") is False)
    q_no_mistakes = q_total - q_with_mistakes
    q_elicited = sum(1 for a in questions if a.get("information_elicited") is True)
    q_not_elicited = q_total - q_elicited
    q_well_formed_no_info = sum(
        1 for a in questions
        if a.get("is_well_formed") is True and a.get("information_elicited") is False
    )
    gold_examples = q_not_elicited  # questions that failed — primary alternatives targets

    mistake_counts: dict[str, int] = {}
    for ann in questions:
        for m in ann.get("mistakes", []):
            mt = m.get("mistake_type", "unknown")
            mistake_counts[mt] = mistake_counts.get(mt, 0) + 1

    # Solution proposal stats.
    sp_elicited = sum(1 for a in proposals if a.get("information_elicited") is True)
    sp_not_elicited = len(proposals) - sp_elicited

    lines = [
        f"Total consultant turns: {total}",
        f"  Questions: {q_total}",
        f"  Solution proposals: {len(proposals)}",
        f"  Unproductive statements: {len(unproductive)}",
        f"  Explanations / acknowledgments (skipped): {len(skipped)}",
        "",
        "Question quality (questions only):",
        f"  Well-formed (no mistakes): {q_no_mistakes}",
        f"  With mistakes: {q_with_mistakes}",
        f"  Elicited information: {q_elicited}",
        f"  Did not elicit information: {q_not_elicited}",
        f"  Well-formed but no information elicited: {q_well_formed_no_info}",
        f"  Candidate gold examples (questions that failed to elicit): {gold_examples}",
    ]
    if mistake_counts:
        freq = ", ".join(
            f"{mt}: {count}"
            for mt, count in sorted(mistake_counts.items(), key=lambda x: -x[1])
        )
        lines.append(f"  Mistake type frequencies: {freq}")
    else:
        lines.append("  Mistake type frequencies: (none)")

    if proposals:
        lines += [
            "",
            "Solution proposals:",
            f"  Proposals that elicited information: {sp_elicited}",
            f"  Proposals that did not elicit information: {sp_not_elicited}",
        ]

    if unproductive:
        lines += [
            "",
            f"Unproductive statements: {len(unproductive)} (missed opportunities to ask or propose)",
        ]

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
    stats_text = _compute_stats(annotations)
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
    report = _extract_content(response)

    print("[REPORT] Report generation complete.")
    return {"report": report, "topic_coverage": coverage}
