# Synthetic Client Training System

Consultant interview preparation tool for Revodata (a Databricks consulting company).
Real consultants practice interviewing AI-generated synthetic clients that behave
like real organizational stakeholders. System evaluates their performance afterward.

## How It Works
1. Consultant receives an opening from a synthetic client stating their high-level need
2. Multi-turn conversation — consultant asks questions to uncover requirements
3. Synthetic client responds based on its persona and only reveals facts the consultant earns
4. After interview ends, system runs a three-node evaluation pipeline and generates a feedback report
5. Session data (transcript, annotations, alternatives, report) is saved to `logs/`

## Architecture

### Knowledge Gating (core design principle)
The synthetic client cannot reveal what it cannot see. All scenario knowledge is split into two tiers:

- **character_text** — always in the system prompt. Defines identity, personality, team structure.
  Contains ZERO factual data about the platform or situation.
- **character_knowledge (CK)** — rich narrative paragraphs parsed from the `Character Knowledge`
  section. Retrieved fresh each turn as topical context. NOT persisted in state — the client can
  draw on this background without the consultant having "unlocked" it. One item per paragraph.
- **discovery_items (DI)** — specific facts the client progressively discloses. Gated: injected
  into the system prompt only after retrieval confirms the consultant earned them. Persist once
  revealed — re-injected on every subsequent turn. Parsed from `Discovery Items` sections with
  explicit `[DI-XX]` IDs.

### Retrieval System
Retrieval is embedding-based using Voyage AI. No LLM call per turn for retrieval.

Two pre-filters run as cheap rule-based Python checks before any embedding call:
1. **Structural check**: does the input contain a verb or question word? Bare noun phrases
   ("SCIM?", "clusters?") fail immediately.
2. **Intent check**: is it a genuine question, not a reaction or catch-all? Blocks
   acknowledgments, "tell me more", and "what about X?" topic-reference patterns.

If both pass, retrieval queries two separate `EmbeddingStore` indices (Voyage `voyage-3.5-lite`,
cosine similarity) with separate thresholds:
- **CK index** (char_threshold=0.45): returns up to 5 character knowledge paragraphs as
  topical context for the current turn. Retrieved fresh every turn — not persisted.
- **DI index** (disc_threshold=0.55): returns up to 3 discovery items that pass the threshold
  and have not already been revealed. These are persisted in state.

**Context-aware retry**: if `needs_context(question)` is true (referential pronouns at start,
"and/but/so" openers, ≤4 words) OR both indices return empty, retrieval is retried with the
preceding exchange prepended: `"Previous exchange:\n...\n\nCurrent question:\n..."`.

**`EmbeddingStore`**: embeds all items once at construction time (document input_type). Normalises
embeddings so query is a matrix-vector dot product — no API call for similarity. Query vectors are
embedded once per pass and reused for both thresholded results and top-5 debug logging.

**Retrieval traces**: every turn produces a trace dict stored in `ConversationState.retrieval_traces`
and written to session logs. Contains: `retrieval_mode` (question-only / context-aware / blocked),
`retrieved_ck_items` and `matched_di_items` with scores and previews, `newly_revealed_di_ids`,
`excluded_already_revealed_di_ids`. Used for offline threshold calibration.

### Conversation Graph (LangGraph Two-Node)
Each turn: `retrieval_node → client_node`
- `retrieval_node`: no-op pass-through — kept for graph shape compatibility with
  `alternative_simulator.py` which invokes the graph as a black box.
- `client_node`: runs embedding retrieval, builds system prompt
  (character_text + CK context + all revealed DI items), calls client LLM
  (Claude Sonnet 4.6, temp 0.7), returns response + newly revealed DI items + retrieval trace.

State fields:
- `messages`: conversation history (add_messages reducer)
- `revealed_items`: accumulated DI facts, deduplicated by ID. Schema: `{id, content, topic, unlocked_at_turn}`
- `retrieval_traces`: one trace dict per real consultant turn (append-only, None-safe reducer)

### Evaluation Pipeline (LangGraph Three-Node)
Runs after the interview ends. Separate graph from the conversation graph.
Flow: `turn_evaluator → alternative_simulator → report_generator`

