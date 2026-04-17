"""
Shared evaluation logic — prompts, transcript formatter, and LLM calls.

Used by turn_evaluator.py (per-turn evaluation) and alternative_simulator.py
(Stage C evaluation of alternative questions).

Turn flow:
  classify_turn()          — GPT-OSS low; determines turn type before mistake evaluation
  evaluate_turn()          — Claude Sonnet 4.6; classifies a question against 7 mistake types
  evaluate_turn_routed()   — orchestrates classification + routing; call this from
                              turn_evaluator.py and streamlit_app.py
"""

import os
import re
import json
import warnings
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, AIMessage

def _get_databricks_token() -> str:
    token = os.environ.get("DATABRICKS_TOKEN")
    if not token:
        raise EnvironmentError("DATABRICKS_TOKEN is not set. Add it to your .env file.")
    return token


def _get_databricks_base_url() -> str:
    url = os.environ.get("DATABRICKS_BASE_URL")
    if not url:
        raise EnvironmentError("DATABRICKS_BASE_URL is not set. Add it to your .env file.")
    return url


def _extract_content(response) -> str:
    """Normalize LLM response to a plain string.

    GPT-OSS-120B sometimes returns content as a list of blocks
    ([{"type": "text", "text": "..."}]) instead of a plain string.
    It also prepends chain-of-thought reasoning before the answer.
    This helper handles both cases and returns the raw text.
    """
    content = response.content
    if isinstance(content, list):
        parts = [b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(parts).strip()
    return str(content).strip()


def _parse_json_response(raw: str) -> dict:
    """Strip markdown fences and extract the first JSON object from a response string."""
    if "```" in raw:
        raw = re.sub(r"```[a-z]*\n?", "", raw).replace("```", "").strip()
    json_match = re.search(r"\{.*\}", raw, re.DOTALL)
    if json_match:
        raw = json_match.group(0)
    return json.loads(raw)


from paths import MISTAKE_TYPES_FILE
MISTAKE_TYPES = MISTAKE_TYPES_FILE.read_text()

# ---------------------------------------------------------------------------
# Turn classification (runs before mistake evaluation)
# ---------------------------------------------------------------------------

_CLASSIFY_PROMPT = """\
Classify the following consultant turn from a requirements discovery interview.

## Full conversation transcript (for context)

{transcript}

## Turn to classify

Turn index: {turn_index}
Consultant's message: "{message}"

## Turn types

(a) question — The consultant is asking the client something to learn about their situation.
    This is what the mistake evaluation is designed to check.

(b) solution_proposal — The consultant is proposing, suggesting, or testing a solution or
    approach. Examples: "we can give them minimal access to start with", "what if we set up
    SCIM to sync users?", "I'd recommend separating the workspaces." Valid consulting behaviour.

(c) explanation — The consultant is responding to a client question or request for
    clarification. The client explicitly asked "what does that mean?" or "can you explain?"
    and the consultant is answering. Only applies when the client asked first.

(d) acknowledgment — Brief transition or filler with no substantive content of its own:
    "got it", "makes sense", "okay", "sure", "okay let me ask about something else."

(e) unproductive_statement — A statement that does not advance discovery and is not a
    solution proposal or explanation. Examples: "pretty bad", "that's not good." A missed opportunity.

## Important

Classify based on the PRIMARY purpose of the turn:
- "that makes sense. how are your environments set up?" → QUESTION (preamble + question)
- "that's a problem. we could set up private endpoints." → SOLUTION_PROPOSAL
- "got it. tell me more about access." → QUESTION (preamble is filler, question is primary)

Output ONLY a JSON object, no explanation, no reasoning, no other text:
{{"turn_type": "question" | "solution_proposal" | "explanation" | "acknowledgment" | "unproductive_statement", "reasoning": "<one sentence explaining the classification>"}}
"""

# ---------------------------------------------------------------------------
# Question mistake evaluation
# ---------------------------------------------------------------------------

EVAL_PROMPT = """\
You are evaluating a consultant's question during a requirements interview with a client.

## The 7 mistake types to check against

{mistake_types}

## Engagement context

{briefing}

## Client's maturity level

The following instructions were given to the synthetic client to define how it behaves.
Use this to assess whether the consultant's language and question complexity are appropriate
for this client. It is directly relevant to Type 5: "Ask a question inappropriate to client's level".

{maturity_level}

## Conversation transcript (up to and including the question being evaluated)

{transcript}

## Turn being evaluated

Turn index: {turn_index}
Consultant's question: "{question}"

## Your task

Classify this specific consultant turn against the 7 mistake types above.
Consider the full conversation context — a question that is vague in isolation
may be appropriate given what was already discussed, and vice versa.

**is_well_formed** — Is the question free of the 7 mistake types? Set to true if no mistake
types apply, false if any mistakes were found. This is about the question itself, not the outcome.

Important:
- Many turns will have zero mistakes. That is fine and expected. Do not force-find problems.
- Only flag a mistake if it clearly applies to this question given the context.
- If this question has a problem, identify the SINGLE most fundamental mistake type — the root cause that, if fixed, would most likely resolve any other issues with the question. If multiple types seem to apply, they are usually symptoms of the same underlying problem. Pick the one that best describes WHY the question failed, not every label that could technically apply. Return exactly one mistake object or an empty list.

Output ONLY a JSON object, no explanation, no reasoning, no other text:
{{
  "turn_index": {turn_index},
  "mistakes": [
    {{
      "mistake_type": "<exact name from the list above>",
      "explanation": "<one sentence: why this mistake applies to this specific question>"
    }}
  ],
  "is_well_formed": true or false
}}

"mistakes" contains at most one item. If there are no mistakes, return "mistakes": [].
"""


def format_transcript(messages: list) -> str:
    """Convert a list of LangChain message objects (or dicts) to a plain text transcript.
    Used when the full conversation is needed — e.g. for classify_turn() and report generation.
    For evaluation of a specific turn, use format_transcript_up_to() instead to avoid
    exposing the client's response to the evaluator (outcome bias).
    """
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


def format_transcript_up_to(messages: list, turn_index: int) -> str:
    """Return transcript text up to and including the consultant's nth turn.

    The client's response to that turn is NOT included — this prevents the
    evaluator from seeing the outcome when judging question quality.
    Hidden opening prompts ([Start of interview...]) are skipped.
    """
    lines = []
    consultant_count = 0
    for m in messages:
        is_human = isinstance(m, HumanMessage) or (
            isinstance(m, dict) and m.get("type") == "human"
        )
        if is_human:
            content = m.content if hasattr(m, "content") else m.get("content", "")
            if content.startswith("[Start of interview"):
                continue
            consultant_count += 1
            lines.append(f"Consultant: {content}")
            if consultant_count == turn_index:
                break  # stop here — exclude the client's response to this turn
        else:
            if consultant_count < turn_index:
                content = m.content if hasattr(m, "content") else m.get("content", "")
                lines.append(f"Client: {content}")
    return "\n".join(lines)


def classify_turn(message: str, transcript_text: str, turn_index: int) -> dict | None:
    """
    Classify a consultant turn into one of five types before mistake evaluation.
    Uses Claude Haiku — simple routing task, low latency priority.
    Returns {"turn_type": str, "reasoning": str} or None on failure.
    """
    llm = ChatAnthropic(model="claude-haiku-4-5-20251001", temperature=0.0)
    prompt = _CLASSIFY_PROMPT.format(
        transcript=transcript_text,
        turn_index=turn_index,
        message=message,
    )
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")
            response = llm.invoke([HumanMessage(content=prompt)])
        return _parse_json_response(_extract_content(response))
    except Exception as e:
        print(f"[CLASSIFY] Failed to classify turn {turn_index}: {e}")
        return None


def evaluate_turn(question: str, transcript_text: str, turn_index: int, maturity_level: str = "", briefing: str = "") -> dict | None:
    """
    Evaluate a consultant QUESTION against the 7 mistake types.
    Only call this for turns already classified as "question".
    transcript_text should contain only the transcript up to and including this question
    — the client's response is excluded to prevent outcome bias.
    Returns the parsed annotation dict, or None on failure.
    """
    llm = ChatAnthropic(model="claude-sonnet-4-6", temperature=0.0)
    prompt = EVAL_PROMPT.format(
        mistake_types=MISTAKE_TYPES,
        briefing=briefing or "(not specified)",
        maturity_level=maturity_level or "(not specified)",
        transcript=transcript_text,
        turn_index=turn_index,
        question=question,
    )
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")
            response = llm.invoke([HumanMessage(content=prompt)])
        raw = _extract_content(response)
        return _parse_json_response(raw)
    except Exception as e:
        print(f"[EVAL_CORE] Failed to evaluate turn {turn_index}: {e}")
        return None


def evaluate_turn_routed(
    content: str,
    transcript_text: str,
    turn_index: int,
    maturity_level: str = "",
    briefing: str = "",
    truncated_transcript_text: str = "",
) -> dict | None:
    """
    Classify a consultant turn and evaluate it according to its type.

    classify_turn receives the full transcript (no outcome bias risk for type classification).
    evaluate_turn receives truncated_transcript_text (up to and including the question, hiding
    the client's response) to prevent outcome bias. Falls back to transcript_text if not provided.

    Routes:
      question             → evaluate_turn() against 7 mistake types; is_well_formed set by LLM
      solution_proposal    → is_well_formed=None
      explanation          → skipped; is_well_formed=None
      acknowledgment       → skipped; is_well_formed=None
      unproductive_statement → is_well_formed=False

    Returns an annotation dict with turn_type set, or None on classification failure.
    """
    classification = classify_turn(content, transcript_text, turn_index)
    turn_type = classification.get("turn_type", "question") if classification else "question"
    reasoning = classification.get("reasoning", "") if classification else ""

    if turn_type == "question":
        eval_transcript = truncated_transcript_text or transcript_text
        annotation = evaluate_turn(content, eval_transcript, turn_index, maturity_level=maturity_level, briefing=briefing)
        if annotation is None:
            return None
        annotation["turn_type"] = "question"
        return annotation

    if turn_type == "solution_proposal":
        return {
            "turn_index": turn_index,
            "turn_type": "solution_proposal",
            "mistakes": [],
            "is_well_formed": None,
        }

    if turn_type == "unproductive_statement":
        return {
            "turn_index": turn_index,
            "turn_type": "unproductive_statement",
            "mistakes": [{"mistake_type": "Unproductive statement", "explanation": reasoning}],
            "is_well_formed": False,
        }

    # explanation or acknowledgment — skip evaluation entirely
    return {
        "turn_index": turn_index,
        "turn_type": turn_type,
        "mistakes": [],
        "is_well_formed": None,
    }
