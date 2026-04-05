# Codebase Index — agent_v2

## File Structure

```
agent_v2/
├── main.py                  # Terminal entry point — conversation loop + evaluation runner
├── streamlit_app.py         # Web UI entry point — Streamlit chat + evaluation display
├── graph.py                 # Builds conversation LangGraph (2 nodes, linear)
├── eval_graph.py            # Builds evaluation LangGraph (3 nodes, linear)
├── client.py                # Conversation nodes: retrieval_node + client_node (closures over scenario)
├── knowledge.py             # Scenario markdown parser + retrieval gate LLM call
├── state.py                 # ConversationState TypedDict with custom revealed_items reducer
├── evaluation_state.py      # EvaluationState TypedDict for the evaluation pipeline
├── evaluator_core.py        # Shared evaluation logic: prompt, transcript formatter, evaluate_turn(), classify_turn()
├── turn_evaluator.py        # Eval node 1: classifies each consultant turn + evaluates against 14 mistake types
├── alternative_simulator.py # Eval node 2: generates better questions + simulates client response + verdict
├── report_generator.py      # Eval node 3: synthesizes Continue/Stop/Start feedback report
├── session_logger.py        # Saves complete session data as timestamped JSON to logs/
├── test_databricks.py       # Standalone test for Databricks GPT-OSS-120B endpoint connectivity
├── test_eval_comparison.py  # Side-by-side evaluation accuracy: GPT-OSS vs Sonnet (13 cases + 5 classify cases)
├── test_retrieval_comparison.py # Retrieval gate accuracy: Haiku vs GPT-OSS low/medium (13 cases)
├── docs/
│   ├── behavior_rules.md                # 9 generic client behavior rules (loaded at runtime by client.py)
│   ├── scenarios/
│   │   └── waste_management_client.md   # example scenario — the only scenario currently
│   └── mistake_types.md                 # 14 mistake types from Shen et al. (loaded by evaluator_core.py)
├── logs/                    # Session JSON files, one per completed interview+evaluation (gitignored)
├── requirements.txt         # Python dependencies
└── .env                     # API keys: ANTHROPIC_API_KEY, DATABRICKS_TOKEN, DATABRICKS_BASE_URL (gitignored)
```

## Module Connection Map

### Conversation pipeline (runs per turn during interview)

```
main.py / streamlit_app.py
    │
    ├─ knowledge.py ─── load_scenario(path) ──→ Scenario dataclass
    │
    ├─ graph.py ─── build_graph(scenario)
    │     │
    │     └─ client.py ─── build_nodes(scenario)
    │           │
    │           ├─ retrieval_node ─── calls knowledge.py::retrieve_relevant_knowledge()
    │           │                         └─ GPT-OSS-120B (temp 0.0, reasoning_effort=medium)
    │           │
    │           └─ client_node ─── reads docs/behavior_rules.md (loaded at module level)
    │                              builds dynamic system prompt
    │                              └─ Claude Sonnet 4.6 (temp 0.7)
    │
    └─ state.py ─── ConversationState flows through both nodes
```

### Evaluation pipeline (runs once after interview ends)

```
main.py / streamlit_app.py
    │
    └─ eval_graph.py ─── build_eval_graph(conversation_graph)
          │
          ├─ turn_evaluator.py ─── turn_evaluator(state)
          │     └─ evaluator_core.py
          │           ├─ classify_turn() ─── GPT-OSS-120B (temp 0.0, reasoning_effort=low)
          │           │     └─ routes each turn: question / unproductive_statement /
          │           │        solution_proposal / explanation / acknowledgment
          │           └─ evaluate_turn() ─── Claude Sonnet 4.6 (temp 0.0)
          │                 ├─ reads docs/mistake_types.md (loaded at module level)
          │                 └─ one call per question/unproductive_statement turn
          │
          ├─ alternative_simulator.py ─── build_alternative_simulator(conversation_graph)
          │     ├─ Stage A: generates alternative question (retry loop up to 3x)
          │     │     ├─ Claude Sonnet 4.6 (temp 0.3) — generate
          │     │     └─ evaluator_core.py::evaluate_turn() — pre-check (Sonnet, temp 0.0)
          │     ├─ Stage B: simulates response via conversation_graph.invoke()
          │     │     └─ reuses retrieval_node (GPT-OSS) + client_node (Sonnet)
          │     └─ Stage C: reuses Stage A pre-check annotation (no extra LLM call)
          │           └─ verdict prompt — Claude Sonnet 4.6 (temp 0.3)
          │
          └─ report_generator.py ─── report_generator(state)
                ├─ _compute_stats() + _compute_coverage() — Python, no LLM
                └─ GPT-OSS-120B (temp 0.3, reasoning_effort=high) — one call for full report

    ↓ after evaluation completes

    session_logger.py ─── save_session() ──→ logs/session_YYYY-MM-DD_HH-MM-SS.json
```