**Node 1 — turn_evaluator**
`classify_turn()` receives the full transcript. `evaluate_turn()` receives a **truncated transcript**
— only up to and including the consultant's question, hiding the client's response — to prevent
outcome bias. `evaluate_turn()` also receives `maturity_level` and `briefing` from the scenario;
the prompt notes maturity is relevant to three specific mistake types: "Use jargon", "Ask a technical
question", and "Ask a question inappropriate to user's profile".

Outputs per turn:
- `mistakes`: list containing **at most one** mistake — the single most fundamental root cause. If multiple mistake types seem to apply, they are treated as symptoms of the same underlying problem; only the one that best explains WHY the question failed is returned.
- `is_well_formed`: true if no mistake types apply
- `information_elicited`: gate-based — true iff `unlocked_at_turn == turn_index` for any revealed item

**Node 2 — alternative_simulator**
For every turn where `is_well_formed` is false (questions and unproductive_statements):

- *Stage A*: generates an improved question (Claude Sonnet 4.6, temp 0.3) using only the prior
  transcript — the generator never sees the client's response to the original question.
  Prompt instructs: prefer a better version of the same question; only shift topic if the original
  topic is completely exhausted AND the client offered a thread in their responses; do not use
  company background context (org structure, geography) as basis for a topic shift.
  Retry loop: up to `_MAX_ALT_ATTEMPTS=3`. Each attempt is pre-checked with `evaluate_turn()`;
  if it fails, the mistake is fed back as a `retry_note` for the next attempt.
- *Stage B*: simulates the client's response by invoking the conversation graph with the
  alternative question in place of the original. Seeded with `prior_revealed` — items unlocked
  at turns 1 through N-1 — so the client has the facts they've already shared available in context.
  Original-turn items (unlocked at turn N) are excluded; the alternative question must earn them
  on its own through retrieval.
- *Stage C*: reuses the Stage A pre-check annotation for `alt_is_well_formed` (no extra LLM call).
  `alt_revealed_items` = items uniquely unlocked in simulation that were not in the original
  conversation through turn N (excludes both prior_revealed and items the original question got).
  `alt_information_elicited` = true iff `alt_revealed_items` is non-empty.
  Then makes a separate verdict call (Claude Sonnet 4.6) that compares both question/response
  pairs and generates a one-sentence `improvement_verdict`.

Result dict per alternative:
`turn_index, original_question, original_response, alternative_question, simulated_response,
alt_revealed_items, alt_is_well_formed, improvement_verdict, alt_retrieval_trace`

**Node 3 — report_generator**
One LLM call (Claude Sonnet 4.6, temp 0.3) that receives the full transcript,
all annotations, all alternatives, and pre-computed topic coverage stats. Returns a structured JSON report:
`{summary, continue, stop, start}`. Statistics and coverage stats are computed in Python before the LLM
call — the LLM is told not to recalculate.

Report section definitions:
- **CONTINUE**: 0–2 specific *techniques* the consultant used effectively (named skill, why it worked, turn refs)
- **STOP**: 0–2 *behavior patterns* that caused problems, evidenced by the alternative working better
- **START**: 0–2 *gaps* grounded in missed subtopics from coverage data — not abstract technique advice
- Non-redundancy self-check: the LLM is instructed to verify no point across any section says the same thing from a different angle before outputting. Empty list is valid for any section.
- Max 2 turn refs per point. No word cap — each point is 2–3 sentences.

Coverage computation (`_compute_coverage()`) runs before the LLM call and its result is stored in
`EvaluationState.topic_coverage` so Streamlit can render the coverage UI independently of the report.

### Shared Evaluation Logic — evaluator_core.py
`MISTAKE_TYPES`, `format_transcript`, and `evaluate_turn()` live here.
Both `turn_evaluator.py` and `alternative_simulator.py` import from this module.
Prevents prompt duplication and ensures the alternative is evaluated with identical criteria.

### Scenario File Structure
Two formats are supported. `knowledge.py` detects the format automatically.

**Legacy format** (single-persona, flat `##` sections):
- **METADATA**: `Consultant Briefing`, `Topics`
- **CHARACTER** (always visible): `Identity`, `Maturity Level`, `Team Members`, `Personality and Communication Style`, `Company Overview`, `Character Knowledge`
- **SURFACE** (gated): `What the Client Can Articulate`
- **TACIT** (gated): `What the Client Knows But Won't Volunteer [Tacit Knowledge]`
- **DROPPED**: `Scope Note`, `Technical Reference`

