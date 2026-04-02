# Synthetic Client Training System

An AI-powered interview simulator that lets consultants practice requirements elicitation against a realistic synthetic client, then evaluates their performance turn-by-turn and generates structured coaching feedback.

Built as a master's thesis project at [Revodata](https://revodata.nl) (Databricks consulting, Amsterdam). The system addresses a real business problem: requirements gathering, not technical implementation, is the primary bottleneck in consulting engagements, yet there is no structured way to train for it.

---

## The Problem

Consultants enter client meetings with deep technical expertise but zero knowledge of the client's organization, maturity level, or political dynamics. They have to discover all of this through conversation. Current training is informal shadowing: unscalable, inconsistent, and with no feedback mechanism.

The core challenge is that clients often can't articulate what they need. A consultant who asks "what are your access control requirements?" gets a blank stare. A consultant who asks "when a new analyst joins, what happens? Who sets up their access, and how long does it take?" gets a story that reveals three governance gaps.

This system lets consultants practice that skill in a controlled environment with immediate, structured feedback.

## What the System Does

**1. Simulated Interview.** The consultant conducts a multi-turn conversation with a synthetic client (an AI persona grounded in real, anonymized engagement materials). The client behaves like a real stakeholder: answers what's asked, doesn't volunteer information, and requires progressively specific questions to reveal deeper knowledge.

**2. Knowledge Gating.** This is the architectural core of the system. The client LLM physically cannot reveal facts it hasn't been shown. Scenario knowledge is split into surface items (shared when asked) and tacit items (require specific probing). A separate retrieval LLM evaluates each question and decides which facts it earns. The client model never sees ungated information. This is structural exclusion, not prompt-based suppression.

