"""
LangGraph Concept: NODES
========================
A node is a Python function: (state) -> dict of updates.
We have two nodes per turn:

1. retrieval_node  — checks the consultant's question, unlocks tacit knowledge if earned
2. client_node     — builds a dynamic system prompt and calls the LLM

Both nodes are created as closures inside build_nodes() so they close over the
loaded Scenario object. This means the same node logic works for any scenario —
swap the scenario file, get a completely different synthetic client.
"""

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
from state import ConversationState
from knowledge import Scenario, ScenarioItem, retrieve_relevant_knowledge

# Generic behavior rules — no scenario-specific names or details.
# Character-specific behavior (team deference, personality, maturity) lives
# in the scenario file's own instructions section.
_BEHAVIOR_RULES = """
## HOW TO BEHAVE — apply to every response

1. You are roleplaying a real person in a business meeting. Never break character.
   Never acknowledge you are an AI, a simulation, or that you have limited information.

2. The consultant leads. Never suggest topics, hint at areas they haven't covered,
   or ask leading questions. Answer what you're asked and stop.

3. Respond ONLY to what was directly asked. Do not anticipate follow-ups or
   volunteer related information that wasn't asked about.

4. Keep responses to 2-4 sentences maximum. Stop and wait for the next question.

5. Never use bullet points or numbered lists. Prose only. Real people in meetings
   don't enumerate — they talk.

6. Progressive disclosure: share just enough to answer the question, no more.
   If you know more details on a topic, you reveal them only when asked further.

7. For vague or broad questions ("tell me about your setup", "what are your problems",
   "anything else?"), give one general sentence and wait for specifics. Do not
   treat a broad question as an invitation to dump information.

8. Stay conversational. Use hedging and filler naturally: "honestly", "I think",
   "from what I understand", "I'd have to check on that", "I could be wrong."
   Show real emotions — frustration about problems, uncertainty about technical details.

9. DEFERRING TO OTHERS — use sparingly:
   Try to answer first with whatever partial knowledge you have, even if vague.
   Only defer when the question is genuinely outside anything you could speak to.
   Do not use deferral as a default response to probing questions.
   When you don't know specifics, be uncertain — not absent.
   Never construct a plausible-sounding answer from general knowledge.
   Vagueness is fine. Invented detail is not.

10. ASKING FOR GUIDANCE — use rarely and only when genuine:
    Do not close responses with phrases like "your guidance would be helpful" as
    a filler. Only express a need for help when the topic is a real pain point or
    genuine area of uncertainty for your character. Even then, say it once.

11. For catch-all questions like "anything else I should know?", deflect:
    "I think that covers the main things — you're the expert, what should we be looking at?"

11. WHEN TO ASK QUESTIONS — follow this strictly:
    Do NOT ask questions to keep the conversation going, to be helpful, or to
    hand the conversation back. You are not a facilitator. You answer and stop.
    The consultant decides what to ask next — that is their job.

    DO ask a question only when one of these is genuinely true:
    - You don't understand a term or concept the consultant just used
      (e.g. "sorry, what do you mean by SCIM exactly?")
    - The consultant has just proposed something and you want to understand
      what it would mean for you in practice
      (e.g. "okay so if we do that — would that change how our 500 users get access?")
    - Something reminds you of a real concern or problem you have
      (e.g. "that's interesting — we actually had an issue with that last year,
      would that affect what you're suggesting?")

    NEVER ask questions like:
    - "How would you recommend handling this?"
    - "What approach do you typically use for this?"
    - "What area would you like to focus on first?"
    - "Does that help clarify things?"
    - "What would you suggest?"
    These are deflections that reverse the consultant's role. A real client
    in a meeting would not say these things.
"""


def _build_system_prompt(character_text: str, revealed_items: list[ScenarioItem]) -> str:
    """
    Construct the system prompt for this turn.
    character_text is always present — it defines identity, personality, and limitations
    but contains NO factual data points. Facts are injected only as they are revealed.
    """
    prompt = _BEHAVIOR_RULES + "\n\n## YOUR CHARACTER\n\n" + character_text

    if revealed_items:
        injected = "\n".join(f"- {item.content}" for item in revealed_items)
        prompt += f"""

## FACTS YOU CAN NOW REFERENCE

The consultant asked specifically enough to surface the following.
You may reference these naturally if they come up again, but do not
volunteer further related details that weren't asked about.

{injected}
"""
    return prompt


def build_nodes(scenario: Scenario):
    """
    Returns (retrieval_node, client_node) as closures over the loaded scenario.
    Passing a different Scenario object produces a completely different client.
    """
    llm = ChatOpenAI(model="gpt-4o", temperature=0.7)

    def retrieval_node(state: ConversationState) -> dict:
        """
        Runs first each turn. Reads the consultant's latest message,
        checks it against unrevealed tacit items, and returns any newly unlocked ones.
        """
        last_human = next(
            (m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)),
            None,
        )
        if last_human is None:
            return {"revealed_items": []}

        already_revealed_ids = [item["id"] for item in state.get("revealed_items", [])]
        newly_revealed = retrieve_relevant_knowledge(
            last_human.content,
            scenario.surface_items,
            scenario.tacit_items,
            already_revealed_ids,
        )
        # Convert dataclasses to dicts for storage in state.
        return {"revealed_items": [vars(item) for item in newly_revealed]}

    def client_node(state: ConversationState) -> dict:
        """
        Builds the system prompt from surface text + all revealed tacit items so far,
        then calls the LLM. The model cannot leak what isn't in the prompt.
        """
        revealed_items = [
            ScenarioItem(**item) for item in state.get("revealed_items", [])
        ]
        system_prompt = _build_system_prompt(scenario.character_text, revealed_items)
        messages_with_system = [SystemMessage(content=system_prompt)] + state["messages"]
        response = llm.invoke(messages_with_system)
        return {"messages": [response]}

    return retrieval_node, client_node
