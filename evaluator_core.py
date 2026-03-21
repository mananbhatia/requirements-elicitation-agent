"""
Shared evaluation logic — prompts, transcript formatter, and LLM calls.

Used by turn_evaluator.py (per-turn evaluation) and alternative_simulator.py
(Stage C evaluation of alternative questions).

Turn flow:
  classify_turn()          — GPT-OSS medium; determines turn type before mistake evaluation
  evaluate_turn()          — GPT-OSS high; classifies a question against 14 mistake types
  check_information_elicited() — GPT-OSS medium; used for non-question turns
  evaluate_turn_routed()   — orchestrates classification + routing; call this from
                              turn_evaluator.py and streamlit_app.py
"""

import os
import re
import json
import warnings
from pathlib import Path
from langchain_openai import ChatOpenAI
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


MISTAKE_TYPES = (
    Path(__file__).parent / "docs" / "evaluation" / "mistake_types.md"
).read_text()

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
    solution proposal or explanation. Examples: "it means you are screwed", "pretty bad.
    anyone can hack you", "that's not good." A missed opportunity.

## Important

Classify based on the PRIMARY purpose of the turn:
- "that makes sense. how are your environments set up?" → QUESTION (preamble + question)
- "that's a problem. we could set up private endpoints." → SOLUTION_PROPOSAL
- "got it. tell me more about access." → QUESTION (preamble is filler, question is primary)

Output ONLY a JSON object, no explanation, no reasoning, no other text:
{{"turn_type": "question" | "solution_proposal" | "explanation" | "acknowledgment" | "unproductive_statement", "reasoning": "<one sentence explaining the classification>"}}
"""

_INFO_ELICITED_PROMPT = """\
You are reviewing a transcript of a consultant interviewing a client.

## Full conversation transcript

{transcript}

## Your task

Look at the client's response immediately after the consultant's turn {turn_index}.

Did the client's response contain substantive new information about their situation?
Set information_elicited to true if the client provided facts, details, named problems,
concrete descriptions, or any actionable content about their specific situation.
Set it to false if the client gave a non-answer, a vague reaction, deflected, or
provided nothing of substance.

Output ONLY a JSON object, no explanation, no other text:
{{"information_elicited": true or false}}
"""

# ---------------------------------------------------------------------------
# Question mistake evaluation
# ---------------------------------------------------------------------------

EVAL_PROMPT = """\
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


def format_transcript(messages: list) -> str:
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


def classify_turn(message: str, transcript_text: str, turn_index: int) -> dict | None:
    """
    Classify a consultant turn into one of five types before mistake evaluation.
    Uses GPT-OSS-120B medium reasoning.
    Returns {"turn_type": str, "reasoning": str} or None on failure.
    """
    llm = ChatOpenAI(
        model="databricks-gpt-oss-120b",
        base_url=_get_databricks_base_url(),
        api_key=_get_databricks_token(),
        temperature=0.0,
        extra_body={"reasoning_effort": "medium"},
    )
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


def check_information_elicited(transcript_text: str, turn_index: int) -> bool:
    """
    Check whether the client's response to a given turn contained substantive information.
    Used for non-question turns (solution_proposal) where evaluate_turn is not called.
    Uses GPT-OSS-120B medium reasoning.
    """
    llm = ChatOpenAI(
        model="databricks-gpt-oss-120b",
        base_url=_get_databricks_base_url(),
        api_key=_get_databricks_token(),
        temperature=0.0,
        extra_body={"reasoning_effort": "medium"},
    )
    prompt = _INFO_ELICITED_PROMPT.format(
        transcript=transcript_text,
        turn_index=turn_index,
    )
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")
            response = llm.invoke([HumanMessage(content=prompt)])
        parsed = _parse_json_response(_extract_content(response))
        return bool(parsed.get("information_elicited", False))
    except Exception as e:
        print(f"[CLASSIFY] Failed to check info elicited for turn {turn_index}: {e}")
        return False


def evaluate_turn(question: str, transcript_text: str, turn_index: int) -> dict | None:
    """
    Evaluate a consultant QUESTION against the 14 mistake types.
    Only call this for turns already classified as "question".
    Returns the parsed annotation dict, or None on failure.
    """
    llm = ChatOpenAI(
        model="databricks-gpt-oss-120b",
        base_url=_get_databricks_base_url(),
        api_key=_get_databricks_token(),
        temperature=0.0,
        extra_body={"reasoning_effort": "high"},
    )
    prompt = EVAL_PROMPT.format(
        mistake_types=MISTAKE_TYPES,
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


def evaluate_turn_routed(content: str, transcript_text: str, turn_index: int) -> dict | None:
    """
    Classify a consultant turn and evaluate it according to its type.

    Routes:
      question             → evaluate_turn() against 14 mistake types
      solution_proposal    → check_information_elicited() only; is_well_formed=None
      explanation          → skip evaluation; is_well_formed=None, info_elicited=None
      acknowledgment       → skip evaluation; is_well_formed=None, info_elicited=None
      unproductive_statement → is_well_formed=False, info_elicited=False; mistake flagged

    Returns an annotation dict with turn_type set, or None on classification failure.
    Called by both turn_evaluator.py (LangGraph node) and streamlit_app.py (per-turn loop).
    """
    classification = classify_turn(content, transcript_text, turn_index)
    turn_type = classification.get("turn_type", "question") if classification else "question"
    reasoning = classification.get("reasoning", "") if classification else ""

    if turn_type == "question":
        annotation = evaluate_turn(content, transcript_text, turn_index)
        if annotation is None:
            return None
        annotation["turn_type"] = "question"
        return annotation

    if turn_type == "solution_proposal":
        info_elicited = check_information_elicited(transcript_text, turn_index)
        return {
            "turn_index": turn_index,
            "turn_type": "solution_proposal",
            "mistakes": [],
            "is_well_formed": None,
            "information_elicited": info_elicited,
        }

    if turn_type == "unproductive_statement":
        return {
            "turn_index": turn_index,
            "turn_type": "unproductive_statement",
            "mistakes": [{"mistake_type": "Unproductive statement", "explanation": reasoning}],
            "is_well_formed": False,
            "information_elicited": False,
        }

    # explanation or acknowledgment — skip evaluation entirely
    return {
        "turn_index": turn_index,
        "turn_type": turn_type,
        "mistakes": [],
        "is_well_formed": None,
        "information_elicited": None,
    }
