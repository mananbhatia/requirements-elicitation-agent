"""
Scenario loader and knowledge retrieval.

Embedding-based two-tier retrieval replaces the LLM relevance gate.
Pre-filters (structural/intent) are rule-based Python — no LLM call per turn.

Two knowledge tiers:
  character_knowledge  — retrievable narrative paragraphs parsed from the
                         Character Knowledge section. Retrieved fresh each turn
                         as topical context. NOT persisted in state.
  discovery_items      — specific facts the client progressively discloses.
                         Persist once revealed; always re-injected on later turns.

File format support:
  Legacy (## sections)        — CK stays in character_text; surface+tacit → discovery_items
  Multi-persona (## Persona:) — CK parsed into retrievable chunks; Discovery Items → discovery_items

Usage:
    scenario = load_scenario(path, persona="Danny")
    char_index, disc_index = build_retrieval_index(scenario)
    char_pars, new_disc, trace = retrieve_relevant_knowledge(
        question, char_index, disc_index, scenario, already_revealed_ids,
        recent_context=recent_context,
    )
"""

import os
import re
import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ScenarioItem:
    id: str       # e.g. "CK-01", "DI-04" — stable across turns
    content: str  # text injected into system prompt when available
    topic: str = ""   # subtopic code e.g. "iam/provisioning", empty if untagged
    layer: str = ""   # retained for parsing; not used in retrieval or state payloads


@dataclass
class Scenario:
    title: str
    character_text: str                         # always in system prompt (identity, personality, team)
    character_knowledge: list[ScenarioItem] = field(default_factory=list)  # retrievable CK chunks
    discovery_items: list[ScenarioItem] = field(default_factory=list)      # gated disclosures
    briefing: str = ""
    maturity: str = ""
    topic_taxonomy: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# EmbeddingStore
# ---------------------------------------------------------------------------

class EmbeddingStore:
    """
    Wraps Voyage embeddings for a fixed set of (id, text) items.

    Embeds all items once at construction time (document input_type).
    Normalizes embeddings at init so query is a fast matrix-vector dot product.

    For testing without API access, use EmbeddingStore._from_arrays().
    """

    def __init__(self, items: list[tuple[str, str]]):
        """
        Args:
            items: list of (id, text) pairs to embed and index.

        Raises:
            ValueError: if VOYAGE_API_KEY is missing or the embed call fails.
        """
        self._ids: list[str] = []
        self._normalized: np.ndarray = np.empty((0, 0), dtype=np.float32)

        if not items:
            return  # empty store — query always returns []

        api_key = os.environ.get("VOYAGE_API_KEY")
        if not api_key:
            raise ValueError(
                "VOYAGE_API_KEY environment variable is not set. "
                "Set it to your Voyage AI API key before using EmbeddingStore."
            )

        model = os.environ.get("EMBEDDING_MODEL", "voyage-3.5-lite")

        try:
            import voyageai
            client = voyageai.Client(api_key=api_key)
        except ImportError:
            raise ImportError(
                "voyageai package not installed. Run: pip install voyageai"
            )

        self._ids = [id_ for id_, _ in items]
        texts = [text for _, text in items]
        self._model = model
        self._client = client

        try:
            result = client.embed(texts, model=model, input_type="document")
        except Exception as e:
            raise ValueError(
                f"Voyage embedding call failed during EmbeddingStore construction: {e}"
            ) from e

        raw = np.array(result.embeddings, dtype=np.float32)
        # Normalize rows so cosine similarity = dot product
        norms = np.linalg.norm(raw, axis=1, keepdims=True)
        self._normalized = raw / (norms + 1e-9)

    @classmethod
    def _from_arrays(cls, ids: list[str], embeddings: np.ndarray) -> "EmbeddingStore":
        """
        Test helper: construct a store from pre-computed embeddings without an API call.
        embeddings must be shape (len(ids), dim).
        """
        store = object.__new__(cls)
        store._ids = list(ids)
        store._model = "test"
        store._client = None
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        store._normalized = embeddings.astype(np.float32) / (norms + 1e-9)
        return store

    def _embed_query(self, text: str) -> np.ndarray:
        """Embed a single query string."""
        try:
            result = self._client.embed([text], model=self._model, input_type="query")
        except Exception as e:
            raise ValueError(f"Voyage query embedding failed: {e}") from e
        return np.array(result.embeddings[0], dtype=np.float32)

    def _query_vec(
        self,
        qvec: np.ndarray,
        top_k: int,
        threshold: float,
    ) -> list[tuple[str, float]]:
        """
        Core similarity computation. Separated from query() so tests can inject
        a pre-computed vector directly without touching the API.

        Returns list of (id, score) sorted descending by similarity, filtered
        to scores >= threshold and capped at top_k.
        """
        if len(self._ids) == 0:
            return []
        norm = np.linalg.norm(qvec)
        qvec_n = qvec / (norm + 1e-9)
        sims = self._normalized @ qvec_n  # shape (n_items,)
        ranked = sorted(
            zip(self._ids, sims.tolist()),
            key=lambda x: x[1],
            reverse=True,
        )
        return [(id_, score) for id_, score in ranked[:top_k] if score >= threshold]

    def query(
        self,
        text: str,
        top_k: int,
        threshold: float,
    ) -> list[tuple[str, float]]:
        """
        Embed text (query input_type) and return (id, score) pairs above threshold.
        """
        qvec = self._embed_query(text)
        return self._query_vec(qvec, top_k, threshold)


