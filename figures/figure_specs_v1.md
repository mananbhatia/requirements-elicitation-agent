# Thesis Figure Specs — Figs 5.1, 5.2, 6.1 (build-ready)

Derived from and verified against `architecture.md`, `graph.py`, `eval_graph.py`, `knowledge.py`, `evaluator_core.py`, `alternative_simulator.py`, `report_generator.py`. These supersede the April 28 versions (wrong architecture) and the May 24 draw.io specs. Build in **Claude Code, TikZ, Opus 4.8**, one standalone `.tex` per figure, compile, then `\input{}`.

Conventions for all three: left-to-right or top-to-bottom flow; rounded rectangles for processes, plain rectangles for data/state, cylinders for stores, dashed arrows for cross-graph handoffs. Keep labels terse; put model/temperature in small italic sublabels.

---

## Figure 5.1 — System Architecture (conversation graph + three knowledge tiers)
**Caption:** Synthetic client architecture: the scenario file is embedded once at session start; the conversation graph retrieves and injects knowledge per turn across three visibility tiers.

**Nodes / groups:**
- **Scenario file (JSON)** — cylinder, source. Contents listed inside or beside: persona core, character knowledge (CK), discovery items (DI), topic taxonomy.
- **Session-start indexing** — process box: `EmbeddingStore` embeds all CK + DI once (Voyage 3.5 Lite, `document` input type), normalises, builds **CK index** and **DI index** (two small cylinders).
- **Three knowledge tiers** — the visual centrepiece, a stacked group:
  - **Tier 1 — Always visible:** persona identity + behavior rules. *Always in system prompt.*
  - **Tier 2 — Retrievable (CK):** background facts. *Threshold τ_char = 0.45; injected transiently, this turn only.*
  - **Tier 3 — Earned (DI):** specific disclosures. *Threshold τ_disc = 0.55; injected and persists once revealed.*
- **Conversation graph (LangGraph)** — bounding box containing two nodes:
  - **Node 1 — Retrieval gate** (detailed in Fig 5.2).
  - **Node 2 — Client response** — Claude Sonnet 4.6. Assembles the system prompt from Tier 1 + injected CK + accumulated DI; generates the reply.
- **Graph state (typed)** — data box: `messages`, `revealed_items`, `retrieval_traces`. Show as persisting across turns.
- **Consultant** — actor, left side, in a loop with the graph (question in, response out).
- **Session log** — cylinder, downstream. Dashed arrow out to "Evaluation graph (Fig 6.1)" to show the handoff.

**Key edges:**
Scenario file → indexing → CK/DI indices. Consultant question → Node 1 (retrieval gate) → (injected CK + newly revealed DI) → Node 2 → response → Consultant. Tier 1 → Node 2 (always). Indices → Node 1. Node 1/Node 2 ↔ Graph state. Graph state → Session log ⇢ Evaluation graph (dashed).

---

## Figure 5.2 — Retrieval Gate Flow (Node 1 detail)
**Caption:** The retrieval gate: rule-based pre-filters eliminate non-questions before any embedding call; passing questions are matched against the two indices under separate thresholds.

**Flow (top to bottom):**
1. **Input:** consultant question.
2. **Pre-filter (rule-based Python, no API)** — decision group:
   - **Structural check** — has verb / question-word? If **no** → **Skip retrieval** (no injection). 
   - **Intent check** — catch-all / acknowledgment / filler? If **filler** → **Skip retrieval**.
3. **Embed query** — Voyage 3.5 Lite (`query` input type); one query vector per index.
4. **Conditional context retry** (diamond) — if question is short/referential **OR** first pass returns no match above either threshold → prepend the **single** preceding exchange, re-embed. Otherwise skip. *(Note: one exchange, conditional — not an unconditional two-exchange prepend.)*
5. **Two parallel matches:**
   - **CK index:** cosine ≥ τ_char (**0.45**), cap `max_char_items = 5` → **inject transiently** (this turn only).
   - **DI index:** cosine ≥ τ_disc (**0.55**), cap `max_disc_items = 3` → **reveal + persist** (append to `revealed_items`).
6. **Retrieval trace** — logged box: query text + scores of passing items (top-5 per index to debug log only).
7. **Output:** injected CK + newly revealed DI → to Node 2 (client response).

---

## Figure 6.1 — Evaluation Pipeline (evaluation graph)
**Caption:** The evaluation pipeline: a one-shot batch job over the completed transcript, classifying each turn, simulating better alternatives for flawed questions, and synthesising a coaching report.

**Flow (left to right, three nodes):**
- **Input:** full session transcript (from the session log).
- **Node 1 — Turn evaluator:** for each consultant turn,
  - `classify_turn()` — Claude Haiku 4.5, temp 0.0 → `turn_type`.
  - Route (branch): **question** → `evaluate_turn()` (Sonnet 4.6, temp 0.0) against the 7 mistake types → annotation `{turn_type, is_well_formed, mistakes}`; **solution_proposal** → noted, not penalised; **acknowledgment** → skipped; **unproductive_statement** → flagged.
- **Node 2 — Alternative simulator** (only where `is_well_formed = false`), three stages:
  - **Stage A — Generate:** improved question (Sonnet 4.6, temp 0.3); retry loop ≤ 3 with a pre-check evaluation.
  - **Stage B — Replay:** `G.invoke(T_prior + q_alt, R_prior)` — **reuses the conversation graph** (dashed arrow back to Fig 5.1) → simulated client response.
  - **Stage C — Verdict:** Sonnet 4.6 compares original vs alternative *response texts* → `improvement_verdict`. (Also computes `alt_revealed_items`, logged for analysis, not fed to the verdict.)
- **Node 3 — Report generator:**
  - **Compute statistics** — Python, deterministic (mistake counts, type distribution, well-formed rate).
  - **Synthesise report** — Sonnet 4.6, temp 0.3 → Continue / Stop / Start structure with per-turn feedback and verdicts.
- **Output:** structured coaching report.

**Note for the figure:** the dashed Stage-B arrow back into the conversation graph is the single most important detail — it visually carries the "graph-reuse counterfactual simulation" contribution. Make it prominent.

---

## Claude Code instruction (paste)

> Read `architecture.md`, `graph.py`, `eval_graph.py`, and `figure_specs_v1.md`. Build three standalone TikZ figures — Figure 5.1 (system architecture with three knowledge tiers), Figure 5.2 (retrieval gate flow), and Figure 6.1 (evaluation pipeline) — following the node/edge specs in `figure_specs_v1.md` and matching the thesis's existing fonts and colours. Compile each as its own `.tex` with pdflatex, fix any errors, and output a working snippet per figure ready to `\input{}`. Keep them legible in single-column width; do not invent components not in the specs or the code.
