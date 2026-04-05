"""
Scenario loader and knowledge retrieval.

Parses any scenario markdown file into three layers:

  character_text  — who the client IS: identity, personality, team, limitations.
                    Always in the system prompt. Contains NO factual data points.

  surface_items   — facts the client would share if asked about the relevant topic.
                    Gated: unlocked when the question is about that topic area.
                    Parsed from: What the Client Can Articulate.

  tacit_items     — facts the client knows but guards carefully.
                    Gated: unlocked only when asked specifically about the
                    current state or process for that topic.
                    Parsed from: What the Client Knows But Won't Volunteer.

Surface and tacit items are injected into the system prompt only after the consultant
earns them. The LLM cannot reveal what it cannot see. The number of items returned per
turn is an output of the relevance judgment — proportional to what the question earned.

Scenario markdown sections are classified as follows:
  CHARACTER sections (kept in character_text):
    - Instructions for Synthetic Client
    - What the Client Genuinely Doesn't Know
    - Team Members
    - Personality and Communication Style

  SURFACE sections (parsed into surface_items):
    - What the Client Can Articulate

  TACIT section (parsed into tacit_items):
    - What the Client Knows But Won't Volunteer [Tacit Knowledge]

  DROPPED sections (meta/training context, not useful to the LLM):
    - Scope Note
"""

import re
import json
from dataclasses import dataclass, field
from pathlib import Path
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage as LCHumanMessage


@dataclass
class ScenarioItem:
    id: str      # slug derived from content
    content: str # the fact — injected into system prompt when unlocked
    layer: str   # "surface" or "tacit" — controls unlock threshold
    topic: str = ""  # subtopic code e.g. "iam/provisioning", empty if untagged


@dataclass
class Scenario:
    title: str
    character_text: str                        # always in system prompt
    briefing: str = ""                         # consultant-facing briefing text
    maturity: str = ""                         # raw maturity level section body — passed to evaluator
    topic_taxonomy: dict = field(default_factory=dict)  # code -> display name
    surface_items: list[ScenarioItem] = field(default_factory=list)
    tacit_items: list[ScenarioItem] = field(default_factory=list)


# Which section headers belong to character_text (case-insensitive partial match).
_CHARACTER_SECTIONS = [
    "identity",
    "maturity level",
    "team members",
    "personality and communication style",
    "company overview",       # always visible context — client knows this freely as manager
    "character knowledge",    # generated scenario format: rich narrative from Phase 4
]

# Which section headers to drop entirely (meta/training context).
_DROPPED_SECTIONS = [
    "scope note",
    "what the client genuinely doesn't know",
    "technical reference",
    "scenario parameters",    # generated scenario metadata — not sent to client LLM
]

# Consultant-facing sections — captured separately, not sent to client LLM.
_BRIEFING_SECTIONS = ["consultant briefing"]
_TAXONOMY_SECTIONS = ["topics"]


def _slugify(text: str, max_words: int = 5) -> str:
    words = re.sub(r"[^a-z0-9\s]", "", text.lower()).split()
    return "_".join(words[:max_words])


def _parse_bullets(section_body: str) -> list[tuple[str, str]]:
    """
    Return (content, topic) pairs for each bullet in a section body.
    Strips inline [topic: X] and any legacy [TIER N] markers from content.
    """
    results = []
    for match in re.finditer(r"^\s*-\s+(.+)$", section_body, re.MULTILINE):
        raw = match.group(1).strip()
        topic_match = re.search(r"\[topic:\s*([^\]]+)\]", raw)
        topic = topic_match.group(1).strip() if topic_match else ""
        content = re.sub(r"\s*\[[^\]]*\]", "", raw).strip().strip('"')
        if content:
            results.append((content, topic))
    return results


