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
The synthetic client cannot reveal what it cannot see. All scenario knowledge is split into three layers:

- **character_text** — always in the system prompt. Defines identity, personality, team structure.
  Contains ZERO factual data about the platform or situation.
- **surface_items** — facts the client would share if asked about the relevant topic.
  Gated: unlocked by genuine specific questions. Parsed from What the Client Can Articulate.
- **tacit_items** — facts the client guards carefully. Written in plain client language,
  no technical jargon. Gated: unlocked only when asked specifically. Parsed from
  the "What the Client Knows But Won't Volunteer" section.

### Retrieval System
Each consultant turn runs through a retrieval LLM call (Databricks GPT-OSS-120B, temp 0.0,
reasoning_effort=medium) before the client responds. The retrieval gate decides whether the
question is genuine and, if so, which items it earned. The number of items returned is an
output of the relevance judgment — not a hard cap. Follows Grice's Maxim of Quantity: items
returned should be proportional to what the question covered.

Retrieval uses a three-step approach:
1. Structural check: does the input contain a verb or question word? Bare noun phrases fail immediately.
2. Intent check: does it ask about this client's specific situation? Topic references ("SCIM?",
   "what about X?") are disqualified. Catch-alls are disqualified.
3. Relevance matching: **direct specificity, not topical association**. For each candidate item,
   the test is: "if this item did not exist, would the question go unanswered?" If yes, it's a
   direct match. If the item is merely about the same topic area, it is not returned. Broad
   questions earn 0–1 items; specific questions earn the items they directly target. Tacit items
   require a stricter bar (question must ask about current state/process, not just the topic area).
   `matched_ids` is ordered by decreasing relevance — most directly targeted item first.
   A code-level `[:3]` cap in `retrieve_relevant_knowledge()` guards against prompt misjudgements;
   if it binds regularly, the prompt needs tuning, not the cap.

The last 2 conversation turns are passed as context so follow-up questions resolve correctly
(e.g. "is it acceptance or production?" maps to PowerBI after discussing PowerBI).

Retrieval returns `{"is_genuine": bool, "matched_ids": [...]}`. If `is_genuine` is false,
no item is revealed regardless of matched_ids.

### Conversation Graph (LangGraph Two-Node)
Each turn: `retrieval_node → client_node`
- `retrieval_node`: reads latest human message + conversation context, calls retrieval LLM,
  returns newly unlocked items as dicts
- `client_node`: builds system prompt (character_text + all revealed items so far), calls
  client LLM (Claude Sonnet 4.6, temp 0.7), returns response

