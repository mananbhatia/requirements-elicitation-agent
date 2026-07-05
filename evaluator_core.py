"""
Shared evaluation logic — prompts, transcript formatter, and LLM calls.

Used by turn_evaluator.py (per-turn evaluation) and alternative_simulator.py
(Stage C evaluation of alternative questions).

Turn flow:
  classify_turn()          — Claude Haiku 4.5; determines turn type before mistake evaluation
  evaluate_turn()          — Claude Sonnet 4.6; classifies a question against 7 mistake types
  evaluate_turn_routed()   — orchestrates classification + routing; call this from
                              turn_evaluator.py and streamlit_app.py
"""

import re
import json
import warnings
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, AIMessage

def _extract_content(response) -> str:
    """Normalize LLM response to a plain string."""
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

# Few-shot exemplars embedded in EVAL_PROMPT below: sourced from
# evals/path_a_fewshot_package.json — pool turns where both practitioners
# independently agreed. Turns from the held-out test split (phase2_split.test_turns
# in evals/evaluation_analysis_results.json) must never appear here — including
# inside exemplar prior contexts, where earlier same-session consultant turns that
# are test turns have been replaced with neutral bracketed summaries.

EVAL_PROMPT = """\
You are evaluating a consultant's question during a requirements interview with a client.

## The 7 mistake types to check against

{mistake_types}

## Worked examples (practitioner-annotated)

The following examples come from real interviews and were annotated by expert
consultants. Use them to calibrate how strictly to apply the definitions above.
Only Types 3, 6, and 7 have mistake examples — Types 1, 2, 4, and 5 are judged
from their definitions alone; the absence of an example does not make a type
less applicable.

Some consultant turns in the prior context are summarised in brackets — only the
final "Consultant's question" line of each example is the turn being judged.

### Example 1 — mistake: Fail to follow up when needed

Prior context:
Consultant: That's clear, and it's a useful signal. When you say people can still get to data across environments, is that happening mainly for the platform team, or are business users and developers also able to reach beyond the environment they should be working in?
Client: Honestly I don't know where the boundary sits in terms of who specifically can reach what. I just know there's nothing technically stopping it from happening, which is the part that concerns me.
Consultant: Understood. From the business side, who are the main user groups relying on the platform today?
Client: The biggest group is PeCo, the Performance Community. They're the primary data consumers in the business, and they're really the barometer for how the platform is perceived internally. And then there's the broader Oracle migration question, because we've got around 500 OBIEE users who currently rely on that environment for reporting, and they'll all need to land somewhere new eventually. That's a number I think about quite a bit.

Consultant's question: "Got it. Shifting slightly, how confident do you feel today in the way ownership is set up around your data assets and catalog structures?"

Verdict: NOT well-formed — Fail to follow up when needed
Rationale: The client has just raised a substantive thread — roughly 500 reporting users who will all need to land somewhere new — and the consultant switches to a different topic without exploring it. The new question is also close to a non-sequitur: the client's previous answers already imply low confidence in how ownership is set up, so asking how confident they feel adds little.

### Example 2 — mistake: Ask for solutions

Prior context:
Consultant: [asks whether a specific technical setup is in place]
Client: I'd need you to explain what that means before I can give you a useful answer on that one.
Consultant: [apologises and asks about the day-to-day process when someone new needs access]
Client: Honestly I'm not close enough to the step-by-step to walk you through it accurately. What I can tell you is that anything involving how users are set up in our identity system goes through Rob Kuppens at Infracore, so there's an external dependency baked into that process already. Whether that's where the drag is coming from, I couldn't say for certain.

Consultant's question: "That external dependency helps explain why it feels sticky. If you were to solve that setup problem properly, what would you want the identity and access process to look like?"

Verdict: NOT well-formed — Ask for solutions
Rationale: A textbook case of asking for a solution: the consultant asks the client to specify what the identity and access process should look like, putting the burden of designing the fix on the client instead of proposing an approach and testing the client's reaction against their lived experience.

### Example 3 — mistake: Bundle distinct topics

Prior context:
Client: So we've got Sajith who's our Solutions Architect, he's the most technically senior person on the team and his focus is Azure. Emil works in a data steward and governance kind of role, he's also the internal champion for an AI use case we're trying to get off the ground. Luc is the one who's been doing a lot of the practical hands-on work around self-service analytics, building out Power BI models for business users. And then Levi flagged some concerns a while back about how we were logging jobs, which led to us getting proper monitoring and alerts in place. That's roughly the team, plus two contractors from a firm called Nexivo.
Consultant: Could you expand on the role of Nexivo in the platform initiative?
Client: They're embedded in the team and they're competent, they're not an outside party in the way DataFoundry was. But they're contractors, so the cost is always visible in a way that permanent headcount isn't, and I have to keep justifying that spend to leadership. It's not a comfortable position to be in when budgets are under scrutiny.

Consultant's question: "You did not mention any security concerns, but I also don't see anyone in the team immediately concerned with the security or access management domain. Can you explain if there is any friction here, or who I can reach out to for more detailed information?"

Verdict: NOT well-formed — Bundle distinct topics
Rationale: The consultant raises security out of the blue while the previous thread — the client having to justify contractor costs to leadership — is still open and not fully answered, and packs two different asks into one turn (whether there is friction, and who to reach out to). The client is forced to choose which thread to follow and the others get dropped.

### Example 4 — well-formed

Prior context:
Client: Hey, I'm Danny, I manage the data platform team here at Verdanta. We've got a platform that, honestly, I'd be the first to admit is a bit of a work in progress, and we're starting to feel the pressure of that more and more. I'm hoping to get a clearer sense of where the real gaps are and what it would take to get things into better shape.
Consultant: [greets the client and asks for clarification of the opening statement]
Client: Sure. When I say work in progress, I mean we've got a platform that's functional but we've had to make compromises along the way, and some of the basics aren't as solid as they should be. On the pressure side, the business keeps telling us they're not getting data fast enough, and at the same time I've got a small team that I think needs to slow down and get the fundamentals right before we can actually go faster. And then there's the visibility problem, which is that every month we don't deliver something the board can point to, I feel my room to manoeuvre getting a little smaller.

Consultant's question: "let's focus on the platform first if you don't mind. Can you tell me in what way you needed to compromise and it i'm also curious about those basics that aren't that solid. What does that mean"

Verdict: well-formed
Rationale: Although the turn contains more than one ask, both probe the same thread the client just opened — the compromises made and the basics that aren't solid. Multiple angles on the same topic are legitimate probing, not bundling, and starting with the fundamentals the client volunteered is the right move.

### Example 5 — well-formed

Prior context:
Client: That actually makes a lot of sense to me. And to be honest, yeah, I don't think we have anything like that in place. We've got the platform, and the team is working on it, but I don't think anyone has ever sat down and mapped out how it should all fit together in a structured way. Which probably explains why things like access control feel like we're just making it up as we go.
Consultant: Alright, we can perform an architectural review for you, which takes a couple of days of intensive interviewing, after which we will propose a detailed concept architecture. Than we can iterate on the design, make some adjustments based on your feedback. Then we can help implement, either fully, as a guide, or specific components, based on your demand.
Client: That sounds like it could be exactly what we need, especially if it gives us something concrete we can point to. The one thing I'd want to understand is what "a couple of days of intensive interviewing" actually means for the team, because they're already stretched pretty thin and I'd need to be realistic about how much time I can pull people away from their normal work.

Consultant's question: "Seeing as there is no foundation in place, I'd say gathering these requirements should have the highest priority.  Perhaps you can tell me for each of the team members, what is their core knowledge domain and how do they currently spend most of their time? If we know this, we can make somewhat a realistic estimate of how much we will need them."

Verdict: well-formed
Rationale: The question directly addresses the concern the client just raised — how much team time the review would take — and asks for facts the client knows from direct experience (each member's domain and how they spend their time), while explaining why the answer is needed.

### Example 6 — well-formed

Prior context:
Consultant: [asks how the access process is going]
Client: From what I hear from the team, it's a source of friction — people waiting longer than they should to get up and running. But I don't have a lot of detail beyond that, and I wouldn't want to point you in the wrong direction by guessing at the specifics.

Consultant's question: "Let me ask that more concretely — when a new analyst needs to get into a specific workspace and query a specific dataset, roughly how long does it take from the moment they ask to the moment they're actually productive?"

Verdict: well-formed
Rationale: The client couldn't describe the access process beyond hearing it is a source of friction, so the consultant narrows to a concrete, measurable experience — elapsed time from request to being productive. Slowness can be quantified even when the process itself is unclear, so the question stays within what the client can answer.

### Example 7 — well-formed

Prior context:
Client: So the big thing on the horizon is a massive consolidation project called Digital Core, which is basically moving all our CRMs and ERPs onto Dynamics 365 and Salesforce. That's going to land on our platform eventually and we need to be ready for it. The honest answer on why I'm talking to you is that we had an external party build the initial infrastructure and then they walked out the door, and I'm not sure we've ever fully gotten on top of what they left us with. On top of that the business is frustrated we're not delivering data fast enough, and I'm sitting here thinking we actually need to slow down and fix some fundamentals before we can go faster, which is not an easy conversation to have internally.
Consultant: [asks the client to clarify which of two aspects of faster delivery the business is unsatisfied about]
Client: Honestly it's a bit of both, but if I had to pick the one that causes the most noise it's that they want new data sources and new reporting capabilities and they want them quickly. The freshness side of things is there too but it's not where most of the complaints land.

Consultant's question: "Understood! So the primary business concern is onboarding new data sources and unlocking new reporting capabilities for their immediate needs. You mentioned a prior partner started building but walked out the door: Can you tell me more about your collaboration with this party and why and when they left you?"

Verdict: well-formed
Rationale: The consultant confirms their understanding of the previous answer, then follows up on a thread the client raised themselves — the partner who built the platform and left. Asking why and when they left is one topic explored from related angles, not a bundle of distinct domains.

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