def load_scenario(path: str | Path) -> Scenario:
    """
    Parse a scenario markdown file into a Scenario object.

    The markdown file encodes three distinct layers of knowledge:
    - character_text: always visible to the LLM — defines identity and personality,
      but contains zero factual data about the platform or situation.
    - surface_items: facts gated behind relevant questions. Parsed from bullet lists
      in "What the Client Can Articulate" sections.
    - tacit_items: facts the client guards more carefully. Parsed from the
      "What the Client Knows But Won't Volunteer" section.

    Consultant-facing metadata (briefing, topic taxonomy) is extracted separately
    and never sent to the client LLM.

    Each bullet item may carry an inline [topic: code] tag to associate it with
    the topic taxonomy. The tag is stripped from the item content before storage.
    Item IDs are generated as content slugs — stable across reloads as long as
    the content doesn't change.
    """
    text = Path(path).read_text()

    # Extract title.
    title_match = re.search(r"#\s+Scenario:\s*(.+)", text)
    title = title_match.group(1).strip() if title_match else Path(path).stem

    # Split into sections on ## headers.
    # Each element: (header_line, body_text)
    section_pattern = re.compile(r"(##[^\n]+)\n(.*?)(?=\n##|\Z)", re.DOTALL)
    sections = section_pattern.findall(text)

    character_parts: list[str] = []
    briefing_parts: list[str] = []
    maturity_text: str = ""
    topic_taxonomy: dict[str, str] = {}
    surface_items: list[ScenarioItem] = []
    tacit_items: list[ScenarioItem] = []
    seen_ids: dict[str, int] = {}

    def make_id(content: str) -> str:
        base = _slugify(content)
        count = seen_ids.get(base, 0)
        seen_ids[base] = count + 1
        return base if count == 0 else f"{base}_{count}"

    for header, body in sections:
        header_lower = header.lower()

        # --- Classify section ---
        is_character = any(k in header_lower for k in _CHARACTER_SECTIONS)
        is_dropped = any(k in header_lower for k in _DROPPED_SECTIONS)
        is_briefing = any(k in header_lower for k in _BRIEFING_SECTIONS)
        is_taxonomy = any(k in header_lower for k in _TAXONOMY_SECTIONS)
        is_tacit = "tacit knowledge" in header_lower
        # Everything else is a surface section.

        if is_dropped:
            continue

        if is_briefing:
            briefing_parts.append(body.strip())
            continue

        if is_taxonomy:
            for line in body.splitlines():
                line = line.strip()
                if not line:
                    continue
                if ":" in line:
                    code, _, display = line.partition(":")
                    topic_taxonomy[code.strip()] = display.strip()
            continue

        if is_character:
            character_parts.append(header.strip() + "\n" + body.strip())
            if "maturity level" in header_lower:
                maturity_text = body.strip()
            continue

        if is_tacit:
            for content, topic in _parse_bullets(body):
                tacit_items.append(ScenarioItem(
                    id=make_id(content),
                    content=content,
                    layer="tacit",
                    topic=topic,
                ))
            continue

        # Surface section — parse bullets.
        for content, topic in _parse_bullets(body):
            surface_items.append(ScenarioItem(
                id=make_id(content),
                content=content,
                layer="surface",
                topic=topic,
            ))

    character_text = "\n\n".join(character_parts)
    return Scenario(
        title=title,
        character_text=character_text,
        briefing="\n".join(briefing_parts),
        maturity=maturity_text,
        topic_taxonomy=topic_taxonomy,
        surface_items=surface_items,
        tacit_items=tacit_items,
    )


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

_RETRIEVAL_PROMPT = """A consultant is interviewing a client about their current setup and situation.

Recent conversation (for context):
{recent_context}

The consultant's latest input: "{question}"

## YOUR TASK

First decide if this is a genuine question. Then, only if it is, find matching items.

### Step 1: Structural check (do this first)
Does the input contain at least one verb or explicit question word?
Question words: what, how, who, where, when, why, which, is, are, was, were,
do, does, did, can, could, will, would, should, have, has, had.

If NO verb or question word is present → the input is a bare noun phrase or topic name.
Set "is_genuine": false immediately. Do not proceed to step 2.

Examples that fail the structural check (no verb, no question word):
"object ownership?", "SCIM?", "clusters?", "self service?", "hub and spoke"

### Step 2: Intent check (only if step 1 passes)
Does the question ask specifically about THIS client's situation in a way that requires
knowing their particular reality? Use the conversation context to resolve references.

It is NOT genuine if:
- "what about X?" or "how about X?" — naming a topic is not asking about it
- It is a reaction or statement: "okay", "really?", "that's interesting"
- It is a catch-all: "anything else?", "tell me more", "share more"
- It asks how a technology works in general, not about this client specifically

### Step 3: Relevance matching (only if steps 1 and 2 pass)
For each candidate item, ask: does this question SPECIFICALLY ask about what THIS item
describes? Or is the item merely about the same general topic area as the question?

**Direct match — return the item:** The question specifically targets what this item
describes. The question could not be meaningfully answered without this item.
Ask yourself: if this item did not exist, would the question go unanswered?
If yes, it is a direct match.

**Topical association — do NOT return the item:** The item is about the same general
area as the question, but the question doesn't specifically address this item's content.
A question about a broad topic area is NOT a direct match for every item in that area.
Only the item (if any) that most centrally answers the question qualifies.

If the question is broad, return at most the one item that most centrally characterises
that area — or none if even that is a stretch. Broad questions earn little.
If the question is specific and directly targets something, return the items it targets.

Order matched_ids by decreasing relevance — most directly targeted item first.

SURFACE items:
{surface_items}

TACIT items:
{tacit_items}

### Output format
Output ONLY a JSON object. No reasoning, no explanation, no other text.
If "is_genuine" is false, "matched_ids" MUST be [].
matched_ids must be ordered: most directly targeted item first.

{{"is_genuine": true/false, "matched_ids": ["id", ...] or []}}
"""


