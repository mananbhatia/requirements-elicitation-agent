"""
LangGraph Concept: STATE
========================
State is the shared data structure that every node in the graph reads and writes.
It flows through the graph like a baton — each node receives it, does something,
and returns an update.

We use `MessagesState`, a built-in LangGraph type that already has one field:
  messages: list of LangChain messages (HumanMessage, AIMessage, SystemMessage)

The magic is in how messages are merged. Normally in Python, if two nodes both
return {"messages": [...]}, the second would overwrite the first. LangGraph uses
a "reducer" to define merge behavior. `MessagesState` uses `add_messages` as its
reducer, which APPENDS new messages to the list instead of overwriting.

You can define your own State with TypedDict if you need custom fields:

  from typing import Annotated
  from langgraph.graph import add_messages

  class MyState(TypedDict):
      messages: Annotated[list, add_messages]
      some_counter: int  # no reducer — will be overwritten each time

For now, MessagesState is all we need.
"""

from langgraph.graph import MessagesState

# Re-export for use in other modules. MessagesState has:
#   messages: Annotated[list[BaseMessage], add_messages]
ConversationState = MessagesState
