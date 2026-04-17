"""
Report generator node — node 3 of the evaluation pipeline.

Receives turn_annotations (node 1) and simulated_alternatives (node 2) and
produces a structured JSON report stored in EvaluationState["report"].

Report shape:
  {
    "summary": "2-3 sentence qualitative narrative of the interview.",
    "continue": [{"point": "2-3 sentence technique observation", "turns": [2, 3]}, ...],
    "stop":     [{"point": "2-3 sentence habit/pattern observation", "turns": [5]}, ...],
    "start":    [{"point": "2-3 sentence gap grounded in transcript moment", "turns": [5]}, ...]
  }

Statistics are computed in Python (_compute_stats) and passed as hard facts —
the LLM must not recalculate them.
"""

import re
import json
import warnings
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, AIMessage, HumanMessage as LCHumanMessage

from evaluation_state import EvaluationState
from evaluator_core import _extract_content

_REPORT_PROMPT = """\
You are giving structured feedback to a consultant after reviewing their requirements \
discovery interview. Write like a senior colleague who observed the session — direct, \
specific, and honest.

## Statistics (do not restate these in the summary — they are shown separately)

{stats_text}

## Full conversation transcript

{transcript}

## Turn-by-turn evaluation

{annotations_text}

## Simulated alternatives for ineffective turns

{alternatives_text}

## Your task

Write structured feedback in four sections. Each section answers a fundamentally different \
question — they must not overlap.

**SUMMARY — Qualitative narrative of the interview**
Write 2 sentences giving an honest overall impression. What did the consultant do well \
overall? Where did they lose momentum? What was the single biggest missed opportunity? \
Do NOT restate the statistics — those are shown separately in the UI. Write like a senior \
colleague giving their honest read after watching the session.

**CONTINUE — What technique should the consultant keep using?**
Identify 1-2 specific techniques the consultant used effectively across multiple turns. \
For each point: name the technique, explain why it worked, and reference what information \
it unlocked. "Asked good questions" is not a technique — what was the underlying skill? \
Did they build on the client's previous answer to go deeper? Did they rephrase concepts in \
the client's language to draw out concrete details? Each point should be 1-2 sentences. \
The evidence base is turns where is_well_formed=true. \
If only one genuine technique stands out, write one point. If none stand out, write an empty list.

**STOP — What recurring habit hurt the consultant?**
Identify 1-2 behavior patterns that caused repeated problems. For each point: name the habit \
and describe its cumulative cost to the interview. Do not describe what the simulated \
alternative produced — that detail is visible in the turn view. A single isolated mistake is \
not a pattern — look for the same underlying problem appearing more than once. Each point \
should be 1-2 sentences. Prioritise turns where the alternative produced a clearly better \
client response — these are the clearest evidence that the habit had real cost. \
If only one pattern recurred, write one point. If none recurred, write an empty list.

**START — What should the consultant explore next time?**
Identify 1-2 specific areas the consultant left unexplored. For each point: name the missed \
area, find a concrete moment where the client signalled it, and explain why the unexplored \
topic mattered to the engagement. Do not write out the question the consultant should have \
asked — name the gap and the signal. Every point must be grounded in a specific moment from \
the conversation. Each point should be 1-2 sentences.

**Non-redundancy rule:**
No point may say essentially the same thing as another point in a different section. \
If two points overlap, keep the stronger one and delete the other. Fewer strong unique \
points beat more redundant ones. If a section has no unique insight not already covered \
elsewhere, output an empty list for it.

**Format rules:**
- Each point is 1-2 sentences.
- Turn numbers go only in the "turns" array — do not repeat them in the point text.
- Do not quote or paraphrase specific turn content — the consultant has the transcript.
- Maximum 2 turn numbers per point.
- Maximum 2 points per section. Minimum 0 — an empty list is valid.
- The summary is plain prose, not a JSON array.
- Output ONLY valid JSON. No markdown formatting, no code fences, no explanation before or after.

**Example of expected output format (content is illustrative, not from this interview):**

{{"summary": "The consultant built real momentum in the first half by following the client's thread on access management, but lost it mid-interview by shifting to generic questions that didn't connect to what had just been shared. The biggest missed opportunity was compute governance — the client mentioned rising cloud costs in passing but the consultant never picked it up.", "continue": [{{"point": "The consultant consistently built on the client's previous answer before moving on, which on turn 4 drew out that business users were accessing development with production data — a detail the client wouldn't have raised unprompted.", "turns": [4, 5]}}, {{"point": "When the client used vague language like 'not clean', the consultant asked what that looked like in practice rather than accepting it, which unlocked specific operational details about the storage setup.", "turns": [7]}}], "stop": [{{"point": "The consultant repeatedly named a technology and asked if the client had it rather than asking about the underlying problem first — the client consistently couldn't engage because they didn't know the terms, so each of these turns produced deflection instead of discovery.", "turns": [6, 9]}}], "start": [{{"point": "Network isolation was never explored despite the client signalling early that data 'just comes in' — ingress architecture and whether there are any private connectivity requirements was a live gap that went untouched.", "turns": [3]}}]}}

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

        lines = [
            f"Turn {idx}: \"{question}\"",
            f"Well-formed: {'yes' if well_formed else 'no'}",
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
        return "(no simulated alternatives — all evaluated turns were well-formed)"
    parts = []
    for alt in alternatives:
        idx = alt.get("turn_index", "?")
        original = alt.get("original_question", "")
        original_response = alt.get("original_response", "")
        alternative = alt.get("alternative_question", "")
        simulated = alt.get("simulated_response", "")
        alt_well_formed = alt.get("alt_is_well_formed", True)
        verdict = alt.get("improvement_verdict", "")

        parts.append(
            f"Turn {idx}:\n"
            f"  Original question:              \"{original}\"\n"
            f"  Client's actual response:       {original_response}\n"
            f"  Alternative question:           \"{alternative}\"\n"
            f"  Alternative well-formed:        {'yes' if alt_well_formed else 'no'}\n"
            f"  Simulated client response:      {simulated}\n"
            f"  Improvement verdict:            {verdict}"
        )
    return "\n\n".join(parts)


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
        "solution_proposals_total": len(proposals),
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
    ]
    if stats.get("mistake_type_frequencies"):
        freq = ", ".join(
            f"{mt}: {count}"
            for mt, count in stats["mistake_type_frequencies"].items()
        )
        lines.append(f"  Mistake type frequencies: {freq}")
    else:
        lines.append("  Mistake type frequencies: (none)")
    if stats.get("unproductive_statements", 0) > 0:
        lines.append(f"\nUnproductive statements: {stats['unproductive_statements']} (missed opportunities)")
    return "\n".join(lines)


def report_generator(state: EvaluationState) -> dict:
    transcript = state["transcript"]
    annotations = state.get("turn_annotations", [])
    alternatives = state.get("simulated_alternatives", [])

    print("[REPORT] Generating feedback report...")

    stats = _compute_stats(annotations)
    stats_text = _format_stats_text(stats)
    transcript_text = _format_transcript(transcript)
    annotations_text = _format_annotations(annotations)
    alternatives_text = _format_alternatives(alternatives)

    prompt = _REPORT_PROMPT.format(
        stats_text=stats_text,
        transcript=transcript_text,
        annotations_text=annotations_text,
        alternatives_text=alternatives_text,
    )

    llm = ChatAnthropic(model="claude-sonnet-4-6", temperature=0.3)
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
    return {"report": report_dict, "stats": stats}
