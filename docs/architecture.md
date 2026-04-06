# System Design Rationale

This document explains the architectural choices from first principles — why this approach was
taken, what alternatives exist, and what problem each design decision solves. Written for
thesis documentation and for understanding the system as an AI engineering exercise.

## Why LLMs for synthetic clients, not rule-based scripting

A scripted client (decision trees, canned responses) can only respond to anticipated consultant
moves. Real clients don't behave that way — they interpret questions, react to framing, and
volunteer adjacent information contextually. LLMs bring natural language understanding and
the ability to respond to unanticipated phrasing without explicit rule coverage.

The risk is the opposite problem: LLMs are too cooperative. Without constraints, an LLM client
answers every question fully and volunteers everything it knows, which eliminates the training
value entirely. The entire architecture — knowledge gating, behavior rules, the retrieval gate —
exists to solve this problem while keeping the conversational naturalness.

## Why knowledge gating (not prompt-based suppression)

The naive approach is to give the client LLM all the scenario knowledge and instruct it via
rules to only reveal information when asked about it. This doesn't work reliably. LLMs leak
context they've been told to suppress, especially under indirect questioning. The information
is in the prompt — the model has it, and sufficiently probing questions will surface it.

Knowledge gating solves this at the architectural level: the client LLM cannot reveal what
it cannot see. Facts are held outside the system prompt and injected only after the retrieval
gate confirms the consultant earned them. Suppression rules cannot reliably beat visibility.
Not giving the information to the model is the only robust approach.

## Why a separate retrieval LLM, not a rule-based matching function

Matching a consultant question to a knowledge item is fundamentally a semantic task, not a
keyword task. "How is user access managed?" and "who controls who can log into the platform?"
are the same question expressed differently. A rule-based matcher (keyword overlap, embedding
similarity threshold) would either over-match (broad questions unlock everything) or under-match
(novel phrasing misses items it should earn).

Using an LLM for retrieval enables the "direct specificity" criterion: the retrieval model can
reason about whether a question specifically targets an item, not just whether it's topically
adjacent. This is a semantic judgment that requires understanding both the question's intent and
the item's content — well-suited to LLMs.

Claude Sonnet 4.6 is used for retrieval (temp 0.0). It runs on every consultant turn and needs
analytical precision, not creative generation. Haiku would be too weak for the semantic directness
judgment; Opus would be over-specified and slow. Sonnet at temp 0.0 gives deterministic, accurate
matching at acceptable latency.

## Why LangGraph, not a plain Python loop

A plain loop (`while True: input → LLM → print`) would work for the conversation. The reasons
to use LangGraph are:

1. **State management**: LangGraph's typed state with reducers makes it explicit what persists
   across turns (`messages`, `revealed_items`) and how updates are merged. A plain dict would
   work but is easier to corrupt by accident.

2. **Separation of concerns**: the retrieval gate and client response are genuinely different
   operations. Putting them in separate nodes means either can be replaced, tested, or logged
   independently.

3. **Reuse for evaluation**: the evaluation pipeline reuses the same conversation graph for
   Stage B simulation (replaying turns with alternative questions). LangGraph's `invoke(state)`
   interface makes this clean — you can seed the graph with a partial state and replay from any
   point.

4. **Extensibility**: adding a new node (e.g. a post-processing node to flag jargon) doesn't
   require restructuring the loop — just insert a node into the graph definition.

## Why two separate graphs (conversation vs evaluation)

The conversation graph is stateless between sessions — it holds only the current turn's context.
The evaluation pipeline is a one-shot batch job that processes the entire transcript after the
interview ends. These have fundamentally different lifecycles.

Mixing them would require the conversation graph to carry evaluation state throughout the
interview (wasteful) or trigger evaluation inline after every turn (too slow, changes the
conversational experience). Keeping them separate means: the conversation graph is fast and
lightweight; the evaluation graph is thorough and runs once.

## Why classify before evaluate (two-step evaluation)

The mistake taxonomy (14 types) is designed for questions. Applying it to non-question turns
— acknowledgments, solution proposals, statements — produces false positives. A consultant
saying "got it, that makes sense" would be flagged for vagueness or not asking about a specific
topic, even though it's not a question.

`classify_turn()` runs first (GPT-OSS low reasoning — simple routing, not semantic) and
routes each turn to the appropriate evaluation logic. Questions go to `evaluate_turn()` against
the 14 types. Solution proposals are noted but not penalised for mistakes. Acknowledgments are
skipped. Unproductive statements are flagged without applying the question taxonomy.

This keeps the mistake taxonomy valid for its intended purpose while correctly handling the
full range of consultant behaviors.

## Why the alternative simulator (counterfactual learning)

Telling a consultant "this question was vague" is less useful than showing them what a better
question would have produced. The alternative simulator creates a concrete counterfactual:
here is the improved question; here is what the client would have said in response.

This is a stronger pedagogical signal than abstract feedback because it answers both "what
should I have asked?" and "why would that have been better?" simultaneously. The improvement
verdict then compares both pairs (original question + actual response vs alternative + simulated
response) in one sentence — a format directly usable in a feedback report.

The three-stage design (generate → verify → simulate) ensures the alternative is genuinely
better before running the expensive simulation: Stage A generates with a retry loop,
each failed attempt carrying its mistake back to the generator. Stage B only runs once the
alternative passes the quality check.

## Why temperature differs by task

Temperature controls creativity vs. consistency:
- **Client response (temp 0.7)**: high temperature produces varied, natural responses.
  The same question asked twice should not produce identical wording — real clients don't.
- **Evaluation (temp 0.0)**: classification and mistake detection should be deterministic.
  The same question evaluated twice should get the same result.
- **Alternative generation (temp 0.3)**: low-creative — wants a better version of the
  original question, not a random rewrite. Enough variation to escape the original phrasing,
  but anchored to the consultant's intent.
- **Report generation (temp 0.3)**: structured synthesis of evidence — moderate creativity
  for readable prose, low enough to stay grounded in the data.

## Why multi-persona scenario files

The original design had one scenario file per persona. This means a consultant always practices
with the same person — they can't experience how the same engagement situation looks through
the lens of a manager vs. a technical architect. The multi-persona format puts both personas in
a single file with a shared briefing and taxonomy, letting the consultant choose who to interview
before the session starts.

The shared header matters architecturally: the briefing and topic taxonomy are the same for
both personas — they describe the engagement, not the person. Per-persona blocks differ only in
character_text (how they speak, what they know, their role) and discovery items (what they've
observed from their particular vantage point). The scenario generator constructs these separately
per persona, then merges them at Phase 6 combine.

## Why a pipeline for scenario construction, not manual authoring

The original scenario (`waste_management_client.md`) was hand-written. This doesn't scale.
Writing a realistic persona requires: extracting facts from engagement notes, deciding what the
persona knows vs what must be discovered, rewriting technical findings into client-language items,
generating a coherent narrative that doesn't create inference paths, validating that narrative,
and assembling the final file. That's 6–8 sequential judgement-heavy tasks — exactly the kind of
work LLMs can do well when given structured prompts with the right constraints.

The pipeline is human-in-the-loop by design. Each phase produces output for human review before
the next phase runs. The LLM does the lifting; the domain expert validates realism, catches
classification errors, and enriches generated content. This is framed in the thesis as
"AI-assisted scenario construction."

The phase separation also enforces design principles: Phase 3's classification prompt encodes
the aha-test and natural-disclosure test; Phase 5's validation prompt encodes the inference-path
constraint. Running all phases in a single call would mix these concerns and make the output
harder to inspect and correct.