# ---------------------------------------------------------------------------
# Pre-filters (rule-based, no LLM)
# ---------------------------------------------------------------------------

_QUESTION_WORDS = frozenset({
    "what", "how", "who", "where", "when", "why", "which",
    "is", "are", "was", "were", "do", "does", "did",
    "can", "could", "will", "would", "should",
    "have", "has", "had",
})

_ACKNOWLEDGMENT_RE = re.compile(
    r"^(okay|ok|right|yes|no|sure|got it|i see|i understand|understood|"
    r"thank you|thanks|interesting|noted|great|good|alright|fine|"
    r"really|that('?s| is) (interesting|helpful|useful|good)|"
    r"tell me more|share more|anything else|go on|elaborate|"
    r"continue|can you (tell me )?more)\??\s*$",
    re.IGNORECASE,
)

# "what about X?" or "how about X?" with <=2 words after "about" — topic reference, not a question.
_WHAT_ABOUT_RE = re.compile(
    r"^(what|how)\s+about\s+(\w+\s*){1,2}\??\s*$",
    re.IGNORECASE,
)


def structural_check(text: str) -> bool:
    """
    Return True if the input contains at least one verb or question word.

    Bare noun phrases ("SCIM?", "clusters?") fail immediately.
    Permissive by design — false negatives worse than false positives.
    """
    words = set(re.sub(r"[^a-z\s]", "", text.lower()).split())
    return bool(words & _QUESTION_WORDS)


def intent_check(text: str) -> bool:
    """
    Return True if the input is not an obvious non-question.

    Blocks acknowledgments, reactions, catch-alls, and topic references.
    Only obvious patterns are blocked — does not attempt semantic analysis.
    """
    stripped = text.strip()
    if _ACKNOWLEDGMENT_RE.match(stripped):
        return False
    if _WHAT_ABOUT_RE.match(stripped):
        return False
    words = stripped.rstrip("?").split()
    if len(words) <= 1:
        return False
    return True


