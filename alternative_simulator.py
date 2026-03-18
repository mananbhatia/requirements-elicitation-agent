"""
Alternative simulator node — node 2 of the evaluation pipeline.

For each turn where is_well_formed is false OR information_elicited is false:
  Stage A — Generate a better question using a restricted transcript
             (everything BEFORE the original question, no client response to it).
  Stage B — Simulate the client's response by invoking the conversation graph
             with the alternative question in place of the original.

Returns simulated_alternatives — list of dicts:
  {
    "turn_index": int,
    "original_question": str,
    "original_response": str,
    "alternative_question": str,
    "simulated_response": str
  }
"""

from pathlib import Path
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, AIMessage

from evaluation_state import EvaluationState

_MISTAKE_TYPES = (
    Path(__file__).parent / "docs" / "evaluation" / "mistake_types.md"
).read_text()

_ALT_PROMPT = """\
You are helping a consultant improve their requirements interview technique.

## Conversation so far (before the problematic question)

{prior_transcript}

## The consultant's original question

"{original_question}"

## Why it was ineffective

{mistake_summary}

## The 14 mistake types

{mistake_types}

## Your task

Write one improved version of this question that avoids the identified mistake(s).
The improved question must be grounded only in what has been discussed so far in the
conversation above — do not introduce information that wasn't already on the table.

The alternative question MUST address the same specific topic as the original question. \
Identify what the consultant was trying to learn — the underlying information need — and \
generate a better way to ask for that same information. Do not redirect to a different topic, \
do not ask about something the consultant wasn't asking about, and do not use the turn as an \
opportunity to ask a "better" question about something else entirely. If the original asked \
about data location, the alternative asks about data location. If the original asked about \
clusters, the alternative asks about clusters.

The alternative question you generate must itself be free of the 14 mistake types listed \
above. In particular, it must not commit the same mistake type it is correcting. For example, \
if the original question was flagged for bundling multiple requirements into one question, the \
alternative must ask only one thing. If the original was flagged for being vague, the \
alternative must be specific. Review your generated question against the 14 mistake types \
before returning it.

Return only the question text. No explanation, no preamble, no quotation marks.
"""


def _format_prior_transcript(messages: list) -> str:
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
    return "\n".join(lines) if lines else "(no prior conversation)"


def _find_message_index(messages: list, turn_index: int) -> int:
    """Return the index in messages of the consultant's turn at turn_index.
    Skips the hidden opening prompt."""
    consultant_count = 0
    for i, m in enumerate(messages):
        is_human = isinstance(m, HumanMessage) or (
            isinstance(m, dict) and m.get("type") == "human"
        )
        if not is_human:
            continue
        content = m.content if hasattr(m, "content") else m.get("content", "")
        if content.startswith("[Start of interview"):
            continue
        consultant_count += 1
        if consultant_count == turn_index:
            return i
    return -1


def build_alternative_simulator(conversation_graph):
    """
    Returns the alternative_simulator node as a closure over the conversation graph.
    The conversation graph is used in Stage B to simulate client responses.
    """
    llm = ChatAnthropic(model="claude-sonnet-4-6", temperature=0.3)

    def alternative_simulator(state: EvaluationState) -> dict:
        annotations = state.get("turn_annotations", [])
        messages = state["transcript"]
        results = []

        for ann in annotations:
            needs_alternative = (
                not ann.get("is_well_formed", True)
                or not ann.get("information_elicited", True)
            )
            if not needs_alternative:
                continue

            turn_index = ann["turn_index"]
            original_question = ann.get("question", "")
            mistakes = ann.get("mistakes", [])

            print(f"[SIM] Turn {turn_index}: {original_question!r}")

            # Locate where this turn sits in the full message list.
            msg_idx = _find_message_index(messages, turn_index)
            if msg_idx == -1:
                print(f"[SIM]   Could not locate turn {turn_index} in transcript, skipping.")
                continue

            # Extract the client's actual response to the original question (next message).
            original_response = ""
            if msg_idx + 1 < len(messages):
                next_msg = messages[msg_idx + 1]
                if hasattr(next_msg, "content"):
                    original_response = next_msg.content
                elif isinstance(next_msg, dict):
                    original_response = next_msg.get("content", "")
            print(f"[SIM]   Original response: {original_response!r}")

            # Stage A: build prior transcript — everything BEFORE this question.
            # The generator must not see the client's response to the original question.
            prior_messages = messages[:msg_idx]
            prior_transcript = _format_prior_transcript(prior_messages)

            mistake_summary = "\n".join(
                f"- [{m['mistake_type']}] {m['explanation']}" for m in mistakes
            ) or "No specific mistake type — question was generally ineffective."

            alt_prompt = _ALT_PROMPT.format(
                prior_transcript=prior_transcript,
                original_question=original_question,
                mistake_summary=mistake_summary,
                mistake_types=_MISTAKE_TYPES,
            )

            try:
                alt_response = llm.invoke([HumanMessage(content=alt_prompt)])
                alternative_question = alt_response.content.strip()
                print(f"[SIM]   Alternative: {alternative_question!r}")
            except Exception as e:
                print(f"[SIM]   Failed to generate alternative for turn {turn_index}: {e}")
                continue

            # Stage B: simulate the client's response to the alternative question.
            # Use prior_messages + the alternative question. Empty revealed_items —
            # we're not tracking item-level unlocking in simulation.
            sim_messages = list(prior_messages) + [HumanMessage(content=alternative_question)]
            try:
                sim_state = conversation_graph.invoke({
                    "messages": sim_messages,
                    "revealed_items": [],
                })
                simulated_response = sim_state["messages"][-1].content
                print(f"[SIM]   Simulated response: {simulated_response!r}")
            except Exception as e:
                print(f"[SIM]   Failed to simulate response for turn {turn_index}: {e}")
                continue

            results.append({
                "turn_index": turn_index,
                "original_question": original_question,
                "original_response": original_response,
                "alternative_question": alternative_question,
                "simulated_response": simulated_response,
            })

        return {"simulated_alternatives": results}

    return alternative_simulator
