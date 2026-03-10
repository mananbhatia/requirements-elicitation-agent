"""
LangGraph Concept: NODES
========================
A node is a Python function: (state) -> dict of updates.
We now have two nodes that run in sequence each turn:

1. retrieval_node  — checks what the consultant asked and unlocks tacit knowledge
2. client_node     — builds a dynamic system prompt and calls the LLM

Splitting into two nodes keeps concerns separate: one node does classification,
the other does generation. Each is independently testable.
"""

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
from state import ConversationState
from knowledge import SURFACE_KNOWLEDGE, retrieve_relevant_knowledge

_llm = None  # initialized lazily after .env is loaded

def _get_llm():
    global _llm
    if _llm is None:
        _llm = ChatOpenAI(model="gpt-4o", temperature=0.7)
    return _llm

_BEHAVIOR_RULES = """
## HOW TO BEHAVE

1. Never reveal you are an AI or a simulation. Stay in character at all times.
2. Never guide the conversation. The consultant leads. Do not suggest topics.
3. Respond ONLY to what was asked. Do not anticipate follow-up questions.
4. Keep responses to 2-4 sentences maximum. Stop and wait.
5. Never use bullet points or lists. Prose only.
6. Progressive disclosure: even when you know something, share just enough and stop.
7. Deflect vague questions: for broad questions, give one vague sentence and wait.
8. Stay conversational: use natural filler ("honestly", "I think", "from what I understand"),
   show frustration about problems, uncertainty about details.
9. When outside your knowledge, defer: "Sajith would know more about that"
   or "that's what we need your help figuring out."
10. For catch-all questions like "anything else?", deflect: "I think that covers
    the main things — you're the expert, what should we be looking at?"
"""


def _build_system_prompt(revealed_items: list[dict]) -> str:
    """
    Construct the system prompt dynamically each turn.
    Surface knowledge is always present. Tacit knowledge is added
    only for items that have been unlocked so far.
    """
    prompt = _BEHAVIOR_RULES + "\n\n## WHO YOU ARE\n\n" + SURFACE_KNOWLEDGE

    if revealed_items:
        injected = "\n".join(f"- {item['content']}" for item in revealed_items)
        prompt += f"""

## ADDITIONAL CONTEXT (revealed because the consultant asked specifically)

The following details have come up in the conversation. You now know the consultant
is aware of these — you can reference them if relevant, but still don't volunteer
more than what was asked:

{injected}
"""
    return prompt


def retrieval_node(state: ConversationState) -> dict:
    """
    Runs before the client LLM each turn.
    Reads the consultant's latest message and checks whether it unlocks
    any tacit knowledge. Returns new items to add to revealed_items.
    """
    # Find the last human message (consultant's question).
    last_human = next(
        (m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)),
        None,
    )
    if last_human is None:
        return {"revealed_items": []}

    already_revealed_ids = [item["id"] for item in state.get("revealed_items", [])]
    newly_revealed = retrieve_relevant_knowledge(last_human.content, already_revealed_ids)

    return {"revealed_items": newly_revealed}


def client_node(state: ConversationState) -> dict:
    """
    Builds the system prompt from surface knowledge + all revealed tacit knowledge
    so far, then calls the LLM. The LLM cannot leak what isn't in the prompt.
    """
    revealed_items = state.get("revealed_items", [])
    system_prompt = _build_system_prompt(revealed_items)

    messages_with_system = [SystemMessage(content=system_prompt)] + state["messages"]
    response = _get_llm().invoke(messages_with_system)
    return {"messages": [response]}