# Auxiliary verb or wh-word at start + subject-position pronoun — no local antecedent.
# Pattern A: "Is it locked down?", "Are they syncing?", "Does it apply?"
# Pattern B: "How is that handled?", "What does it mean?", "When was it introduced?"
# Does NOT match "Is your environment...?" or "How are users added — is that done...?"
_PRONOUNS = r"(it|that|this|those|these|they|them)"
_AUX = r"(is|are|was|were|do|does|did|can|could|will|would|should|has|have|had)"
_SUBJECT_PRONOUN_RE = re.compile(
    rf"^({_AUX}\s+{_PRONOUNS}|"
    rf"(how|what|when|where|why|which)\s+{_AUX}\s+{_PRONOUNS})\b",
    re.IGNORECASE,
)


def needs_context(question: str) -> bool:
    """
    Return True only when the question is clearly referential or incomplete on its own.

    Default is question-only retrieval. Context-aware is the exception.

    Triggers on:
      - Auxiliary/wh-word + subject-position pronoun at sentence start (no local antecedent)
      - Explicit follow-up openers: "And ...", "But ...", "What about ..."
      - Questions of 4 words or fewer

    Does NOT trigger on a pronoun mid-sentence with a local antecedent:
      "How are users added — is that done automatically?" → starts with "How are users"
      "Is your environment accessible, or is it on a private network?" → starts with "Is your"
    """
    text = question.strip()
    if _SUBJECT_PRONOUN_RE.match(text):
        return True
    if re.match(r"^(and|but|so|also|what about|how about)\b", text, re.IGNORECASE):
        return True
    if len(text.split()) <= 4:
        return True
    return False


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _slugify(text: str, max_words: int = 5) -> str:
    words = re.sub(r"[^a-z0-9\s]", "", text.lower()).split()
    return "_".join(words[:max_words])


def _strip_topic_tags(text: str) -> tuple[str, str]:
    """Return (content_without_tags, first_topic_code)."""
    topic_match = re.search(r"\[topic:\s*([^\]]+)\]", text)
    topic = topic_match.group(1).strip() if topic_match else ""
    content = re.sub(r"\s*\[[^\]]*\]", "", text).strip().strip('"')
    return content, topic


def _parse_bullets(section_body: str) -> list[tuple[str, str]]:
    results = []
    for match in re.finditer(r"^\s*-\s+(.+)$", section_body, re.MULTILINE):
        content, topic = _strip_topic_tags(match.group(1).strip())
        if content:
            results.append((content, topic))
    return results


def _parse_discovery_item(line: str) -> tuple[str, str, str] | None:
    """Parse '- [DI-01] [topic: X] content' → (id, content, topic) or None."""
    match = re.match(r"^\s*-\s+\[([^\]]+)\]\s+(.*)", line)
    if not match:
        return None
    item_id = match.group(1).strip()
    content, topic = _strip_topic_tags(match.group(2).strip())
    return (item_id, content, topic) if content else None


def _parse_taxonomy_body(body: str) -> dict[str, str]:
    taxonomy = {}
    for line in body.splitlines():
        line = line.strip()
        if line and ":" in line:
            code, _, display = line.partition(":")
            taxonomy[code.strip()] = display.strip()
    return taxonomy


# ---------------------------------------------------------------------------
# Section classification constants
# ---------------------------------------------------------------------------

_DROPPED_SECTIONS = [
    "scope note",
    "what the client genuinely doesn't know",
    "technical reference",
    "scenario parameters",
]
_BRIEFING_SECTIONS = ["consultant briefing"]
_TAXONOMY_SECTIONS = ["topics"]
_LEGACY_CHARACTER_SECTIONS = [
    "identity",
    "maturity level",
    "team members",
    "personality and communication style",
    "company overview",
    "character knowledge",  # legacy: stays in character_text (no per-paragraph retrieval)
]


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_scenario(path: str | Path, persona: str | None = None) -> Scenario:
    """
    Parse a scenario markdown file into a Scenario object.

    Supports two file formats:

    Legacy (single-persona, ## sections):
        Character Knowledge goes into character_text. All gated items → discovery_items.
        persona argument is ignored.

    Multi-persona (## Persona: {name} blocks):
        Character Knowledge is parsed into retrievable CK chunks (character_knowledge).
        Discovery Items → discovery_items with explicit [DI-XX] IDs.
        persona is required for multi-persona files — raises ValueError if omitted.

    Args:
        path:   Path to the scenario markdown file.
        persona: Persona name to load (multi-persona format only). Case-insensitive.

    Raises:
        ValueError: If persona is specified but not found in the file.
    """
    text = Path(path).read_text()
    if re.search(r"^## Persona:", text, re.MULTILINE):
        return _load_multi_persona(text, Path(path), persona)
    return _load_legacy(text, Path(path))


