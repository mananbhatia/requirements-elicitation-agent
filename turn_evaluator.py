"""
Turn evaluator node.

Iterates through the interview transcript, identifies every consultant turn,
and makes one LLM call per turn to classify mistakes against Shen et al.'s
14 mistake types.

Returns a list of turn_annotations — one dict per consultant turn:
  {
    "turn_index": int,
    "question": str,
    "mistakes": [{"mistake_type": str, "explanation": str}],
    "is_well_formed": bool,
    "information_elicited": bool
  }
"""

import re
import json
from pathlib import Path
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage as LCHumanMessage, AIMessage

from evaluation_state import EvaluationState

_MISTAKE_TYPES = (
    Path(__file__).parent / "docs" / "evaluation" / "mistake_types.md"
).read_text()

_EVAL_PROMPT = """\
You are evaluating a consultant's question during a requirements interview with a client.

## The 14 mistake types to check against

{mistake_types}

## Full conversation transcript

{transcript}

## Turn being evaluated

Turn index: {turn_index}
Consultant's question: "{question}"

## Your task

Classify this specific consultant turn against the 14 mistake types above.
Consider the full conversation context — a question that is vague in isolation
may be appropriate given what was already discussed, and vice versa.

Assess two things independently for this turn:

1. **is_well_formed** — Is the question free of the 14 mistake types? Set to true if no mistake
   types apply, false if any mistakes were found. This is about the question itself, not the outcome.

2. **information_elicited** — Did the client's response (visible in the transcript immediately
   after this turn) contain substantive new information? Set to true if the client provided facts,
   details, named problems, concrete descriptions, or any actionable content about their situation.
   Set to false if the client deferred to a colleague, asked for clarification, deflected, gave a
   non-answer, or provided nothing of substance. This is about the outcome, not the question quality.
   These two fields are independent — a well-formed question can fail to elicit information if the
   client doesn't have the answer, and a poorly-formed question can still elicit information if the
   client understood despite the issue.

Important:
- Many turns will have zero mistakes. That is fine and expected. Do not force-find problems.
- Only flag a mistake if it clearly applies to this question given the context.
- If the consultant's message is a direct explanation or clarification in response to the client asking something like "what do you mean by that?" or "can you explain?", this is not a question to evaluate. The consultant is doing the right thing by explaining when asked. Return an empty mistakes list, set is_well_formed to true, and set information_elicited to true.
- Each flagged mistake type must independently apply to the question on its own merits. Do not flag a mistake type to explain or reinforce why another mistake type applies. For example, "Ask a question that is too long or articulated" means the question is literally too long or complex — it does not apply to a question that is too short or minimal.
- The "ask a question that involves multiple kinds of requirements" mistake type applies only when multiple distinct questions are bundled into a single turn. It does NOT apply when a single question merely shifts to a new topic compared to the previous turn. A topic change is not the same as asking multiple questions at once.

Output ONLY a JSON object, no explanation, no reasoning, no other text:
{{
  "turn_index": {turn_index},
  "mistakes": [
    {{
      "mistake_type": "<exact name from the list above>",
      "explanation": "<one sentence: why this mistake applies to this specific question>"
    }}
  ],
  "is_well_formed": true or false,
  "information_elicited": true or false
}}

If there are no mistakes, return "mistakes": [].
"""


def _format_transcript(messages: list) -> str:
    lines = []
    for m in messages:
        if isinstance(m, LCHumanMessage):
            lines.append(f"Consultant: {m.content}")
        elif isinstance(m, AIMessage):
            lines.append(f"Client: {m.content}")
        elif isinstance(m, dict):
            role = m.get("type", m.get("role", "unknown"))
            content = m.get("content", "")
            label = "Consultant" if role == "human" else "Client"
            lines.append(f"{label}: {content}")
    return "\n".join(lines)


def turn_evaluator(state: EvaluationState) -> dict:
    messages = state["transcript"]
    llm = ChatAnthropic(model="claude-sonnet-4-6", temperature=0.0)

    transcript_text = _format_transcript(messages)
    annotations = []
    turn_index = 0

    for i, message in enumerate(messages):
        # Identify consultant turns (HumanMessage), skip the hidden opening prompt.
        is_human = isinstance(message, LCHumanMessage) or (
            isinstance(message, dict) and message.get("type") == "human"
        )
        if not is_human:
            continue

        content = message.content if hasattr(message, "content") else message.get("content", "")

        # Skip the hidden opening prompt injected by main.py.
        if content.startswith("[Start of interview"):
            continue

        turn_index += 1
        print(f"[EVAL] Turn {turn_index}: {content!r}")

        prompt = _EVAL_PROMPT.format(
            mistake_types=_MISTAKE_TYPES,
            transcript=transcript_text,
            turn_index=turn_index,
            question=content,
        )

        try:
            response = llm.invoke([LCHumanMessage(content=prompt)])
            raw = response.content.strip()

            # Strip markdown code fences if present.
            if "```" in raw:
                raw = re.sub(r"```[a-z]*\n?", "", raw).replace("```", "").strip()
            json_match = re.search(r"\{.*\}", raw, re.DOTALL)
            if json_match:
                raw = json_match.group(0)

            annotation = json.loads(raw)
            annotation["question"] = content

            mistakes = annotation.get("mistakes", [])
            well_formed = annotation.get("is_well_formed", True)
            info_elicited = annotation.get("information_elicited", True)
            if mistakes:
                print(f"[EVAL]   Mistakes: {[m['mistake_type'] for m in mistakes]}")
            else:
                print(f"[EVAL]   No mistakes found.")
            print(f"[EVAL]   Well-formed: {well_formed} | Information elicited: {info_elicited}")

            annotations.append(annotation)

        except (json.JSONDecodeError, AttributeError, Exception) as e:
            print(f"[EVAL]   Failed to parse response for turn {turn_index}: {e}")
            continue

    return {"turn_annotations": annotations}
