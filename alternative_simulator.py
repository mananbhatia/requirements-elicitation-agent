"""
Alternative simulator node — node 2 of the evaluation pipeline.

For each turn where is_well_formed is false OR information_elicited is false:
  Stage A — Generate a better question using a restricted transcript
             (everything BEFORE the original question, no client response to it).
  Stage B — Simulate the client's response by invoking the conversation graph
             with the alternative question in place of the original.
  Stage C — Evaluate the alternative question and compare both response pairs.
             Produces alt_is_well_formed, alt_information_elicited, and a
             one-sentence improvement_verdict.

Returns simulated_alternatives — list of dicts:
  {
    "turn_index": int,
    "original_question": str,
    "original_response": str,
    "alternative_question": str,
    "simulated_response": str,
    "alt_is_well_formed": bool,
    "alt_information_elicited": bool,
    "improvement_verdict": str
  }
"""

import re
import json
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, AIMessage

from evaluation_state import EvaluationState
from evaluator_core import MISTAKE_TYPES, format_transcript, evaluate_turn

_ALT_PROMPT = """\
You are helping a consultant improve their requirements interview technique.

## Conversation so far (before the problematic turn)

{prior_transcript}

## The consultant's original turn

"{original_question}"

## Why it was ineffective

{mistake_summary}

## The 14 mistake types

{mistake_types}

## Your task

Write one improved QUESTION that the consultant could have asked instead.
The alternative must always be a question — regardless of what the original turn was.
The improved question must be grounded only in what has been discussed so far in the
conversation above — do not introduce information that wasn't already on the table.

The alternative question MUST address the same specific topic or information need as the
original turn. Identify what the consultant was trying to learn or accomplish, and generate
a question that achieves that goal. Do not redirect to a different topic.

The alternative question you generate must itself be free of the 14 mistake types listed
above. Review your generated question against the 14 mistake types before returning it.

Return only the question text. No explanation, no preamble, no quotation marks.
"""


_VERDICT_PROMPT = """\
You are comparing an original consultant question with an improved alternative, \
and explaining what changed and what it produced.

## Original question and client's actual response

Original question: "{original_question}"
Client's actual response: {original_response}

## Alternative question and simulated client response

Alternative question: "{alternative_question}"
Simulated client response: {simulated_response}

## Your task

Write one sentence describing what specifically changed between the original and the \
alternative, and what that produced in the client's response. Be concrete about both \
the question change and the response outcome.

Examples of the right level of specificity:
- "The alternative asked specifically about who approves access requests, which prompted \
the client to name the process and flag that it's manual and slow."
- "Despite a cleaner question framing, the client still couldn't answer — this topic \
appears to be outside their knowledge."
- "The alternative removed the jargon but the client's response was equally vague, \
suggesting the problem is topic depth rather than phrasing."

Return only the verdict sentence. No explanation, no preamble, no quotation marks.
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
            turn_type = ann.get("turn_type", "question")

            if turn_type == "question":
                needs_alternative = (
                    not ann.get("is_well_formed", True)
                    or not ann.get("information_elicited", True)
                )
            elif turn_type == "solution_proposal":
                needs_alternative = not ann.get("information_elicited", True)
            elif turn_type == "unproductive_statement":
                needs_alternative = True
            else:
                # explanation, acknowledgment — no alternative needed
                needs_alternative = False

            if not needs_alternative:
                continue

            turn_index = ann["turn_index"]
            original_question = ann.get("question", "")
            mistakes = ann.get("mistakes", [])

            print(f"[SIM] Turn {turn_index} ({turn_type}): {original_question!r}")

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

            if mistakes:
                mistake_summary = "\n".join(
                    f"- [{m['mistake_type']}] {m['explanation']}" for m in mistakes
                )
            elif turn_type == "solution_proposal":
                mistake_summary = "The consultant proposed a solution but it did not surface new information from the client. Show what question they could have asked instead to learn more."
            elif turn_type == "unproductive_statement":
                mistake_summary = "The consultant made a statement that did not advance discovery instead of asking a question. Show what question they could have asked instead."
            else:
                mistake_summary = "The question was generally ineffective."

            alt_prompt = _ALT_PROMPT.format(
                prior_transcript=prior_transcript,
                original_question=original_question,
                mistake_summary=mistake_summary,
                mistake_types=MISTAKE_TYPES,
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

            # Stage C: evaluate the alternative question and generate improvement verdict.
            # Build a mini-transcript: prior context + alternative Q + simulated response.
            mini_transcript = format_transcript(prior_messages) + (
                f"\nConsultant: {alternative_question}"
                f"\nClient: {simulated_response}"
            )
            alt_annotation = evaluate_turn(alternative_question, mini_transcript, turn_index)
            alt_is_well_formed = alt_annotation.get("is_well_formed", True) if alt_annotation else True
            alt_information_elicited = alt_annotation.get("information_elicited", True) if alt_annotation else True
            print(f"[SIM]   Alt well-formed: {alt_is_well_formed} | Alt info elicited: {alt_information_elicited}")

            verdict_prompt = _VERDICT_PROMPT.format(
                original_question=original_question,
                original_response=original_response,
                alternative_question=alternative_question,
                simulated_response=simulated_response,
            )
            try:
                verdict_response = llm.invoke([HumanMessage(content=verdict_prompt)])
                improvement_verdict = verdict_response.content.strip()
                print(f"[SIM]   Verdict: {improvement_verdict!r}")
            except Exception as e:
                improvement_verdict = ""
                print(f"[SIM]   Failed to generate verdict for turn {turn_index}: {e}")

            results.append({
                "turn_index": turn_index,
                "original_question": original_question,
                "original_response": original_response,
                "alternative_question": alternative_question,
                "simulated_response": simulated_response,
                "alt_is_well_formed": alt_is_well_formed,
                "alt_information_elicited": alt_information_elicited,
                "improvement_verdict": improvement_verdict,
            })

        return {"simulated_alternatives": results}

    return alternative_simulator