def _load_legacy(text: str, path: Path) -> Scenario:
    """
    Legacy single-persona format.

    Character Knowledge goes into character_text.
    All gated items (surface + tacit) → discovery_items.
    """
    title_match = re.search(r"#\s+Scenario:\s*(.+)", text)
    title = title_match.group(1).strip() if title_match else path.stem

    section_pattern = re.compile(r"(##[^\n]+)\n(.*?)(?=\n##|\Z)", re.DOTALL)
    sections = section_pattern.findall(text)

    character_parts: list[str] = []
    briefing_parts: list[str] = []
    maturity_text: str = ""
    topic_taxonomy: dict[str, str] = {}
    discovery_items: list[ScenarioItem] = []
    seen_ids: dict[str, int] = {}

    def make_id(content: str) -> str:
        base = _slugify(content)
        count = seen_ids.get(base, 0)
        seen_ids[base] = count + 1
        return base if count == 0 else f"{base}_{count}"

    for header, body in sections:
        header_lower = header.lower()
        if any(k in header_lower for k in _DROPPED_SECTIONS):
            continue
        if any(k in header_lower for k in _BRIEFING_SECTIONS):
            briefing_parts.append(body.strip())
            continue
        if any(k in header_lower for k in _TAXONOMY_SECTIONS):
            topic_taxonomy.update(_parse_taxonomy_body(body))
            continue
        if any(k in header_lower for k in _LEGACY_CHARACTER_SECTIONS):
            character_parts.append(header.strip() + "\n" + body.strip())
            if "maturity level" in header_lower:
                maturity_text = body.strip()
            continue
        # Everything else: bullets → discovery_items
        for content, topic in _parse_bullets(body):
            discovery_items.append(ScenarioItem(
                id=make_id(content),
                content=content,
                topic=topic,
            ))

    return Scenario(
        title=title,
        character_text="\n\n".join(character_parts),
        character_knowledge=[],
        discovery_items=discovery_items,
        briefing="\n".join(briefing_parts),
        maturity=maturity_text,
        topic_taxonomy=topic_taxonomy,
    )