### Import dependency graph

```
state.py                    ← imported by client.py
knowledge.py                ← imported by client.py, main.py, streamlit_app.py
client.py                   ← imported by graph.py
graph.py                    ← imported by main.py, streamlit_app.py, eval_graph.py (indirectly via conversation_graph param)
evaluation_state.py         ← imported by turn_evaluator.py, alternative_simulator.py, report_generator.py
evaluator_core.py           ← imported by turn_evaluator.py, alternative_simulator.py
turn_evaluator.py           ← imported by eval_graph.py
alternative_simulator.py    ← imported by eval_graph.py
report_generator.py         ← imported by eval_graph.py
eval_graph.py               ← imported by main.py, streamlit_app.py
session_logger.py           ← imported by main.py, streamlit_app.py
```

No circular dependencies. `evaluator_core.py` is the shared module that prevents duplication between turn_evaluator and alternative_simulator.

### Files loaded at runtime (not imported, read from disk)

```
client.py          reads  docs/behavior_rules.md               (at module load)
evaluator_core.py  reads  docs/mistake_types.md     (at module load)
knowledge.py       reads  docs/scenarios/*.md                  (at function call)
```

## Key Dependencies

```
langchain           — base abstractions (messages, LLM interface)
langgraph           — StateGraph, add_messages reducer, START/END
langchain-anthropic — ChatAnthropic for Claude Sonnet 4.6 (client + evaluator + alt generator + verdict)
langchain-openai    — ChatOpenAI for Databricks GPT-OSS-120B (retrieval + classify_turn + report)
python-dotenv       — loads .env for API keys
streamlit           — web UI
```

## Config & Setup

### Required
1. Python 3.11+ with virtual environment
2. `pip install -r requirements.txt`
3. `.env` file with:
   - `ANTHROPIC_API_KEY=sk-ant-...`
   - `DATABRICKS_TOKEN=dapi...`
   - `DATABRICKS_BASE_URL=https://...`

### Running
```bash
python main.py                                    # terminal interview, default scenario
python main.py docs/scenarios/some_scenario.md    # terminal interview, custom scenario
streamlit run streamlit_app.py                    # web UI
python test_databricks.py                         # test Databricks model connectivity
```

## LLM Calls Per Session (cost reference)

| Component | Model | Temp | Calls per session | Purpose |
|---|---|---|---|---|
| Retrieval gate | GPT-OSS-120B | 0.0 | 1 per conversation turn (~15–20) | Gate: is question genuine + which items does it earn? |
| Client LLM | Sonnet 4.6 | 0.7 | 1 per conversation turn (~15–20) | Generate client response |
| Turn classifier | GPT-OSS-120B | 0.0 | 1 per consultant turn (~8–15) | Route turn type (question / statement / proposal / etc.) |
| Turn evaluator | Sonnet 4.6 | 0.0 | 1 per question/unproductive turn (~6–12) | Evaluate against 14 mistake types |
| Alt generator | Sonnet 4.6 | 0.3 | 1–3 per ineffective turn (~4–8) | Generate better question (retry loop) |
| Alt pre-check | Sonnet 4.6 | 0.0 | 1–3 per ineffective turn (~4–8) | Validate alt before Stage B simulation |
| Alt simulation | GPT-OSS + Sonnet | 0.0/0.7 | 2 per ineffective turn (~4–8) | Run alt through conversation pipeline |
| Verdict | Sonnet 4.6 | 0.3 | 1 per ineffective turn (~4–8) | Compare original vs alternative pairs |
| Report | GPT-OSS-120B | 0.3 | 1 total | Synthesize feedback report |

**Typical total: ~65–110 LLM calls per full session** (conversation + evaluation).

## Key Design Patterns

**Closure pattern for LangGraph nodes:** Nodes can only receive `state` as argument. `build_nodes(scenario)` and `build_alternative_simulator(conversation_graph)` return node functions as closures that capture external dependencies.

**Markdown-as-config:** Behavior rules and mistake types live in `.md` files, loaded at module level. Editable without touching Python code. Scenario files are also markdown, parsed by `load_scenario()` using header keyword matching.

**Two-graph separation:** Conversation graph and evaluation graph are independent `StateGraph` instances. They share data (transcript, revealed_items) but not logic. Only cross-dependency: alternative_simulator invokes the conversation graph via the closure to simulate responses.

**Statistics in Python, synthesis in LLM:** Turn counts, mistake frequencies, topic coverage, and comparison signals are computed in Python before the report generator LLM call. The LLM receives hard facts and is told not to recalculate.

**Stage C reuses Stage A annotation:** The alternative question is pre-checked with `evaluate_turn()` during the Stage A retry loop. Stage C reuses that annotation for `alt_is_well_formed` — no extra LLM call needed.
