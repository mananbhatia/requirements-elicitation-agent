# Synthetic Client Training System

Consultant interview preparation tool for Revodata (a Databricks consulting company).
Real consultants practice interviewing AI-generated synthetic clients that behave
like real organizational stakeholders. System evaluates their performance afterward.

## How It Works
1. Consultant receives an opening from a synthetic client stating their high-level need
2. Multi-turn conversation — consultant asks questions to uncover requirements
3. Synthetic client responds based on its persona and only reveals facts the consultant earns
4. After interview ends, system evaluates and generates a feedback report (not yet built)

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
Each consultant turn runs through a retrieval LLM call (GPT-4o, temp 0.0) before the
client responds. The retrieval decides whether the question is genuine and specific enough
to unlock a new fact. Returns at most ONE item per turn.

Retrieval uses a two-step approach:
1. Structural check: does the input contain a verb or question word? Bare noun phrases fail immediately.
2. Intent check: does it ask about this client's specific situation? Topic references ("SCIM?",
   "what about X?") are disqualified. Catch-alls are disqualified.

The last 2 conversation turns are passed as context so follow-up questions resolve correctly
(e.g. "is it acceptance or production?" maps to PowerBI after discussing PowerBI).

Retrieval returns `{"is_genuine": bool, "matched_ids": [...]}`. If `is_genuine` is false,
no item is revealed regardless of matched_ids.

### LangGraph Two-Node Design
Each turn: `retrieval_node → client_node`
- `retrieval_node`: reads latest human message + conversation context, calls retrieval LLM,
  returns newly unlocked items as dicts
- `client_node`: builds system prompt (character_text + all revealed items so far), calls
  client LLM (GPT-4o, temp 0.7), returns response

State: `messages` (conversation history, using add_messages reducer) +
`revealed_items` (accumulated facts, deduplicated by ID via custom reducer)

### Scenario File Structure
Markdown files in `docs/scenarios/`. Sections classified by header:
- **CHARACTER** (always visible): four sections, each a distinct authoring knob:
  - `Identity` — who the client is, their role, company, and meeting context
  - `Maturity Level` — three behavioral dimensions: technical knowledge, self-awareness of problems, response to proposals. Also sets deferral behavior. Set to Low / Medium / High.
  - `Team Members` — names and roles only; who to defer to
  - `Personality and Communication Style` — tone, register, quirks; how they speak, not what they know
- **SURFACE** (gated): Company Overview, Current Data Platform, What the Client Can Articulate
- **TACIT** (gated): What the Client Knows But Won't Volunteer [Tacit Knowledge]
- **DROPPED** (never used): Scope Note, Technical Reference [EVALUATION ONLY]

The Technical Reference section maps client plain language to technical terms — used by
the evaluator, never seen by the client LLM.

## Synthetic Client Behavior Rules (in client.py)
Generic rules, scenario-agnostic, based on C-LEIA research principles:
1. Answer only what was asked — stop there
2. Never volunteer information
3. Only know what is in context — no fabrication, no implied familiarity with unknown tools
4. Never give recommendations or priorities — redirect to consultant
5. Never break character
6. No bullets, no formatting, no markdown — prose only
7. Vague questions → minimal response + ask for clarification (not deferral)
8. When deferring, name the specific team member if known
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
- Each maturity dimension (technical knowledge, self-awareness, response to proposals) must be explicitly defined — don't leave any as implicit
- `Personality` is tone and quirks only — no behavioral rules, no maturity-dependent behavior
- `Team Members` lists names and roles only — their concerns are gated surface/tacit items, not character
- Tacit items must be written in the client's plain language — no technical jargon (SCIM, CMK, NCC etc.)
- Technical terms belong only in the Technical Reference section
- Tier labels on items: TIER 1 (must explore), TIER 2 (good consultants cover), TIER 3 (context)

## Evaluation (not yet built)
Criteria for judging consultant performance:
- **Solution space coverage**: which dimensions explored vs. missed?
- **Question quality**: assessed against established mistake types (docs/evaluation/mistake_types.md)
- **Interaction strategy**: questions only, or also proposed solutions/directions?
- **Adaptability**: adapted to client's knowledge level?

Output: one-page feedback report. Structure: Continue / Stop / Start
- Continue: effective questions, keep these
- Stop: ineffective or counterproductive questions, with reasons
- Start: questions that should have been asked but weren't

Evaluator has access to the full scenario file including Technical Reference section
for ground truth technical terminology.

## Tech Stack
- Python, LangChain, LangGraph
- OpenAI GPT-4o for both retrieval and client LLM
- python-dotenv for API key management
- Streamlit for UI (not yet built)

## Commands
- `python main.py` — run terminal interview with default scenario
- `python main.py docs/scenarios/custom_scenario.md` — run with specific scenario
- `pip install -r requirements.txt` — install dependencies
- `python -m pytest tests/` — run tests (not yet written)

## Project Structure
```
agent_v2/
├── main.py          # terminal conversation loop
├── graph.py         # LangGraph graph construction
├── client.py        # retrieval_node, client_node, behavior rules
├── knowledge.py     # scenario parser, retrieval LLM call
├── state.py         # ConversationState TypedDict, custom reducers
├── docs/
│   └── scenarios/
│       └── waste_management_client.md   # first scenario (GreenCycle Industries)
└── requirements.txt
```

## Key Design Decisions
- **No facts in character_text**: the only reliable way to prevent leakage is to not give
  the LLM the information at all. Rules cannot reliably suppress what the LLM can see.
- **One item per turn**: forces consultants to ask specific follow-up questions; prevents
  information dumps from single vague questions.
- **Retrieval is a gate, not a search**: the retrieval LLM's job is to disqualify, not to find.
  Most turns should return nothing. Specific, well-formed questions earn one fact.
- **Plain language in tacit items**: technical terms in scenario items caused the client to
  use jargon it couldn't explain, creating incoherence. Danny speaks Danny's language.
- **Context passed to retrieval**: last 2 turns of conversation passed so follow-up questions
  ("is it X or Y?") resolve correctly without requiring the consultant to repeat the topic.