def _load_multi_persona(text: str, path: Path, persona: str | None) -> Scenario:
    """
    Multi-persona format (## Persona: blocks).

    Character Knowledge is parsed into retrievable CK chunks — NOT put in character_text.
    Each double-newline-separated paragraph → one ScenarioItem with id CK-01, CK-02, ...
    Paragraphs that are pure markdown headers (start with #) are skipped.
    Discovery Items → discovery_items with explicit [DI-XX] IDs.
    """
    title_match = re.search(r"#\s+Scenario:\s*(.+)", text)
    title = title_match.group(1).strip() if title_match else path.stem

    first_persona_pos = re.search(r"^## Persona:", text, re.MULTILINE).start()
    shared_text = text[:first_persona_pos]
    personas_text = text[first_persona_pos:]

    # Parse shared sections
    topic_taxonomy: dict[str, str] = {}
    briefing_parts: list[str] = []
    shared_section_pattern = re.compile(r"(##[^\n]+)\n(.*?)(?=\n##|\Z)", re.DOTALL)
    for header, body in shared_section_pattern.findall(shared_text):
        header_lower = header.lower()
        if any(k in header_lower for k in _DROPPED_SECTIONS):
            continue
        if any(k in header_lower for k in _BRIEFING_SECTIONS):
            briefing_parts.append(body.strip())
        elif any(k in header_lower for k in _TAXONOMY_SECTIONS):
            topic_taxonomy.update(_parse_taxonomy_body(body))

    # Find persona block
    persona_block_pattern = re.compile(
        r"^## Persona:\s*(.+?)\n(.*?)(?=^## Persona:|\Z)",
        re.DOTALL | re.MULTILINE,
    )
    blocks = [(m.group(1).strip(), m.group(2)) for m in persona_block_pattern.finditer(personas_text)]
    if not blocks:
        raise ValueError(f"No ## Persona: sections found in {path}")

    if persona is None:
        available = [name for name, _ in blocks]
        raise ValueError(
            f"This scenario has multiple personas {available}. "
            f"Pass persona= explicitly to load_scenario()."
        )
    else:
        match = next(((n, b) for n, b in blocks if n.lower() == persona.lower()), None)
        if match is None:
            available = [n for n, _ in blocks]
            raise ValueError(
                f"Persona '{persona}' not found in {path}. Available: {available}"
            )
        chosen_name, persona_body = match

    # Parse persona subsections
    character_parts: list[str] = []
    character_knowledge: list[ScenarioItem] = []
    discovery_items: list[ScenarioItem] = []
    maturity_text: str = ""
    seen_ids: dict[str, int] = {}

    def make_di_id(id_hint: str) -> str:
        count = seen_ids.get(id_hint, 0)
        seen_ids[id_hint] = count + 1
        return id_hint if count == 0 else f"{id_hint}_{count}"

    # Match exactly-3-hash subsection headers. [^#] in lookahead prevents ####
    # sub-sub-headers inside a section body from being treated as section boundaries.
    subsection_pattern = re.compile(r"(###[^\n]+)\n(.*?)(?=\n###[^#]|\Z)", re.DOTALL)
    ck_counter = 0

    for header, body in subsection_pattern.findall(persona_body):
        header_lower = header.lower().lstrip("#").strip()
        is_discovery = "discovery items" in header_lower
        is_ck = "character knowledge" in header_lower
        is_maturity = "maturity" in header_lower

        if is_discovery:
            for line in body.splitlines():
                parsed = _parse_discovery_item(line)
                if parsed:
                    item_id, content, topic = parsed
                    discovery_items.append(ScenarioItem(
                        id=make_di_id(item_id),
                        content=content,
                        topic=topic,
                    ))

        elif is_ck:
            # Split on double newlines; skip markdown header lines (start with #).
            paragraphs = re.split(r"\n{2,}", body)
            for para in paragraphs:
                para = para.strip()
                if not para or para.startswith("#"):
                    continue
                content, topic = _strip_topic_tags(para)
                if not content:
                    continue
                ck_counter += 1
                character_knowledge.append(ScenarioItem(
                    id=f"CK-{ck_counter:02d}",
                    content=content,
                    topic=topic,
                ))

        else:
            # Identity, Persona Maturity, Personality, Team Members → character_text
            normalized_header = "##" + header.lstrip("#")
            character_parts.append(normalized_header.strip() + "\n" + body.strip())
            if is_maturity:
                maturity_text = body.strip()

    return Scenario(
        title=f"{title} — {chosen_name}",
        character_text="\n\n".join(character_parts),
        character_knowledge=character_knowledge,
        discovery_items=discovery_items,
        briefing="\n".join(briefing_parts),
        maturity=maturity_text,
        topic_taxonomy=topic_taxonomy,
    )


# ---------------------------------------------------------------------------
# Retrieval index construction
# ---------------------------------------------------------------------------

def build_retrieval_index(scenario: Scenario) -> tuple["EmbeddingStore", "EmbeddingStore"]:
    """
    Build embedding indices for a loaded scenario.

    Returns (char_index, disc_index). Build once per session and reuse for all turns.
    In-memory only — no cross-session caching.
    """
    char_items = [(item.id, item.content) for item in scenario.character_knowledge]
    disc_items = [(item.id, item.content) for item in scenario.discovery_items]
    char_index = EmbeddingStore(char_items)
    disc_index = EmbeddingStore(disc_items)
    return char_index, disc_index


