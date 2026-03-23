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
import warnings
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, AIMessage

from evaluation_state import EvaluationState
from evaluator_core import MISTAKE_TYPES, format_transcript, evaluate_turn, _extract_content

_MAX_ALT_ATTEMPTS = 3

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
{retry_note}
## Your task

Write one question that a skilled interviewer would ask at this point in the conversation.
First, read the transcript carefully and identify what has already been established — do
not ask about anything that has already been answered or covered. Then decide what is most
valuable to explore next: prefer finding a better way to ask about the same topic as the
original turn, and only shift to a different topic if the original topic is genuinely
beyond what this client can answer at all.

Do not just fix the wording of the original question. Ask what a skilled interviewer
would genuinely want to know at this point, given what has and has not been covered.
Do not introduce topics that have not been hinted at or mentioned in the conversation.

The question must be free of all 14 mistake types listed above. Review your question
against each type before returning it.

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
        maturity = state.get("maturity", "")
        briefing = state.get("briefing", "")
        results = []

        for ann in annotations:
            turn_type = ann.get("turn_type", "question")

            if turn_type == "question":
                needs_alternative = not ann.get("is_well_formed", True)
            elif turn_type == "unproductive_statement":
                needs_alternative = True
            else:
                # solution_proposal, explanation, acknowledgment — no alternative needed
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
            elif turn_type == "unproductive_statement":
                mistake_summary = "The consultant made a statement that did not advance discovery instead of asking a question. Show what question they could have asked instead."
            else:
                mistake_summary = "The question was generally ineffective."

            mini_transcript_for_eval = format_transcript(prior_messages)

            # Stage A (with retry): generate alternative, evaluate well-formedness before
            # committing to the expensive Stage B simulation. Retry up to _MAX_ALT_ATTEMPTS
            # times, passing the previous attempt's mistake back to the generator each time.
            alternative_question = ""
            retry_note = ""
            for attempt in range(1, _MAX_ALT_ATTEMPTS + 1):
                alt_prompt = _ALT_PROMPT.format(
                    prior_transcript=prior_transcript,
                    original_question=original_question,
                    mistake_summary=mistake_summary,
                    mistake_types=MISTAKE_TYPES,
                    retry_note=retry_note,
                )
                try:
                    with warnings.catch_warnings():
                        warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")
                        alt_response = llm.invoke([HumanMessage(content=alt_prompt)])
                    alternative_question = _extract_content(alt_response).strip()
                    print(f"[SIM]   Alternative (attempt {attempt}): {alternative_question!r}")
                except Exception as e:
                    print(f"[SIM]   Failed to generate alternative for turn {turn_index} (attempt {attempt}): {e}")
                    break

                if not alternative_question:
                    break

                # Quick well-formedness check before running Stage B.
                pre_eval_transcript = mini_transcript_for_eval + f"\nConsultant: {alternative_question}"
                pre_annotation = evaluate_turn(
                    alternative_question, pre_eval_transcript, turn_index,
                    maturity_level=maturity,
                    briefing=briefing,
                )
                if pre_annotation and pre_annotation.get("is_well_formed", True):
                    print(f"[SIM]   Alternative passed pre-check on attempt {attempt}.")
                    break
                elif pre_annotation and attempt < _MAX_ALT_ATTEMPTS:
                    pre_mistakes = pre_annotation.get("mistakes", [])
                    mistake_desc = (
                        f"[{pre_mistakes[0]['mistake_type']}] {pre_mistakes[0]['explanation']}"
                        if pre_mistakes else "not well-formed"
                    )
                    retry_note = (
                        f"\n## Previous attempt (rejected)\n"
                        f"Your previous attempt was: \"{alternative_question}\"\n"
                        f"It was rejected because: {mistake_desc}\n"
                        f"Generate a different question that avoids this problem.\n"
                    )
                    print(f"[SIM]   Attempt {attempt} failed pre-check ({mistake_desc}), retrying.")

            if not alternative_question:
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

            # Stage C: reuse the pre-check annotation for well-formedness (already evaluated).
            # Compute gate-based information_elicited from the simulation state.
            alt_information_elicited = len(sim_state.get("revealed_items", [])) > 0
            alt_is_well_formed = pre_annotation.get("is_well_formed", True) if pre_annotation else True
            print(f"[SIM]   Alt well-formed: {alt_is_well_formed} | Alt info elicited: {alt_information_elicited}")

            verdict_prompt = _VERDICT_PROMPT.format(
                original_question=original_question,
                original_response=original_response,
                alternative_question=alternative_question,
                simulated_response=simulated_response,
            )
            try:
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")
                    verdict_response = llm.invoke([HumanMessage(content=verdict_prompt)])
                improvement_verdict = _extract_content(verdict_response).strip()
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
