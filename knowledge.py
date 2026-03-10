"""
Scenario loader and knowledge retrieval.

Parses any scenario markdown file into three layers:

  character_text  — who the client IS: identity, personality, team, limitations.
                    Always in the system prompt. Contains NO factual data points.

  surface_items   — facts the client would share if asked about the relevant topic.
                    Gated: unlocked when the question is about that topic area.
                    Parsed from: Company Overview, Current Data Platform,
                    What the Client Can Articulate.

  tacit_items     — facts the client knows but guards carefully.
                    Gated: unlocked only when asked specifically about the
                    current state or process for that topic.
                    Parsed from: What the Client Knows But Won't Volunteer.

Both surface and tacit items are injected into the system prompt only after
the consultant earns them. The LLM cannot reveal what it cannot see.

Scenario markdown sections are classified as follows:
  CHARACTER sections (kept in character_text):
    - Instructions for Synthetic Client
    - What the Client Genuinely Doesn't Know
    - Team Members
    - Personality and Communication Style

  SURFACE sections (parsed into surface_items):
    - Company Overview
    - Current Data Platform
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
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage as LCHumanMessage


@dataclass
class ScenarioItem:
    id: str      # slug derived from content
    content: str # the fact — injected into system prompt when unlocked
    tier: str    # "TIER 1", "TIER 2", "TIER 3" — used by evaluator
    layer: str   # "surface" or "tacit" — controls unlock threshold


@dataclass
class Scenario:
    title: str
    character_text: str                        # always in system prompt
    surface_items: list[ScenarioItem] = field(default_factory=list)
    tacit_items: list[ScenarioItem] = field(default_factory=list)


# Which section headers belong to character_text (case-insensitive partial match).
_CHARACTER_SECTIONS = [
    "instructions for synthetic client",
    "team members",
    "personality and communication style",
]

# Which section headers to drop entirely (meta/training context).
_DROPPED_SECTIONS = [
    "scope note",
    "what the client genuinely doesn't know",
]


def _slugify(text: str, max_words: int = 5) -> str:
    words = re.sub(r"[^a-z0-9\s]", "", text.lower()).split()
    return "_".join(words[:max_words])


def _tier_from_header(header: str) -> str:
    """Extract tier from a section header like '## Company Overview [TIER 3 — ...]'."""
    m = re.search(r"TIER\s+([123])", header)
    return f"TIER {m.group(1)}" if m else "TIER 3"


def _parse_bullets(section_body: str, default_tier: str) -> list[tuple[str, str]]:
    """
    Return (content, tier) pairs for each bullet in a section body.
    Strips inline [TIER N] markers from content.
    """
    results = []
    for match in re.finditer(r"^\s*-\s+(.+)$", section_body, re.MULTILINE):
        raw = match.group(1).strip()
        tier_match = re.search(r"\[(TIER\s+[123])[^\]]*\]", raw)
        tier = tier_match.group(1).strip() if tier_match else default_tier
        content = re.sub(r"\s*\[[^\]]*\]", "", raw).strip().strip('"')
        if content:
            results.append((content, tier))
    return results


def load_scenario(path: str | Path) -> Scenario:
    """
    Parse a scenario markdown file into a Scenario object.
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
        is_tacit = "tacit knowledge" in header_lower
        # Everything else is a surface section.

        if is_dropped:
            continue

        if is_character:
            character_parts.append(header.strip() + "\n" + body.strip())
            continue

        if is_tacit:
            default_tier = _tier_from_header(header)
            for content, tier in _parse_bullets(body, default_tier):
                tacit_items.append(ScenarioItem(
                    id=make_id(content),
                    content=content,
                    tier=tier,
                    layer="tacit",
                ))
            continue

        # Surface section — parse bullets.
        default_tier = _tier_from_header(header)
        for content, tier in _parse_bullets(body, default_tier):
            surface_items.append(ScenarioItem(
                id=make_id(content),
                content=content,
                tier=tier,
                layer="surface",
            ))

    character_text = "\n\n".join(character_parts)
    return Scenario(
        title=title,
        character_text=character_text,
        surface_items=surface_items,
        tacit_items=tacit_items,
    )


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

_RETRIEVAL_PROMPT = """A consultant is interviewing a client about their Databricks setup.

The consultant just said: "{question}"

Your job is to judge whether the consultant's input is a genuine, specific question
about this client's situation — or just a topic reference.

DISQUALIFY and return empty for any of these:
- A bare topic name, with or without a question mark: "SCIM", "hub and spoke?", "key vaults"
- "What about X?" — this is a topic reference, not a question. Disqualify regardless
  of how specific X is. "What about self-service analytics?" is the same as saying
  "self-service analytics" — it names a topic without asking anything about it.
- Any input where adding "?" to a topic name would produce the same meaning
- Catch-all prompts: "anything else?", "what else?", "go on"
- Questions about how a technology works in general (not about this client's setup)

A genuine question has real structure and intent — it asks HOW, WHO, WHETHER, or WHAT
specifically about this client's circumstances. If a question could be answered the same
way for any client, it is generic. A question earns information only when it probes
this client's particular reality in a way that requires knowing their specific situation.

If disqualified, return empty.

If it is pinpointed, find the single best matching item across both pools below.
Match at most ONE item — the most directly relevant one.

SURFACE items:
{surface_items}

TACIT items:
{tacit_items}

Return JSON: {{"matched_ids": ["id"]}} or {{"matched_ids": []}}
"""


def retrieve_relevant_knowledge(
    question: str,
    surface_items: list[ScenarioItem],
    tacit_items: list[ScenarioItem],
    already_revealed_ids: list[str],
) -> list[ScenarioItem]:
    """
    Returns newly unlocked items (surface or tacit) for this turn.
    """
    unrevealed_surface = [t for t in surface_items if t.id not in already_revealed_ids]
    unrevealed_tacit = [t for t in tacit_items if t.id not in already_revealed_ids]

    if not unrevealed_surface and not unrevealed_tacit:
        return []

    surface_text = "\n".join(
        f'- id: "{t.id}", fact: "{t.content}"' for t in unrevealed_surface
    ) or "(none remaining)"

    tacit_text = "\n".join(
        f'- id: "{t.id}", fact: "{t.content}"' for t in unrevealed_tacit
    ) or "(none remaining)"

    llm = ChatOpenAI(model="gpt-4o", temperature=0.0)
    prompt = _RETRIEVAL_PROMPT.format(
        question=question,
        surface_items=surface_text,
        tacit_items=tacit_text,
    )
    response = llm.invoke([LCHumanMessage(content=prompt)])

    try:
        # Strip markdown code fences if present (```json ... ```)
        raw = response.content.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
        parsed = json.loads(raw)
        matched_ids = parsed.get("matched_ids", [])
    except (json.JSONDecodeError, AttributeError):
        matched_ids = []

    all_unrevealed = unrevealed_surface + unrevealed_tacit
    return [t for t in all_unrevealed if t.id in matched_ids]