# ---------------------------------------------------------------------------
# Retrieval function
# ---------------------------------------------------------------------------

def retrieve_relevant_knowledge(
    question: str,
    char_index: "EmbeddingStore",
    disc_index: "EmbeddingStore",
    scenario: "Scenario",
    already_revealed_ids: list[str],
    recent_context: str = "",
    char_threshold: float = 0.45,
    disc_threshold: float = 0.55,
    max_char_items: int = 5,
    max_disc_items: int = 3,
) -> tuple[list[ScenarioItem], list[ScenarioItem], dict]:
    """
    Return (character_paragraphs, new_discovery_items, trace) for the given question.

    The trace dict captures what retrieval did on this turn: mode, matched items with
    scores and previews, newly revealed vs already-known DI IDs. Stored in session state
    and written to session JSON logs for offline inspection and threshold tuning.

    Pipeline:
      1. structural_check — does input contain a verb or question word?
      2. intent_check — is it a genuine question, not a catch-all or reaction?
      3. Embed the query once per index; run _query_vec for thresholded results.
      4. If needs_context() or both results are empty, re-embed with the preceding
         exchange prepended and rerun _query_vec.
      5. Compute top-5 scores for logging via a second _query_vec call (pure numpy —
         no extra API call) using the same already-computed query vector.
      6. Filter discovery matches against already_revealed_ids.
      7. Build and return the trace dict.

    Character paragraphs: fresh context for current turn (not persisted).
    Discovery items: new disclosures only (caller is responsible for persistence).
    Thresholds are first-pass defaults — review logged scores to tune.
    """
    _empty_trace = {
        "retrieval_mode": "blocked",
        "retrieved_ck_items": [],
        "matched_di_items": [],
        "newly_revealed_di_ids": [],
        "excluded_already_revealed_di_ids": [],
    }

    # --- Pre-filters ---
    if not structural_check(question):
        logger.debug("[RETRIEVAL] structural_check FAILED: %r", question)
        return [], [], _empty_trace
    if not intent_check(question):
        logger.debug("[RETRIEVAL] intent_check FAILED: %r", question)
        return [], [], _empty_trace

    # --- Embed query and run retrieval ---
    # _embed_query is called once per index per pass. _query_vec is pure numpy.
    used_context = False

    def _run(query_text: str):
        """Embed query_text and retrieve from both indices. Returns (cq, dq, cm, dm)
        where cq/dq are the raw query vectors (reused for top-5 logging) and
        cm/dm are the thresholded match lists."""
        cq = char_index._embed_query(query_text) if char_index._ids else np.array([])
        dq = disc_index._embed_query(query_text) if disc_index._ids else np.array([])
        cm = char_index._query_vec(cq, max_char_items, char_threshold) if char_index._ids else []
        dm = disc_index._query_vec(dq, max_disc_items, disc_threshold) if disc_index._ids else []
        return cq, dq, cm, dm

    char_qvec, disc_qvec, char_matches, disc_matches = _run(question)

    # --- Context-aware retry ---
    # Retry if the question is referential/short (needs_context) OR both results are empty.
    if (needs_context(question) or (not char_matches and not disc_matches)) and recent_context:
        contextual_query = (
            f"Previous exchange:\n{recent_context}\n\nCurrent question:\n{question}"
        )
        char_qvec, disc_qvec, char_matches, disc_matches = _run(contextual_query)
        used_context = True

    # --- Top-5 for logging (reuses query vectors — no extra API call) ---
    char_all5 = char_index._query_vec(char_qvec, top_k=5, threshold=-1.0) if char_index._ids else []
    disc_all5 = disc_index._query_vec(disc_qvec, top_k=5, threshold=-1.0) if disc_index._ids else []

    # --- Map IDs back to ScenarioItems ---
    char_lookup = {item.id: item for item in scenario.character_knowledge}
    disc_lookup = {item.id: item for item in scenario.discovery_items}

    _log_retrieval(
        question=question,
        used_context=used_context,
        char_all5=char_all5,
        disc_all5=disc_all5,
        char_matches=char_matches,
        disc_matches=disc_matches,
        already_revealed_ids=already_revealed_ids,
        char_lookup=char_lookup,
        disc_lookup=disc_lookup,
    )

    char_paragraphs = [char_lookup[id_] for id_, _ in char_matches if id_ in char_lookup]

    new_disc = [
        disc_lookup[id_]
        for id_, _ in disc_matches
        if id_ in disc_lookup and id_ not in already_revealed_ids
    ]

    # --- Build retrieval trace from already-available local variables (no extra API calls) ---
    trace = {
        "retrieval_mode": "context-aware" if used_context else "question-only",
        "retrieved_ck_items": [
            {
                "id": id_,
                "topic": char_lookup[id_].topic,
                "score": round(s, 4),
                "preview": char_lookup[id_].content[:50],
            }
            for id_, s in char_matches if id_ in char_lookup
        ],
        "matched_di_items": [
            {
                "id": id_,
                "topic": disc_lookup[id_].topic,
                "score": round(s, 4),
                "preview": disc_lookup[id_].content[:50],
            }
            for id_, s in disc_matches if id_ in disc_lookup
        ],
        "newly_revealed_di_ids": [
            id_ for id_, _ in disc_matches
            if id_ in disc_lookup and id_ not in already_revealed_ids
        ],
        "excluded_already_revealed_di_ids": [
            id_ for id_, _ in disc_matches if id_ in already_revealed_ids
        ],
    }

    return char_paragraphs, new_disc, trace


