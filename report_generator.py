"""
Report generator node — node 3 of the evaluation pipeline.

Receives turn_annotations (node 1) and simulated_alternatives (node 2) and
produces a structured feedback report in Continue/Stop/Start format.

One LLM call. The full report is stored in the `report` field of EvaluationState.
"""

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, AIMessage, HumanMessage as LCHumanMessage

from evaluation_state import EvaluationState

_REPORT_PROMPT = """\
You are a senior consultant giving feedback to a junior colleague after reviewing their \
requirements discovery interview. Be direct, specific, and constructive — write like a \
senior colleague, not a formal report.

## Statistics (use these exact numbers, do not recalculate)

{stats_text}

## Full conversation transcript

{transcript}

## Turn-by-turn evaluation

{annotations_text}

## Simulated alternatives for ineffective turns

{alternatives_text}

## Your task

Write a feedback report with exactly four sections: SUMMARY, CONTINUE, STOP, START.

**SUMMARY**
Two or three sentences. Use the exact numbers from the Statistics section above to state \
how many questions the consultant asked, how many had mistakes (is_well_formed: false), \
how many failed to elicit information, and how many were well-formed but got no useful \
response. Add a brief overall impression.

**CONTINUE**
Focus on turns where both is_well_formed and information_elicited are true — these are the \
turns that worked on both dimensions. Do not just list every such turn — look for PATTERNS. \
What did those questions have in common? Were they specific, single-topic, adapted to the \
client's level? Name the pattern, then illustrate it with one or two concrete examples, \
quoting the actual question and referencing the turn number.

**STOP**
Two separate analyses:

First, mistake patterns (is_well_formed: false). Group by mistake type across all turns. \
For each mistake type that appeared more than once: name it, describe the pattern, give one \
concrete example by quoting the original question (with its turn number), explain why it \
was a mistake, and — if a simulated alternative exists for that turn — quote the alternative \
question and the simulated client response as evidence of what a better question unlocks. \
If the simulated alternative got the same deflection or vague response as the original, \
note that this topic may need a fundamentally different approach. For mistake types that \
appeared only once, mention them briefly without a full example. If no mistakes recurred, \
say so clearly.

Second, well-formed questions that did not elicit information. Use the "Turns well-formed \
but no information elicited" count from the statistics. Identify which turns these were \
from the annotations (is_well_formed: true, information_elicited: false). For each such \
turn, look at the client's actual response in the transcript to determine why no information \
was elicited — did the client defer to a colleague, say they didn't know, ask for \
clarification, or give a vague non-answer? Only report a pattern if multiple turns share \
the same root cause. A single incident does not warrant a general recommendation. If a \
pattern exists, describe what it reveals about where the consultant directed their questions \
relative to what this client could actually answer, and what that implies for how to \
structure the next conversation.

**START**
2–4 actionable recommendations derived from the actual failures above. Draw from both \
failure types: mistake patterns (where phrasing needs to change) and well-formed questions \
that got no information (where the consultant may need to read client knowledge level cues \
better and pivot earlier). Be concrete — tell them exactly what to do differently.

Format rules:
- Use plain text with the section headers: SUMMARY, CONTINUE, STOP, START
- No bullet points inside SUMMARY or CONTINUE — write in prose
- STOP and START may use short paragraphs or minimal bullets where it aids clarity
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

        parts.append(
            f"Turn {idx}:\n"
            f"  Original question:          \"{original}\"\n"
            f"  Client's actual response:   {original_response}\n"
            f"  Alternative question:       \"{alternative}\"\n"
            f"  Simulated client response:  {simulated}"
        )
    return "\n\n".join(parts)


def _compute_stats(annotations: list) -> str:
    total = len(annotations)
    with_mistakes = sum(1 for a in annotations if not a.get("is_well_formed", True))
    no_mistakes = total - with_mistakes
    elicited = sum(1 for a in annotations if a.get("information_elicited", True))
    not_elicited = total - elicited
    well_formed_no_info = sum(
        1 for a in annotations
        if a.get("is_well_formed", True) and not a.get("information_elicited", True)
    )

    mistake_counts: dict[str, int] = {}
    for ann in annotations:
        for m in ann.get("mistakes", []):
            mt = m.get("mistake_type", "unknown")
            mistake_counts[mt] = mistake_counts.get(mt, 0) + 1

    lines = [
        f"Total consultant turns evaluated: {total}",
        f"Turns with no mistakes (is_well_formed: true): {no_mistakes}",
        f"Turns with mistakes (is_well_formed: false): {with_mistakes}",
        f"Turns that elicited information (information_elicited: true): {elicited}",
        f"Turns that did not elicit information (information_elicited: false): {not_elicited}",
        f"Turns well-formed but no information elicited: {well_formed_no_info}",
    ]
    if mistake_counts:
        freq = ", ".join(
            f"{mt}: {count}"
            for mt, count in sorted(mistake_counts.items(), key=lambda x: -x[1])
        )
        lines.append(f"Mistake type frequencies: {freq}")
    else:
        lines.append("Mistake type frequencies: (none)")
    return "\n".join(lines)


def report_generator(state: EvaluationState) -> dict:
    transcript = state["transcript"]
    annotations = state.get("turn_annotations", [])
    alternatives = state.get("simulated_alternatives", [])

    print("[REPORT] Generating feedback report...")

    stats_text = _compute_stats(annotations)
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
    response = llm.invoke([LCHumanMessage(content=prompt)])
    report = response.content.strip()

    print("[REPORT] Report generation complete.")
    return {"report": report}
