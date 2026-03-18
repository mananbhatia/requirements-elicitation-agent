"""
EvaluationState — shared data structure for the evaluation graph.

Completely separate from ConversationState. Receives the finished
conversation as input and accumulates evaluation outputs as it flows
through the evaluation nodes.
"""

from typing import TypedDict


class EvaluationState(TypedDict):
    transcript: list          # full message history from the interview
    revealed_items: list      # knowledge items unlocked during the conversation
    scenario_items_total: int # count of TIER 1 items in the scenario
    turn_annotations: list    # one dict per consultant turn (output of turn_evaluator)
    simulated_alternatives: list  # reserved for future use
    report: str               # final report string (built by a later node)
