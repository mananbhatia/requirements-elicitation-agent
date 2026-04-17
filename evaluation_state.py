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
    briefing: str             # consultant-facing engagement context — passed to evaluate_turn
    maturity: str             # raw maturity level section body — passed to evaluate_turn
    turn_annotations: list    # one dict per consultant turn (output of turn_evaluator)
    simulated_alternatives: list
    stats: dict               # pre-computed turn/quality stats dict (populated by report_generator)
    report: dict              # structured JSON report {summary, continue, stop, start}