def _log_retrieval(
    question: str,
    used_context: bool,
    char_all5: list[tuple[str, float]],
    disc_all5: list[tuple[str, float]],
    char_matches: list[tuple[str, float]],
    disc_matches: list[tuple[str, float]],
    already_revealed_ids: list[str],
    char_lookup: dict,
    disc_lookup: dict,
) -> None:
    """
    Log retrieval diagnostics at DEBUG level for threshold calibration.

    All score data is pre-computed from the actual query path — no extra API calls.
    When context-aware retrieval was used, scores correspond to the contextual query.
    """
    if not logger.isEnabledFor(logging.DEBUG):
        return

    def _fmt(id_: str, score: float, lookup: dict) -> str:
        item = lookup.get(id_)
        preview = item.content[:50] + "..." if item and len(item.content) > 50 else (item.content if item else "?")
        return f'{id_}={score:.3f} "{preview}"'

    mode = "context-aware" if used_context else "question-only"
    logger.debug("[RETRIEVAL] question=%r  mode=%s", question, mode)

    if char_all5:
        logger.debug(
            "[RETRIEVAL] char top-5 (%s):\n  %s",
            mode,
            "\n  ".join(_fmt(id_, s, char_lookup) for id_, s in char_all5),
        )
    logger.debug(
        "[RETRIEVAL] char passed threshold: %s",
        [id_ for id_, _ in char_matches] or "none",
    )

    if disc_all5:
        logger.debug(
            "[RETRIEVAL] disc top-5 (%s):\n  %s",
            mode,
            "\n  ".join(_fmt(id_, s, disc_lookup) for id_, s in disc_all5),
        )
    logger.debug(
        "[RETRIEVAL] disc passed threshold: %s",
        [id_ for id_, _ in disc_matches] or "none",
    )

    excluded = [id_ for id_, _ in disc_matches if id_ in already_revealed_ids]
    if excluded:
        logger.debug("[RETRIEVAL] disc excluded (already revealed): %s", excluded)