def retrieve_relevant_knowledge(
    question: str,
    surface_items: list[ScenarioItem],
    tacit_items: list[ScenarioItem],
    already_revealed_ids: list[str],
    recent_context: str = "",
) -> list[ScenarioItem]:
    """
    Decide which knowledge items (if any) the consultant's question has earned.

    The retrieval LLM (Claude Sonnet 4.6) applies a three-step filter:
    1. Structural check — does the input contain a verb or question word?
       Bare noun phrases like "SCIM?" or "clusters?" fail immediately.
    2. Intent check — does it ask about this specific client's situation?
       Generic technology questions or catch-alls ("tell me more") are rejected.
    3. Relevance matching — for each candidate item, the test is:
       "if this item did not exist, would the question go unanswered?"
       This is direct specificity, not topical association. A broad question
       about "access management" does NOT earn every item about access — at most
       the one item that most centrally characterises that area.

    Tacit items have an extra pre-filter: a tacit item only becomes a candidate
    once a surface item in the same subtopic has already been revealed. This
    models how a client's guarded knowledge surfaces: first they describe the
    situation, then specific probing reveals the underlying problem.

    The [:3] cap at the end is a guardrail against prompt misjudgement — the
    prompt does the calibration work; the cap is a last resort. If it binds
    regularly, the prompt needs tuning, not the cap.

    Returns a list of newly unlocked ScenarioItem objects, ordered by relevance
    (most directly targeted item first), or an empty list if the question is
    not genuine or earns nothing.
    """
    unrevealed_surface = [t for t in surface_items if t.id not in already_revealed_ids]
    unrevealed_tacit = [t for t in tacit_items if t.id not in already_revealed_ids]

    # Tacit items only become candidates once a surface item in the same subtopic
    # has already been revealed. The LLM can't match what it can't see.
    revealed_subtopics = {
        t.topic for t in surface_items
        if t.id in already_revealed_ids and t.topic
    }
    eligible_tacit = [t for t in unrevealed_tacit if t.topic in revealed_subtopics]

    if not unrevealed_surface and not eligible_tacit:
        return []

    surface_text = "\n".join(
        f'- id: "{t.id}", fact: "{t.content}"' for t in unrevealed_surface
    ) or "(none remaining)"

    tacit_text = "\n".join(
        f'- id: "{t.id}", fact: "{t.content}"' for t in eligible_tacit
    ) or "(none remaining)"

    llm = ChatAnthropic(model="claude-sonnet-4-6", temperature=0.0)
    prompt = _RETRIEVAL_PROMPT.format(
        question=question,
        recent_context=recent_context or "(start of conversation)",
        surface_items=surface_text,
        tacit_items=tacit_text,
    )
    response = llm.invoke([LCHumanMessage(content=prompt)])

    try:
        raw = response.content.strip()
        # Strip markdown code fences if present.
        if "```" in raw:
            raw = re.sub(r"```[a-z]*\n?", "", raw).replace("```", "").strip()
        # Fallback: extract the JSON object from anywhere in the response.
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if json_match:
            raw = json_match.group(0)
        parsed = json.loads(raw)
        if not parsed.get("is_genuine", False):
            return []
        matched_ids = parsed.get("matched_ids", [])
    except (json.JSONDecodeError, AttributeError):
        matched_ids = []

    # Return items the LLM judged relevant. The prompt does the calibration work —
    # direct specificity, not topical association. The [:3] cap is a safety guardrail
    # for when the LLM misjudges despite good instructions. If the cap is consistently
    # binding, it means the prompt needs tuning, not the cap.
    all_unrevealed = {t.id: t for t in unrevealed_surface + unrevealed_tacit}
    return [all_unrevealed[id] for id in matched_ids if id in all_unrevealed][:3]