**Multi-persona format** (generated by `scenario_generator/`, detected by `## Persona:` headers):
- **Shared header** (before first `## Persona:`): `## Scenario Parameters` (dropped), `## Topics`, `## Consultant Briefing`
- **Per-persona blocks**: `## Persona: {name}` containing `###` subsections:
  - `### Identity`, `### Persona Maturity`, `### Personality and Communication Style`, `### Team Members` → `character_text`
  - `### Character Knowledge` → `character_knowledge` list (one `ScenarioItem` per double-newline paragraph, IDs `CK-01`, `CK-02`, ...). Markdown sub-headers (`####`) are stripped. NOT in character_text.
  - `### Discovery Items` → `discovery_items`; parsed with explicit `[DI-XX]` IDs
- `load_scenario(path, persona="Danny")` — `persona=` is **required** for multi-persona files. Raises `ValueError` with available names if omitted (no silent default to first persona).
- `scenario.title` = `"{scenario_title} — {persona_name}"`

**Scenario Parameters note**: `interview_stage` (initial_discovery / follow_up / ongoing) is a pipeline parameter injected after extraction — not inferred from notes. `consultant_prior_knowledge` is not extracted; what the consultant knows going in is derived from engagement type and interview stage in the briefing prompt.

Items in `Discovery Items` and `Character Knowledge` sections may carry `[topic: code]` inline
tags (e.g. `[topic: iam/provisioning]`) to associate them with the topic taxonomy. The tag is
stripped from the item content before use. Items without a topic tag still work — they just won't
link to any taxonomy entry.

The Technical Reference section maps client plain language to technical terms — used by
the evaluator, never seen by the client LLM.

## Synthetic Client Behavior Rules
Defined in `docs/behavior_rules.md` (loaded by `client.py` at runtime). 9 scenario-agnostic rules based on C-LEIA research principles: answer only what was asked, no fabrication, express facts through experience, deferral is a last resort, never break character.

## Client Maturity Levels
Three dimensions, each set independently in the `Maturity Level` section of the scenario file:

**Technical knowledge**
- Low: no Databricks terminology; asks consultant to explain everything
- Medium: knows names of concepts but not how they work; can't evaluate proposals
- Medium_High: deeply familiar with the platform architecture from an operational perspective; understands infrastructure configurations and access patterns; lacks vendor consulting expertise — evaluates proposals on operational feasibility, not best practices
- High: uses terms correctly; can react to and challenge proposals

**Self-awareness of problems**
- Low: knows things are broken but can't describe what or why
- Medium: knows specific symptoms from team feedback; can't diagnose root causes
- Medium_High: understands root causes from an operational perspective; can articulate symptoms and diagnoses with technical precision; may have attempted fixes and can explain why they didn't work
- High: can articulate both the problem and what good looks like

**Response to proposals**
- Low: accepts most suggestions; pushes back only on cost or complexity
- Medium: asks clarifying questions; connects proposals to things team members have said
- Medium_High: evaluates proposals on operational impact — pushes back on feasibility, dependencies, team capacity; engages in architecture discussions from a practitioner's perspective; does not evaluate vendor best-practice alignment
- High: evaluates critically; references past attempts or environment-specific constraints

## Scenario Authoring Guidelines
- `Identity`, `Maturity Level`, `Team Members`, `Personality` must contain ZERO platform facts
- `Company Overview` is always visible — only put organisational context here (industry, structure, strategy). No technical platform facts.
- Each maturity dimension (technical knowledge, self-awareness, response to proposals) must be explicitly defined — don't leave any as implicit
- `Personality` is tone and quirks only — no behavioral rules, no maturity-dependent behavior
- `Team Members` lists names and roles only — their concerns are gated surface/tacit items, not character.
  Note: the Team Members note should discourage deferral ("You'd need to ask XYZ" is a dead end).
  Consultants should relay secondhand knowledge using "from what XYZ tells me..." instead.
- Tacit items must be written in the client's plain language — no technical jargon (SCIM, CMK, NCC etc.)
- Technical terms belong only in the Technical Reference section
- Items should carry `[topic: code]` tags matching the `Topics` taxonomy where applicable
- Add a `Consultant Briefing` section at the top with engagement context (client name, meeting type, scope)
- Add a `Topics` section listing all topic codes and display names for the sidebar taxonomy

