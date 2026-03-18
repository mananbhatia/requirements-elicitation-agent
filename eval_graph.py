"""
Evaluation graph.

Completely separate from the conversation graph (graph.py).
Uses EvaluationState and runs after the interview ends.

Flow: START → turn_evaluator → alternative_simulator → report_generator → END
"""

from langgraph.graph import StateGraph, START, END
from evaluation_state import EvaluationState
from turn_evaluator import turn_evaluator
from alternative_simulator import build_alternative_simulator
from report_generator import report_generator


def build_eval_graph(conversation_graph):
    alternative_simulator = build_alternative_simulator(conversation_graph)

    builder = StateGraph(EvaluationState)
    builder.add_node("turn_evaluator", turn_evaluator)
    builder.add_node("alternative_simulator", alternative_simulator)
    builder.add_node("report_generator", report_generator)
    builder.add_edge(START, "turn_evaluator")
    builder.add_edge("turn_evaluator", "alternative_simulator")
    builder.add_edge("alternative_simulator", "report_generator")
    builder.add_edge("report_generator", END)
    return builder.compile()