**3. Turn-Level Evaluation.** After the interview, each consultant question is classified against 14 validated mistake types (adapted from [Shen et al., 2025](https://doi.org/10.1109/RE59067.2024.00028)). The evaluator sees only the question and prior context, never the client's response, to prevent outcome bias.

**4. Counterfactual Alternatives.** For every flawed question, the system generates an improved version, runs it through the same conversation graph, and shows what the client *would have* said. This answers both "what should I have asked?" and "why would it have been better?"

**5. Coaching Report.** A structured feedback report with three sections: *Continue* (effective techniques), *Stop* (behavior patterns causing problems), and *Start* (gaps grounded in missed topic areas). Includes topic coverage analysis showing which areas of the client's situation were explored vs. missed.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                  CONVERSATION GRAPH                  │
│                  (LangGraph, 2 nodes)                │
│                                                      │
│   Human message ──► Retrieval Node ──► Client Node   │
│                     (GPT-OSS-120B)    (Claude Sonnet) │
│                          │                 │          │
│                    Decides which      Responds using  │
│                    facts are earned   only revealed   │
│                                       facts           │
└─────────────────────────────────────────────────────┘
                          │
                    Interview ends
                          ▼
┌─────────────────────────────────────────────────────┐
│                 EVALUATION GRAPH                     │
│                 (LangGraph, 3 nodes)                 │
│                                                      │
│   Turn Evaluator ──► Alternative ──► Report          │
│   (14 mistake        Simulator       Generator       │
│    types)            (generate →     (Continue /     │
│                       verify →        Stop / Start)  │
│                       simulate)                      │
└─────────────────────────────────────────────────────┘
```

Two separate LangGraph state machines with distinct lifecycles. The conversation graph is lightweight and runs per-turn. The evaluation graph is a one-shot batch job that processes the full transcript after the interview ends.

### Key Design Decisions

- **Structural gating over behavioral prompting.** The client LLM cannot leak what it cannot see. Prompt-based suppression rules are unreliable when the model has access to the information.
- **Separate retrieval LLM.** Question-to-knowledge matching is a semantic task, not keyword matching. "How is user access managed?" and "who controls who can log in?" need to match the same knowledge item.
- **Evaluator outcome isolation.** The client's response is hidden from the evaluator so question quality is judged independently of what it happened to unlock.
- **Counterfactual simulation reuses the conversation graph.** The alternative question runs through the identical retrieval + client pipeline, ensuring a fair apples-to-apples comparison.
- **Statistics computed in Python, not by LLMs.** Turn counts, mistake frequencies, and topic coverage are pre-computed and passed to the report LLM as hard facts. This removes a known failure mode where LLMs miscount from long annotation lists.

## Tech Stack

| Layer | Technology | Role |
|---|---|---|
| Orchestration | **LangGraph** | State machines for conversation and evaluation pipelines |
| Client Simulation | **Claude Sonnet 4.6** (temp 0.7) | Synthetic client persona with natural language variation |
| Knowledge Gating | **Databricks GPT-OSS-120B** (temp 0.0) | Semantic retrieval gate (any OpenAI-compatible model works) |
| Turn Evaluation | **Claude Sonnet 4.6** (temp 0.0) | Mistake classification against 14-type taxonomy |
| Alternative Generation | **Claude Sonnet 4.6** (temp 0.3) | Improved question generation with retry loop |
| Report Generation | **Databricks GPT-OSS-120B** (temp 0.3) | Structured coaching feedback synthesis (swappable) |
| UI | **Streamlit** | Conversation interface, evaluation display, session logging |
| Deployment | **Databricks Apps** | Production deployment at Revodata (runs anywhere Streamlit runs) |
| Framework | **LangChain** | LLM abstraction, prompt management, model routing |

Temperature is set intentionally per task: high for natural client responses, zero for deterministic evaluation, low for constrained generation.

## Project Structure

```
├── main.py                    # Terminal conversation loop
├── streamlit_app.py           # Streamlit UI (briefing sidebar, progress bar, evaluation display)
├── graph.py                   # Conversation LangGraph (2 nodes)
├── eval_graph.py              # Evaluation LangGraph (3 nodes)
├── client.py                  # Retrieval node + client node (Claude Sonnet)
├── knowledge.py               # Scenario parser, retrieval LLM (GPT-OSS), gating logic
├── state.py                   # ConversationState TypedDict + custom reducers
├── evaluation_state.py        # EvaluationState TypedDict
├── evaluator_core.py          # Shared evaluation logic (14 mistake types, evaluate_turn)
├── turn_evaluator.py          # Node 1: per-turn classification
├── alternative_simulator.py   # Node 2: alternative generation + counterfactual simulation
├── report_generator.py        # Node 3: feedback report + topic coverage
├── session_logger.py          # Session JSON export
├── docs/
│   ├── behavior_rules.md      # Client behavior rules (Grice's Cooperative Principle)
│   ├── scenarios/
│   │   └── waste_management_client.md   # Primary scenario (anonymized from real engagement)
│   └── evaluation/
│       └── mistake_types.md   # 14-type mistake taxonomy
└── tests/
    ├── test_eval_comparison.py       # Evaluation accuracy: GPT-OSS vs Sonnet (13 cases)
    └── test_retrieval_comparison.py  # Retrieval gate accuracy: Haiku vs GPT-OSS (13 cases)
```

## Getting Started

### Prerequisites

- Python 3.10+
- Anthropic API key (Claude Sonnet access)
- Any OpenAI-compatible LLM endpoint for the retrieval gate, turn classification, and report generation (the production deployment uses Databricks GPT-OSS-120B, but any model behind an OpenAI-compatible API works: GPT-4o, Llama, Mistral, a local Ollama instance, etc.)

### Installation

```bash
git clone https://github.com/mananbhatia/requirements-elicitation-agent.git
cd requirements-elicitation-agent
pip install -r requirements.txt
```

### Configuration

Create a `.env` file in the project root:

```env
ANTHROPIC_API_KEY=your-anthropic-key

# Any OpenAI-compatible endpoint. Examples:
# Databricks:  https://your-workspace.cloud.databricks.com/serving-endpoints
# OpenAI:      https://api.openai.com/v1
# Ollama:      http://localhost:11434/v1
OPENAI_BASE_URL=your-endpoint-url
OPENAI_API_KEY=your-key
```

### Running

**Streamlit UI** (recommended):
```bash
streamlit run streamlit_app.py
```

**Terminal mode**:
```bash
python main.py                                            # Default scenario
python main.py docs/scenarios/waste_management_client.md  # Specific scenario
```

### Creating New Scenarios

Scenario files are self-contained Markdown documents with structured sections for client identity, personality, maturity level, and gated knowledge items. See `docs/scenarios/waste_management_client.md` for the full format. The architecture is scenario-agnostic: swapping clients requires no code changes, just a different scenario file.

## Research Context

This project is the artifact of a Design Science Research master's thesis (JADS, 2025-2026). The problem was validated through 19 semi-structured interviews with 8 practitioners. The system design is grounded in 5 meta-requirements and 8 design principles derived from practitioner evidence and academic literature.

Key academic foundations:
- **Shen et al. (2025)**: 14 mistake types for evaluating requirements elicitation interview quality
- **Lojo et al. (2025), C-LEIA**: Validated LLM-based client simulation for interview training (120 students, 85% preferred AI client over static materials)
- **Jin et al. (2025), ReqElicitGym**: Oracle User design principles for simulated clients (Groundedness, Passive Response, Context Awareness)

## License

This project was developed as part of a thesis internship. Contact the author for licensing information.