## Evaluation — What Is Built

### Mistake-based evaluation (complete)
- Per-turn classification against 14 mistake types
- Two independent dimensions: `is_well_formed` and `information_elicited`
- Alternative generation with topic preservation and mistake-avoidance constraints
- Stage C evaluation of alternatives using same criteria as original turns
- One-sentence improvement verdict comparing both question/response pairs
- Structured JSON feedback report `{summary, continue, stop, start}` with non-redundancy self-check; each section answers a distinct question (technique / habit / gap)

### Topic coverage (complete)
- Computed from `revealed_items` topic codes vs. all scenario items' topic codes
- Binary per subtopic: covered if any item with that subtopic was revealed
- A top-level topic is fully/partially/not covered based on its subtopics
- Stats pre-computed in Python (`_compute_coverage()` in `report_generator.py`)
- Passed to report LLM as hard facts; LLM writes the COVERAGE section of the report
- Also rendered in Streamlit as a 2-column grid: bold topic name, colored fraction (green/orange/red), subtopics as inline dot-separated caption in taxonomy order

### Not yet built
- **Interaction strategy**: did the consultant ask questions only, or also propose solutions?
- **Adaptability**: did the consultant adapt to the client's knowledge level over time?

## Session Logging
`session_logger.py` saves two types of files:

**Partial save** (`save_partial_session`): written after every consultant turn during the conversation.
- File: `logs/partial_{session_id}.json` — fixed name, overwritten each turn
- Contains: transcript + revealed_items + retrieval_traces. No evaluation data.
- Purpose: captures conversation even if consultant never clicks End Interview. Best-effort (errors suppressed).

**Full save** (`save_session`): written after evaluation completes.
- **Local**: `logs/sessions/session_YYYY-MM-DD_HH-MM-SS.json`
- **Databricks Apps**: `SESSION_LOG_DIR/sessions/session_YYYY-MM-DD_HH-MM-SS.json` written via Databricks Files API (PUT /api/2.0/fs/files/) — Unity Catalog Volumes are not auto-mounted in App containers.
- Contains: `consultant_email`, timestamp, scenario title, transcript, revealed_items, retrieval_traces, turn_annotations, simulated_alternatives (each with `alt_retrieval_trace`), report dict, summary_stats.

`revealed_items` canonical schema: `{id, content, topic, unlocked_at_turn}` — no `layer` field.

`SESSION_LOG_DIR` defaults to `logs/` locally; set via env var in `app.yaml` for deployment.
`logs/` is gitignored.

## Tech Stack
- Python, LangChain, LangGraph
- Anthropic Claude Sonnet 4.6 for: synthetic client (temp 0.7), turn evaluation / evaluate_turn (temp 0.0), alternative question generation (temp 0.3), report generation (temp 0.3)
- Anthropic Claude Haiku 4.5 for: turn classification / classify_turn (temp 0.0)
- Anthropic Claude Opus 4.6 for: scenario generator pipeline (narrative generation, refinement passes, validation, review)
- Voyage AI (`voyageai`, model `voyage-3.5-lite`) for: embedding-based retrieval — CK and DI indices built once per session; cosine similarity via numpy dot product
- numpy for: in-memory embedding similarity computation
- Databricks GPT-OSS-120B: no longer used in production (still referenced in tests/)
- python-dotenv for API key management (`ANTHROPIC_API_KEY`, `VOYAGE_API_KEY`, `DATABRICKS_TOKEN`, `DATABRICKS_BASE_URL`)
- `EMBEDDING_MODEL` env var: overrides default embedding model (default: `voyage-3.5-lite`)
- Streamlit for UI
- fpdf2: present in requirements but PDF download button removed from UI (Latin-1 rendering limitation)

