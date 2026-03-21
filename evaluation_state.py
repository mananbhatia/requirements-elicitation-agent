"""
EvaluationState — shared data structure for the evaluation graph.

Completely separate from ConversationState. Receives the finished
conversation as input and accumulates evaluation outputs as it flows
through the evaluation nodes.
"""

from typing import TypedDict


class EvaluationState(TypedDict):
    transcript: list          # full message history from the interview
    revealed_items: list      # knowledge items unlocked during the conversation (list of dicts)
    topic_taxonomy: dict      # code -> display name from Scenario.topic_taxonomy
    scenario_items: list      # all surface+tacit items as dicts — used for coverage computation
    turn_annotations: list    # one dict per consultant turn (output of turn_evaluator)
    simulated_alternatives: list  # reserved for future use
    topic_coverage: dict      # computed coverage stats (populated by report_generator)
    report: str               # final report string (built by a later node)
