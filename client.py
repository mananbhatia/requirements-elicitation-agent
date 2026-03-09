"""
LangGraph Concept: NODES
========================
A node is just a Python function with this signature:
  def my_node(state: State) -> dict

It receives the current state, does something (call an LLM, run a tool,
transform data), and returns a dict of state updates. LangGraph merges
those updates back into the state using the reducers defined on State.

The node doesn't mutate state directly — it returns what changed.
That's what makes LangGraph graphs composable and inspectable.
"""

from pathlib import Path
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage
from state import ConversationState

_PRINCIPLES_PATH = Path(__file__).parent / "docs" / "research" / "client_design_principles.md"
_SCENARIO_PATH = Path(__file__).parent / "docs" / "scenarios" / "waste_management_client.md"

_PRINCIPLES = _PRINCIPLES_PATH.read_text()
_SCENARIO = _SCENARIO_PATH.read_text()

_SYSTEM_PROMPT = f"""You are a synthetic client in a Databricks consulting interview simulation.
A consultant is interviewing you to understand your organisation's requirements and challenges.
Your character, knowledge, and communication style are defined entirely by the scenario below.

## HOW TO BEHAVE — follow these rules strictly

{_PRINCIPLES}

## WHO YOU ARE AND WHAT YOU KNOW — your scenario

{_SCENARIO}

---

IMPORTANT REMINDERS:
- Maximum 2-4 sentences per response. Stop there and wait.
- Never use bullet points or numbered lists. Prose only.
- Never reveal information that wasn't directly asked about.
- Never guide the consultant toward topics. Let them lead.
- If a question is vague or broad, give one high-level sentence and wait for specifics.
"""

_llm = ChatOpenAI(model="gpt-4o", temperature=0.7)


def synthetic_client_node(state: ConversationState) -> dict:
    """
    This is the only node in our graph.

    It receives the full conversation history (state["messages"]),
    prepends the system prompt, calls the LLM, and returns the new
    AI message. LangGraph will append it to state["messages"] via
    the add_messages reducer.
    """
    messages_with_system = [SystemMessage(content=_SYSTEM_PROMPT)] + state["messages"]
    response = _llm.invoke(messages_with_system)
    return {"messages": [response]}