## Streamlit UI Features
- **Persona selection screen** (multi-persona files only): shown on first load before the conversation starts. Heading: "Choose who to interview first" with a recommendation caption. One card per persona (name, role, maturity hint). Selecting a persona sets `st.session_state.selected_persona` and transitions to conversation. No mid-conversation switching — "Start New Interview" resets to persona selection.
- **Sidebar** (conversation + evaluation phases): alpha caption, End Interview button (top + bottom), "Consultant Briefing" heading (####), briefing fields, "Topics to cover" heading (####). No divider between briefing and topics (reduces vertical whitespace).
- **Conversation**: standard chat interface; partial session JSON (with retrieval_traces) saved after every turn
- **Evaluation progress bar**: Step 1 advances per turn (0–33%), Steps 2–3 are single jumps
- **Evaluation display**: single page, three stacked sections:
  1. Stats bar (4 metrics) + topic coverage grid (2-col, bold topic + colored fraction + subtopic caption line)
  2. Summary sentence + Continue / Stop / Start in three columns
  3. Turn-by-Turn Detail — each turn is its own expander showing badges, You/Client exchange, mistake tag + explanation, and Original vs Alternative side-by-side HTML table where applicable
- **Download**: session log (JSON) only — PDF download removed

## Deployment (Databricks Apps)

The app is deployed as a Databricks App on Azure Databricks. Configuration is in `app.yaml`.

Key deployment details:
- `paths.py` uses `Path(__file__).resolve().parent` for all file paths — safe for any working directory.
- `SESSION_LOG_DIR` is set via env var in `app.yaml` to a Unity Catalog Volume path (`/Volumes/...`).
- UC Volumes are NOT auto-mounted in App containers — `session_logger.py` uses the Databricks Files API
  (`PUT /api/2.0/fs/files/`) to write logs. No directory pre-creation needed; the PUT creates the file directly.
- `DATABRICKS_HOST` is auto-injected by the App runtime (hostname only, no `https://`).
  `_get_workspace_host()` in `session_logger.py` adds the scheme if missing.
- API keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DATABRICKS_TOKEN`) are stored as Databricks secrets
  and referenced via `valueFrom` in `app.yaml`.
- User identity is captured from the `X-Forwarded-Email` request header (auto-injected by the App runtime)
  and stored as `consultant_email` in each session log.

## Commands
- `python main.py` — run terminal interview with default scenario
- `python main.py docs/scenarios/custom_scenario.md` — run with specific scenario
- `streamlit run streamlit_app.py` — run the Streamlit UI
- `pip install -r requirements.txt` — install dependencies
- `python -m pytest tests/` — run tests

## Project Structure
```
agent_v2/
├── main.py                  # terminal conversation loop
├── streamlit_app.py         # Streamlit UI (persona selection, sidebar, evaluation, session log download)
├── graph.py                 # conversation LangGraph; builds embedding indices once per session
├── eval_graph.py            # evaluation LangGraph construction
├── client.py                # retrieval_node (no-op), client_node (embedding retrieval + prompt + LLM)
├── knowledge.py             # scenario parser; EmbeddingStore; structural/intent/needs_context checks; retrieve_relevant_knowledge
├── state.py                 # ConversationState TypedDict (messages, revealed_items, retrieval_traces)
├── evaluation_state.py      # EvaluationState TypedDict
├── evaluator_core.py        # shared MISTAKE_TYPES, format_transcript, evaluate_turn (Sonnet 4.6)
├── turn_evaluator.py        # node 1: per-turn mistake classification
├── alternative_simulator.py # node 2: alternative generation (Sonnet 4.6) + Stage C comparison
├── report_generator.py      # node 3: feedback report synthesis (Sonnet 4.6)
├── session_logger.py        # saves session JSON (partial per-turn + full post-eval); local or Databricks Files API
├── paths.py                 # deployment-safe path resolution; SESSION_LOG_DIR from env
├── app.yaml                 # Databricks Apps deployment config (env vars, secret refs)
├── logs/                    # session logs (gitignored)
├── tests/
│   ├── test_databricks.py           # smoke test for Databricks GPT-OSS endpoint
│   ├── test_eval_comparison.py      # evaluation accuracy: GPT-OSS vs Sonnet (13 cases)
│   └── test_retrieval_comparison.py # retrieval gate accuracy: Haiku vs GPT-OSS low/medium (13 cases)
├── docs/
│   ├── behavior_rules.md            # generic client behavior rules (loaded by client.py)
│   ├── scenarios/
│   │   └── waste_management.md      # multi-persona scenario (Danny + Sajith); generated by scenario_generator/
│   ├── architecture.md              # system design rationale (thesis documentation)
│   └── mistake_types.md             # taxonomy of consultant question mistakes
├── scenario_generator/              # LLM pipeline for generating scenario files from engagement notes
│   ├── pipeline.py                  # orchestrator: run_from_notes(), run_from_scratch(), resume(), combine_personas()
│   ├── config.py                    # shared config, llm_call(), I/O helpers, MATURITY_LEVELS
│   ├── cli.py                       # CLI entry point (python -m scenario_generator.cli)
│   ├── phase0_generate.py           # generate scenario from parameters (no source notes)
│   ├── phase1_extract.py            # extract structured facts from engagement notes
│   ├── phase2_anonymize.py          # anonymize identifying information
│   ├── phase3_classify.py           # classify facts into CK/DI/drop; persona-aware; taxonomy generation
│   ├── phase3_5_completeness.py     # completeness check + gap-fill
│   ├── phase4_narrate.py            # generate character knowledge narrative (Opus)
│   ├── phase5_validate.py           # inference path validation with autofix loop (Opus)
│   ├── phase6_assemble.py           # per-persona assembly + multi-persona combine
│   └── phase7_review.py             # dedup, revalidation, retag, review checklist
└── requirements.txt
```

## Scenario Generator Pipeline

Multi-phase LLM pipeline in `scenario_generator/` for producing scenario files from raw engagement notes or parameters. Human reviews intermediate outputs between phases; the pipeline encodes design principles so LLM output is principled by default.

### Pipeline Phases
- **Phase 0** (optional): generate synthetic notes from parameters (no source material)
- **Phase 1**: extract structured facts from engagement notes → `phase1_extraction_output.json`
- **Phase 2** (optional): anonymize company/person names → `phase2_anonymized_output.json`
- **Taxonomy** (once): generate shared topic taxonomy from full extraction → `scenario_taxonomy.json`
- **Phase 3** (per persona): classify facts into character knowledge / discovery items / drop; Opus refinement pass; plain-language rewrite; CK/DI dedup; retag to shared taxonomy → `phase3_classified_{persona}.json`
- **Phase 3.5** (per persona): completeness check + gap-fill → `phase3_5_completeness_{persona}.json`
- **Phase 4** (per persona): generate character knowledge narrative (Opus) → `phase4_narrative_{persona}.md`
- **Phase 5** (per persona): inference path validation with autofix loop (Opus, up to 3 iterations) → `phase5_validation_{persona}.json`
- **Phase 6** (per persona): assemble single-persona scenario file; `run_phase6_combine()` merges all personas into combined file → `scenario_assembled_{persona}.md` + `docs/scenarios/{name}.md`
- **Phase 7** (per persona): dedup, revalidation, retag, review checklist → `scenario_final_reviewed_{persona}.md`

### Key Pipeline Parameters
- `personas`: list of `{"name": str, "role": str, "maturity": str}` — Phases 3–7 run once per persona
- `interview_stage`: pipeline-provided (`initial_discovery` / `follow_up` / `ongoing`), not extracted from notes
- `anonymize`: whether to run Phase 2; defaults to False
- Maturity values: `LOW`, `MEDIUM`, `MEDIUM_HIGH`, `HIGH`

### Running the Pipeline
```python
from scenario_generator.pipeline import run_from_notes, resume, combine_personas

# Full run (auto_run=True skips review checkpoints — for testing only)
run_from_notes(
    notes_path='scenario_generator/notes/engagement.txt',
    scenario_name='my_scenario',
    anonymize=True,
    interview_stage='initial_discovery',
    personas=[
        {'name': 'Danny', 'role': 'manager of the data platform team', 'maturity': 'LOW'},
        {'name': 'Sajith', 'role': 'Solutions Architect for the Data Platform', 'maturity': 'MEDIUM_HIGH'},
    ],
    auto_run=True,
)

# Resume from a specific phase for one persona
resume(scenario_name='my_scenario', from_phase=3, persona_name='Sajith',
       persona_role='Solutions Architect for the Data Platform', persona_maturity='MEDIUM_HIGH',
       source_phase='phase2_anonymized', auto_run=True)

# Combine personas after all Phase 7 runs complete
combine_personas('my_scenario', [
    {'name': 'Danny', 'role': '...', 'maturity': 'LOW'},
    {'name': 'Sajith', 'role': '...', 'maturity': 'MEDIUM_HIGH'},
])
```

### Taxonomy Generation
Taxonomy is generated once per scenario from the full Phase 1/2 extraction (all facts, before any persona-specific classification). Saved as `scenario_taxonomy.json` in the workspace. Phase 3 loads this shared taxonomy for retagging instead of generating per-persona. Phase 6 loads it for the Topics section, pruning codes that have no items tagged.

## Key Design Decisions
- **No facts in character_text**: the only reliable way to prevent leakage is to not give
  the LLM the information at all. Rules cannot reliably suppress what the LLM can see.
- **Embedding retrieval, not LLM gate**: semantic matching is now done via Voyage embeddings
  (cosine similarity) rather than an LLM call per turn. Pre-filters (structural, intent) remain
  as cheap rule-based Python checks. This removes per-turn LLM cost for retrieval, makes results
  deterministic and inspectable (scores logged), and enables offline threshold calibration via
  retrieval traces.
- **Two-tier knowledge injection**: CK items provide contextual background retrieved fresh each
  turn; DI items are stateful disclosures that persist once revealed. Keeping these separate
  prevents a consultant gaining permanent "credit" for contextual background that should always
  be available.
- **Background context belongs in character_text / CK, not gated DI items**: tooling stack,
  team size, migration status are things the client knows freely as a manager — they don't warrant
  discovery. Only facts that require a specific question to earn belong in discovery_items. This
  prevents early turns from dumping large amounts of platform context.
- **`persona=` required for multi-persona files**: `load_scenario()` raises `ValueError` if
  `persona` is omitted on a multi-persona file. Silent fallback to "first persona in file" was
  position-dependent and invisible — a file reorder would silently load the wrong persona.
- **Model routing by task type**: Claude Sonnet 4.6 for synthetic client (temp 0.7),
  `evaluate_turn()` (temp 0.0), alternative generation (temp 0.3), report (temp 0.3).
  Claude Haiku 4.5 for `classify_turn()` (simple routing, temp 0.0). Claude Opus 4.6 for
  scenario generator (narrative, validation, review). Voyage AI for retrieval embedding.
  GPT-OSS-120B no longer used in production.
- **Plain language in discovery items**: technical terms in scenario items caused the client to
  use jargon it couldn't explain, creating incoherence. The client speaks in plain language.
- **Context passed to retrieval only when needed**: `needs_context()` detects referential or
  incomplete questions (subject-position pronoun at start, follow-up openers, ≤4 words) before
  triggering context-aware retry. Self-contained questions — even those containing "it" or "that"
  mid-sentence with a local antecedent — stay in question-only mode to avoid unnecessary API calls.
- **Two independent evaluation dimensions**: `is_well_formed` (question quality) and
  `information_elicited` (outcome) are assessed separately. A well-formed question can fail
  to elicit information if the client doesn't have the answer — these are different problems
  requiring different interventions.
- **Stage C reuses evaluator_core**: the alternative question is evaluated with the exact
  same prompt and criteria as the original. This makes the comparison meaningful — both
  sides are judged on the same scale.
- **Gold examples drive the report**: turns where the original failed but the alternative
  succeeded (`information_elicited: false` → `alt_information_elicited: true`) are the most
  instructive. The report generator is told to prioritise these as primary examples in Stop/Start
  rather than re-deriving recommendations from scratch.
- **Statistics computed in Python**: turn counts and mistake frequencies are computed before
  the report LLM call and passed as hard facts. The LLM is told not to recalculate — removes
  a known failure mode where LLMs miscount from long annotation lists.
- **Topic taxonomy as authoring + UI contract**: the `Topics` section in a scenario file is
  both an authoring reference (which topic codes to use in `[topic: X]` tags) and the
  source for the sidebar "Topics to cover" display. Parent topics are bold; subtopics shown
  as dot-separated captions. Items without a topic tag still work — they just won't link to
  any taxonomy entry. The tier system (TIER 1/2/3) has been removed; topic coverage will
  replace it as the coverage metric in a future change.
- **Deferral is a last resort**: rewriting behavior rules from "hostile witness" to Grice's
  Cooperative Principle. The client relays secondhand knowledge from colleagues rather than
  redirecting. Rule 7 distinguishes unclear framing (answer + flag) from genuinely ambiguous
  questions (ask for clarification first).
