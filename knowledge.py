"""
Scenario loader and knowledge retrieval.

Parses any scenario markdown file into two layers:
  surface_text  — everything the client LLM always has in context
  tacit_items   — facts hidden until the consultant asks specifically enough

Scenario markdown format expected:
  # Scenario: <title>
  ## <any sections>
  ## What the Client Knows But Won't Volunteer [Tacit Knowledge]
  - <fact> [TIER 1]
  - <fact> [TIER 2]
  ## <more sections>

Everything except the tacit knowledge bullets becomes surface_text.
The tacit section is parsed item-by-item, tier labels preserved.
"""

import re
import json
from dataclasses import dataclass, field
from pathlib import Path
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage as LCHumanMessage


@dataclass
class ScenarioItem:
    id: str        # slug derived from content, used to track revealed state
    content: str   # the fact itself — injected into system prompt when unlocked
    tier: str      # "TIER 1", "TIER 2", or "TIER 3" — used by evaluator later


@dataclass
class Scenario:
    title: str
    surface_text: str          # always in system prompt
    tacit_items: list[ScenarioItem] = field(default_factory=list)


def _slugify(text: str, max_words: int = 5) -> str:
    """Derive a stable ID from the first few words of a string."""
    words = re.sub(r"[^a-z0-9\s]", "", text.lower()).split()
    return "_".join(words[:max_words])


def load_scenario(path: str | Path) -> Scenario:
    """
    Parse a scenario markdown file into a Scenario object.
    The tacit knowledge section is extracted and removed from surface_text.
    Everything else (character instructions, personality, team, platform context)
    stays in surface_text so the LLM knows who it is.
    """
    text = Path(path).read_text()

    # Extract title from the first H1 heading.
    title_match = re.search(r"^#\s+Scenario:\s*(.+)$", text, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else Path(path).stem

    # Find the tacit knowledge section.
    # Matches: "## What the Client Knows But Won't Volunteer [Tacit Knowledge]"
    # or any H2 containing "Tacit Knowledge"
    tacit_section_pattern = re.compile(
        r"(##[^\n]*Tacit Knowledge[^\n]*\n)(.*?)(?=\n##|\Z)",
        re.DOTALL | re.IGNORECASE,
    )
    tacit_match = tacit_section_pattern.search(text)

    tacit_items: list[ScenarioItem] = []

    if tacit_match:
        tacit_body = tacit_match.group(2)

        # Parse each bullet: "- <content> [TIER N]" or "- <content> [TIER N — note]"
        bullet_pattern = re.compile(
            r"^\s*-\s+(.+?)\s+\[(TIER\s+[123])[^\]]*\]",
            re.MULTILINE,
        )
        seen_ids: dict[str, int] = {}
        for match in bullet_pattern.finditer(tacit_body):
            content = match.group(1).strip()
            tier = match.group(2).strip()
            base_id = _slugify(content)
            # Deduplicate IDs if two items produce the same slug.
            count = seen_ids.get(base_id, 0)
            seen_ids[base_id] = count + 1
            item_id = base_id if count == 0 else f"{base_id}_{count}"
            tacit_items.append(ScenarioItem(id=item_id, content=content, tier=tier))

        # Remove tacit section from surface text — the LLM should not see these facts.
        surface_text = tacit_section_pattern.sub("", text).strip()
    else:
        surface_text = text.strip()

    return Scenario(title=title, surface_text=surface_text, tacit_items=tacit_items)


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

_RETRIEVAL_PROMPT = """A consultant is interviewing a client about their Databricks setup.

The consultant just said: "{question}"

Below are private facts the client knows but has not revealed yet.
Identify which facts (if any) this question is specifically asking about.

Rules:
- Only match a fact if the question directly and specifically asks about that topic.
- Broad or catch-all questions ("tell me about your setup", "what problems do you have",
  "anything else?", "what should I know?") must match NOTHING.
- A general question about a topic area should unlock at most one fact — the most
  directly relevant one. Do not unlock multiple related facts at once.
- Return an empty list for vague or exploratory questions.

Facts:
{items}

Return a JSON object: {{"matched_ids": ["id1", "id2"]}} or {{"matched_ids": []}}
"""


def retrieve_relevant_knowledge(
    question: str,
    tacit_items: list[ScenarioItem],
    already_revealed_ids: list[str],
) -> list[ScenarioItem]:
    """
    Returns newly unlocked tacit items for this turn.
    Uses gpt-4o to classify which items the question specifically asks about.
    """
    unrevealed = [t for t in tacit_items if t.id not in already_revealed_ids]
    if not unrevealed:
        return []

    items_text = "\n".join(
        f'- id: "{t.id}", fact: "{t.content}"' for t in unrevealed
    )

    llm = ChatOpenAI(model="gpt-4o", temperature=0.0)
    prompt = _RETRIEVAL_PROMPT.format(question=question, items=items_text)
    response = llm.invoke([LCHumanMessage(content=prompt)])

    try:
        parsed = json.loads(response.content)
        matched_ids = parsed.get("matched_ids", [])
    except (json.JSONDecodeError, AttributeError):
        matched_ids = []

    return [t for t in unrevealed if t.id in matched_ids]