State: `messages` (conversation history, using add_messages reducer) +
`revealed_items` (accumulated facts, deduplicated by ID via custom reducer)

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
  at turns 1 through N-1 — so Danny has the facts he's already shared available in context.
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
alt_revealed_items, alt_is_well_formed, alt_information_elicited, improvement_verdict`

**Node 3 — report_generator**
One LLM call (GPT-OSS-120B, high reasoning, temp 0.3) that receives the full transcript,
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
Markdown files in `docs/scenarios/`. Sections classified by header:
- **METADATA** (consultant-facing, never sent to client LLM):
  - `Consultant Briefing` — engagement context shown in Streamlit sidebar (client name, meeting type, what they asked for, consultant role). Parsed into `scenario.briefing`.
  - `Topics` — flat list of `code: Display Name` pairs defining the topic taxonomy. Parsed into `scenario.topic_taxonomy`. Shown in sidebar under "Topics to cover".
- **CHARACTER** (always visible): five sections, each a distinct authoring knob:
  - `Identity` — who the client is, their role, company, and meeting context
  - `Maturity Level` — three behavioral dimensions: technical knowledge, self-awareness of problems, response to proposals. Also sets deferral behavior. Set to Low / Medium / High.
  - `Team Members` — names and roles only; who to defer to
  - `Personality and Communication Style` — tone, register, quirks; how they speak, not what they know
  - `Company Overview` — public context the client knows freely as a manager (industry, org structure, strategic direction). Always visible. No platform facts.
- **SURFACE** (gated): What the Client Can Articulate
- **TACIT** (gated): What the Client Knows But Won't Volunteer [Tacit Knowledge]
- **DROPPED** (never used): Scope Note, Technical Reference [EVALUATION ONLY]

Items in SURFACE and TACIT sections may carry `[topic: code]` inline tags (e.g. `[topic: iam/provisioning]`)
to associate them with the topic taxonomy. The tag is stripped from the item content before injection.
Items without a topic tag still work — they just won't link to any taxonomy entry.

The Technical Reference section maps client plain language to technical terms — used by
the evaluator, never seen by the client LLM.

## Synthetic Client Behavior Rules
Defined in `docs/behavior_rules.md` (loaded by `client.py` at runtime). 9 scenario-agnostic rules based on C-LEIA research principles: answer only what was asked, no fabrication, express facts through experience, deferral is a last resort, never break character.

## Client Maturity Levels
Three dimensions, each set independently in the `Maturity Level` section of the scenario file:

**Technical knowledge**
- Low: no Databricks terminology; asks consultant to explain everything
- Medium: knows names of concepts but not how they work; can't evaluate proposals
- High: uses terms correctly; can react to and challenge proposals

**Self-awareness of problems**
- Low: knows things are broken but can't describe what or why
- Medium: knows specific symptoms from team feedback; can't diagnose root causes
- High: can articulate both the problem and what good looks like

**Response to proposals**
- Low: accepts most suggestions; pushes back only on cost or complexity
- Medium: asks clarifying questions; connects proposals to things team members have said
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
After each evaluation, `session_logger.py` saves a JSON file:
- **Local**: `logs/sessions/session_YYYY-MM-DD_HH-MM-SS.json`
- **Databricks Apps**: `SESSION_LOG_DIR/sessions/session_YYYY-MM-DD_HH-MM-SS.json` written via Databricks Files API (PUT /api/2.0/fs/files/) — Unity Catalog Volumes are not auto-mounted in App containers.

`SESSION_LOG_DIR` defaults to `logs/` locally; set via env var in `app.yaml` for deployment.

Contains: `consultant_email` (from `X-Forwarded-Email` header on Databricks), timestamp, scenario title,
transcript (role+content), revealed items (id/content/layer/topic/unlocked_at_turn),
turn_annotations, simulated_alternatives (including `alt_revealed_items` and all Stage C fields),
report dict `{summary, continue, stop, start}`, summary_stats.
Note: `topic_coverage` and `stats` are stored in `EvaluationState` but not currently persisted to the session log.

`logs/` is gitignored.

## Tech Stack
- Python, LangChain, LangGraph
- Anthropic Claude Sonnet 4.6 for: synthetic client (temp 0.7), turn evaluation / evaluate_turn (temp 0.0), alternative question generation (temp 0.3)
- Databricks GPT-OSS-120B (OpenAI-compatible endpoint) for: retrieval gate (medium), turn classification / classify_turn (low), report generation (medium)
- python-dotenv for API key management (`DATABRICKS_TOKEN`, `DATABRICKS_BASE_URL`)
- Streamlit for UI

## Streamlit UI Features
- Sidebar shows consultant briefing (parsed from `Consultant Briefing` section) and topic taxonomy
  (`Topics` section) with parent topics bold and subtopics listed as `·`-separated captions
- Evaluation progress bar: Step 1 (turn evaluation) advances per turn (0–33%), Steps 2–3 are single jumps
- Evaluation display: single page, three stacked sections:
  1. Stats bar (4 metrics) + topic coverage grid (2-col, bold topic + colored fraction + subtopic caption line)
  2. Summary sentence + Continue / Stop / Start in three columns
  3. Turn-by-Turn Detail (heading) — each turn is its own expander showing badges, You/Danny exchange, mistake tag + explanation, and Original vs Alternative side-by-side HTML table where applicable
- Download session log button below the turn-by-turn section

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
├── streamlit_app.py         # Streamlit UI (sidebar briefing/taxonomy, granular progress bar, download button)
├── graph.py                 # conversation LangGraph construction
├── eval_graph.py            # evaluation LangGraph construction
├── client.py                # retrieval_node, client_node, behavior rules (Claude Sonnet 4.6)
├── knowledge.py             # scenario parser (briefing, topic_taxonomy, topic tags), retrieval LLM (GPT-OSS)
├── state.py                 # ConversationState TypedDict, custom reducers
├── evaluation_state.py      # EvaluationState TypedDict
├── evaluator_core.py        # shared MISTAKE_TYPES, format_transcript, evaluate_turn (Sonnet 4.6)
├── turn_evaluator.py        # node 1: per-turn mistake classification
├── alternative_simulator.py # node 2: alternative generation (Sonnet 4.6) + Stage C comparison
├── report_generator.py      # node 3: feedback report synthesis (GPT-OSS high)
├── session_logger.py        # saves session JSON; local filesystem or Databricks Files API
├── paths.py                 # deployment-safe path resolution; SESSION_LOG_DIR from env
├── app.yaml                 # Databricks Apps deployment config (env vars, secret refs)
├── logs/                    # session logs (gitignored)
├── tests/
│   ├── test_databricks.py           # smoke test for Databricks GPT-OSS endpoint
│   ├── test_eval_comparison.py      # evaluation accuracy: GPT-OSS vs Sonnet (13 cases)
│   └── test_retrieval_comparison.py # retrieval gate accuracy: Haiku vs GPT-OSS low/medium (13 cases)
├── docs/
│   ├── behavior_rules.md                # generic client behavior rules (loaded by client.py)
│   ├── scenarios/
│   │   └── waste_management_client.md   # first scenario (GreenCycle Industries); includes Topics + Consultant Briefing
│   ├── architecture.md                  # system design rationale (thesis documentation)
│   └── mistake_types.md                 # taxonomy of consultant question mistakes
└── requirements.txt
```

