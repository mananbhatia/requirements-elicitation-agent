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
  Gated: unlocked by genuine specific questions. Parsed from Company Overview,
  Current Data Platform, What the Client Can Articulate sections.
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
3. Relevance matching: each unrevealed item is assessed independently. Tacit items require a
   stricter bar (question must ask about current state/process, not just the topic area).
   Calibration: most questions match 1–2 items; a well-targeted question might match 3–4;
   matching 5+ is rare.

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
Makes one LLM call per consultant turn using the full conversation transcript as context.
Outputs per turn:
- `mistakes`: list of flagged mistake types with explanations (from docs/evaluation/mistake_types.md)
- `is_well_formed`: true if no mistake types apply
- `information_elicited`: true if the client's response contained substantive new information

Both fields are assessed independently — a well-formed question can fail to elicit information
if the client doesn't have the answer, and vice versa.

**Node 2 — alternative_simulator**
For every turn where `is_well_formed` is false OR `information_elicited` is false:

- *Stage A*: generates an improved question (Claude Sonnet 4.6, temp 0.3) using only the prior
  transcript — the generator never sees the client's response to the original question.
  Constraints: same topic as original, free of all 14 mistake types.
- *Stage B*: simulates the client's response by invoking the conversation graph with the
  alternative question in place of the original.
- *Stage C*: evaluates the alternative using the shared `evaluate_turn()` from `evaluator_core.py`
  (same prompt/logic as Node 1), producing `alt_is_well_formed` and `alt_information_elicited`.
  Then makes a separate verdict call that compares both question/response pairs and generates
  a one-sentence `improvement_verdict`.

Result dict per alternative:
`turn_index, original_question, original_response, alternative_question, simulated_response,
alt_is_well_formed, alt_information_elicited, improvement_verdict`

**Node 3 — report_generator**
One LLM call (Databricks GPT-OSS-120B, temp 0.3, reasoning_effort=high) that receives the full transcript, all annotations,
and all alternatives, then writes the feedback report in SUMMARY / CONTINUE / STOP / START format.
Statistics (turn counts, mistake frequencies, gold example count) are computed in Python
and passed as hard facts — the LLM is told not to recalculate.

The prompt prioritises turns where `alt_information_elicited` flipped from false to true
as primary examples in Stop/Start — these are the gold examples with proven improvement.

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
  - `Company Overview` — public context the client knows freely as a manager (industry, org structure, strategic direction). TIER 3 only. No platform facts.
- **SURFACE** (gated): Current Data Platform, What the Client Can Articulate
- **TACIT** (gated): What the Client Knows But Won't Volunteer [Tacit Knowledge]
- **DROPPED** (never used): Scope Note, Technical Reference [EVALUATION ONLY]

Items in SURFACE and TACIT sections may carry `[topic: code]` inline tags (e.g. `[topic: iam/provisioning]`)
to associate them with the topic taxonomy. The tag is stripped from the item content before injection.
TIER 1 and TIER 2 items should be tagged; TIER 3 context items typically are not.

The Technical Reference section maps client plain language to technical terms — used by
the evaluator, never seen by the client LLM.

## Synthetic Client Behavior Rules (in docs/behavior_rules.md, loaded by client.py)
Generic rules, scenario-agnostic, based on C-LEIA research principles:
1. Answer only what was asked — stop there
2. Never volunteer information
3. Only know what is in context — no fabrication, no approximate answers
4. Never give recommendations or priorities — redirect to consultant
5. Never break character
6. No bullets, no formatting, no markdown — prose only
7. Unclear framing → answer what you understand + flag the term; genuinely ambiguous (multiple valid
   interpretations) → ask what they mean first. Relay secondhand knowledge from colleagues as your
   own ("from what Thomas tells me...") rather than redirecting. Deferral is a last resort.
8. When deferring, name the specific team member; let the consultant lead technical explanations
9. Express facts through experience and reaction, not as statements
10. Only ask questions when genuinely confused — not to hand control back

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
- `Company Overview` is always visible — only put TIER 3 organisational context here (industry, structure, strategy). No technical platform facts.
- Each maturity dimension (technical knowledge, self-awareness, response to proposals) must be explicitly defined — don't leave any as implicit
- `Personality` is tone and quirks only — no behavioral rules, no maturity-dependent behavior
- `Team Members` lists names and roles only — their concerns are gated surface/tacit items, not character.
  Note: the Team Members note should discourage deferral ("You'd need to ask XYZ" is a dead end).
  Consultants should relay secondhand knowledge using "from what XYZ tells me..." instead.
- Tacit items must be written in the client's plain language — no technical jargon (SCIM, CMK, NCC etc.)
- Technical terms belong only in the Technical Reference section
- Tier labels on items: TIER 1 (must explore), TIER 2 (good consultants cover), TIER 3 (context)
- TIER 1 and TIER 2 items should carry `[topic: code]` tags matching the `Topics` taxonomy
- Add a `Consultant Briefing` section at the top with engagement context (client name, meeting type, scope)
- Add a `Topics` section listing all topic codes and display names for the sidebar taxonomy

## Evaluation — What Is Built

