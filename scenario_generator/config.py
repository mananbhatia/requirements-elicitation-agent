"""
Scenario Generator — Configuration and shared utilities.

Design rationale:
    This pipeline produces training scenarios for a synthetic client simulation system.
    Each phase transforms structured data through an LLM call with a specific prompt,
    producing an intermediate output that a human reviews before proceeding.

    The pipeline encodes design principles — not shortcuts. Every classification decision,
    narrative generation rule, and validation check traces to a principle documented in
    the system's design rationale (docs/architecture.md).

    Key principles encoded:
    1. Knowledge gating: Character knowledge vs discovery items is an architectural
       distinction, not a content preference. Discovery items are structurally excluded
       from the persona LLM's context until earned through specific questioning.
    2. The "aha" test: A discovery item is a fact where premature disclosure would
       undermine training value. If a consultant would say "aha, I didn't know that"
       upon learning it — it stays gated.
    3. No inference paths: Character knowledge must not allow the LLM to infer
       discovery items through reasoning. This is verified in Phase 5.
    4. Grounded in primary data: Scenarios should be grounded in real engagement
       materials wherever possible (per "Using AI for User Representation").
       LLM-generated content fills gaps but is explicitly flagged for human review.
    5. The briefing presents facts, not instructions: The consultant briefing describes
       the situation and expected outcome — it does not prescribe interview technique.
       The evaluation system assesses technique; the briefing provides context.
"""

import json
import os
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# LLM client
# ---------------------------------------------------------------------------

def get_client() -> Anthropic:
    """Return an Anthropic client using the ANTHROPIC_API_KEY env var."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "Set ANTHROPIC_API_KEY before running the pipeline."
        )
    return Anthropic(api_key=api_key)


DEFAULT_MODEL = "claude-sonnet-4-6"

def llm_call(
    prompt: str,
    *,
    system: str = "",
    model: str = DEFAULT_MODEL,
    max_tokens: int = 8192,
    temperature: float = 0.3,
) -> str:
    """Single-turn LLM call. Returns the text content of the response."""
    client = get_client()
    messages = [{"role": "user", "content": prompt}]
    kwargs = dict(
        model=model,
        max_tokens=max_tokens,
        messages=messages,
        temperature=temperature,
    )
    if system:
        kwargs["system"] = system
    response = client.messages.create(**kwargs)
    return response.content[0].text


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

WORKSPACE = Path(__file__).resolve().parent / "scenario_workspace"


def ensure_workspace(scenario_name: str) -> Path:
    """Create and return a workspace directory for a scenario run."""
    ws = WORKSPACE / scenario_name
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _phase_filename(phase: str, persona_name: Optional[str] = None) -> str:
    """Return the filename for a phase output, optionally persona-scoped."""
    if persona_name:
        return f"{phase}_{persona_name.lower()}.json"
    return f"{phase}_output.json"


def save_phase_output(
    scenario_name: str,
    phase: str,
    data: dict,
    persona_name: Optional[str] = None,
) -> Path:
    """Save a phase's output as JSON for human review.

    If persona_name is provided, the file is saved as {phase}_{persona}.json
    (e.g. phase3_classified_danny.json). Otherwise: {phase}_output.json.
    """
    ws = ensure_workspace(scenario_name)
    path = ws / _phase_filename(phase, persona_name)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return path


def load_phase_output(
    scenario_name: str,
    phase: str,
    persona_name: Optional[str] = None,
) -> dict:
    """Load a phase's output (possibly human-edited).

    If persona_name is provided, loads {phase}_{persona}.json.
    Otherwise: {phase}_output.json.
    """
    ws = ensure_workspace(scenario_name)
    path = ws / _phase_filename(phase, persona_name)
    if not path.exists():
        raise FileNotFoundError(
            f"No output found for phase '{phase}'"
            + (f" (persona: {persona_name})" if persona_name else "")
            + f" in scenario '{scenario_name}'. Expected: {path}"
        )
    return json.loads(path.read_text())


def save_markdown(scenario_name: str, filename: str, content: str) -> Path:
    """Save a markdown file (narrative, final scenario, etc.)."""
    ws = ensure_workspace(scenario_name)
    path = ws / filename
    path.write_text(content)
    return path


def load_markdown(scenario_name: str, filename: str) -> str:
    """Load a markdown file."""
    ws = ensure_workspace(scenario_name)
    path = ws / filename
    if not path.exists():
        raise FileNotFoundError(f"No file found: {path}")
    return path.read_text()


# ---------------------------------------------------------------------------
# Data contracts — shared across phases
# ---------------------------------------------------------------------------

# Fact categories for Phase 1 extraction
FACT_CATEGORIES = [
    "technical_finding",
    "organizational_context",
    "strategic_context",
    "team_dynamics",
    "attempted_solutions",
    "external_relationships",
    "business_use_cases",
]

# Narrative sections for Phase 4 character knowledge
NARRATIVE_SECTIONS = [
    "organizational_history",
    "strategic_context",
    "current_platform_state",
    "team_dynamics",
    "attempted_solutions",
    "external_relationships",
    "mental_model",
]

# Engagement types
ENGAGEMENT_TYPES = [
    "platform_review",
    "architecture_design",
    "migration",
    "implementation",
    "bi_analytics",
    "managed_onboarding",
]

# Maturity levels
MATURITY_LEVELS = ["LOW", "MEDIUM", "MEDIUM_HIGH", "HIGH"]

# Cloud platforms
CLOUD_PLATFORMS = ["azure", "aws", "gcp"]