## Key Design Decisions
- **No facts in character_text**: the only reliable way to prevent leakage is to not give
  the LLM the information at all. Rules cannot reliably suppress what the LLM can see.
- **Retrieval matches on direct specificity, not topical association**: the gate asks "would the
  question go unanswered without this item?" — not "is this item about the same topic?". A broad
  question about a topic area earns 0–1 items. A specific question earns the items it directly
  targets. A `[:3]` code-level cap guards against prompt misjudgement; if it binds regularly,
  the prompt needs tuning. `matched_ids` is ordered by relevance so the cap takes the strongest matches.
- **Background context belongs in character_text, not gated items**: tooling stack, team size,
  migration status are things Danny knows freely as a manager — they don't warrant discovery.
  Only facts that require a specific question to earn belong in surface/tacit. This prevents
  early turns from dumping large amounts of platform context.
- **Model routing by task type**: Claude Sonnet 4.6 is used for synthetic client (persona fidelity,
  temp 0.7), `evaluate_turn()` (analytical precision, temp 0.0), and alternative question generation
  (creative rewriting, temp 0.3). Databricks GPT-OSS-120B is used for retrieval (medium reasoning),
  `classify_turn()` (low reasoning — simple routing task), and report generation (high reasoning, temp 0.3).
  GPT-OSS returns responses as `[reasoning_block, text_block]` lists; `_extract_content()` normalises
  this; all `.invoke()` calls are wrapped in `warnings.catch_warnings()` to suppress the Pydantic
  serializer warning this triggers.
- **Plain language in tacit items**: technical terms in scenario items caused the client to
  use jargon it couldn't explain, creating incoherence. Danny speaks Danny's language.
- **Context passed to retrieval**: last 2 turns of conversation passed so follow-up questions
  ("is it X or Y?") resolve correctly without requiring the consultant to repeat the topic.
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