### Mistake-based evaluation (complete)
- Per-turn classification against 14 mistake types
- Two independent dimensions: `is_well_formed` and `information_elicited`
- Alternative generation with topic preservation and mistake-avoidance constraints
- Stage C evaluation of alternatives using same criteria as original turns
- One-sentence improvement verdict comparing both question/response pairs
- Feedback report with SUMMARY / CONTINUE / STOP / START using gold examples as evidence

### Not yet built
- **Solution space coverage**: which TIER 1 items did the consultant reach vs. miss?
  (`scenario_items_total` and `revealed_items` are already in `EvaluationState` in anticipation)
- **Interaction strategy**: did the consultant ask questions only, or also propose solutions?
- **Adaptability**: did the consultant adapt to the client's knowledge level over time?

## Session Logging
After each evaluation, `session_logger.py` saves a JSON file to `logs/`:
`logs/session_YYYY-MM-DD_HH-MM-SS.json`

Contains: timestamp, scenario title, transcript (role+content), revealed items (id/content/tier/layer/**topic**),
turn_annotations, simulated_alternatives (including all Stage C fields), report string, summary_stats.

`logs/` is gitignored.

## Tech Stack
- Python, LangChain, LangGraph
- Anthropic Claude Sonnet 4.6 for client LLM (temp 0.7) and alternative question generation (temp 0.3)
- Databricks GPT-OSS-120B (OpenAI-compatible endpoint) for retrieval (reasoning_effort=medium), turn evaluation (reasoning_effort=high), and report generation (reasoning_effort=high)
- python-dotenv for API key management (`DATABRICKS_TOKEN`, `DATABRICKS_BASE_URL`)
- Streamlit for UI

## Streamlit UI Features
- Sidebar shows consultant briefing (parsed from `Consultant Briefing` section) and topic taxonomy
  (`Topics` section) with parent topics bold and subtopics listed as `·`-separated captions
- Evaluation progress bar: Step 1 (turn evaluation) advances per turn (0–33%), Steps 2–3 are single jumps
- Feedback Report tab includes a download button that saves the report as a dated markdown file
- Progress bar appears below the conversation transcript, not above it

## Commands
- `python main.py` — run terminal interview with default scenario
- `python main.py docs/scenarios/custom_scenario.md` — run with specific scenario
- `streamlit run streamlit_app.py` — run the Streamlit UI
- `pip install -r requirements.txt` — install dependencies
- `python -m pytest tests/` — run tests (not yet written)

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
├── evaluator_core.py        # shared MISTAKE_TYPES, format_transcript, evaluate_turn (GPT-OSS high)
├── turn_evaluator.py        # node 1: per-turn mistake classification
├── alternative_simulator.py # node 2: alternative generation (Sonnet) + Stage C comparison
├── report_generator.py      # node 3: feedback report synthesis (GPT-OSS high)
├── session_logger.py        # saves session JSON to logs/ (includes topic field on revealed items)
├── logs/                    # session logs (gitignored)
├── test_databricks.py       # smoke test for Databricks GPT-OSS endpoint
├── test_eval_comparison.py  # side-by-side evaluation accuracy: GPT-OSS vs Sonnet (13 cases)
├── test_retrieval_comparison.py # retrieval gate accuracy: Haiku vs GPT-OSS low/medium (13 cases)
├── docs/
│   ├── behavior_rules.md                # generic client behavior rules (loaded by client.py)
│   ├── scenarios/
│   │   └── waste_management_client.md   # first scenario (GreenCycle Industries); includes Topics + Consultant Briefing
│   ├── evaluation/
│   │   └── mistake_types.md             # taxonomy of consultant question mistakes
│   └── research/
│       └── client_design_principles.md  # C-LEIA-based authoring principles
└── requirements.txt
```

## Key Design Decisions
- **No facts in character_text**: the only reliable way to prevent leakage is to not give
  the LLM the information at all. Rules cannot reliably suppress what the LLM can see.
- **Retrieval is a gate, not a search**: the retrieval LLM's job is to disqualify, not to find.
  Most turns should return nothing. The number of items returned is proportional to what the
  question earned — a vague question earns nothing, a well-targeted question covering multiple
  dimensions of a topic can earn several.
- **GPT-OSS for retrieval and evaluation**: retrieval and evaluation use Databricks GPT-OSS-120B.
  Testing showed GPT-OSS medium significantly outperforms Haiku on over-matching prevention
  (e.g. "tell me about your setup" → Haiku: 24 items, GPT-OSS low: 29 items, GPT-OSS medium: 0).
  Evaluation accuracy is roughly equivalent to Sonnet (6/13 vs 5/13 on 13-case test).
  GPT-OSS also returns responses as `[reasoning_block, text_block]` lists rather than plain strings;
  `_extract_content()` in `evaluator_core.py` normalises this.
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
  any taxonomy entry.
- **Deferral is a last resort**: rewriting behavior rules from "hostile witness" to Grice's
  Cooperative Principle. The client relays secondhand knowledge from colleagues rather than
  redirecting. Rule 7 distinguishes unclear framing (answer + flag) from genuinely ambiguous
  questions (ask for clarification first).
