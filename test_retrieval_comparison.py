"""
Retrieval gate comparison: Haiku vs GPT-OSS (low) vs GPT-OSS (medium).

Tests the full retrieve_relevant_knowledge() function with real scenario items
from the waste management scenario. Focuses on:
  - Binary gate accuracy (should this question unlock anything?)
  - Multi-item yield (how many items does a well-targeted question earn?)
  - Over-matching (does a vague question unlock too much?)

Usage:
    python test_retrieval_comparison.py
"""

import os
import time
import json
import re
from dotenv import load_dotenv
load_dotenv()

from pathlib import Path
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage as LCHumanMessage
from langchain_anthropic import ChatAnthropic

from knowledge import load_scenario, ScenarioItem, _RETRIEVAL_PROMPT
from evaluator_core import _get_databricks_token, _get_databricks_base_url

SCENARIO_PATH = Path(__file__).parent / "docs" / "scenarios" / "waste_management_client.md"

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

def make_haiku():
    return ("haiku", ChatAnthropic(model="claude-haiku-4-5-20251001", temperature=0.0))

def make_gpt_oss(effort: str):
    llm = ChatOpenAI(
        model="databricks-gpt-oss-120b",
        base_url=_get_databricks_base_url(),
        api_key=_get_databricks_token(),
        temperature=0.0,
        extra_body={"reasoning_effort": effort},
    )
    return (f"gpt-oss ({effort})", llm)


def run_retrieval(llm, question: str, surface_items, tacit_items, already_revealed=None, recent_context=""):
    """Run the retrieval gate with a given LLM and return (matched_ids, elapsed_seconds)."""
    already_revealed = already_revealed or []
    unrevealed_surface = [t for t in surface_items if t.id not in already_revealed]
    unrevealed_tacit = [t for t in tacit_items if t.id not in already_revealed]

    surface_text = "\n".join(f'- id: "{t.id}", fact: "{t.content}"' for t in unrevealed_surface) or "(none)"
    tacit_text = "\n".join(f'- id: "{t.id}", fact: "{t.content}"' for t in unrevealed_tacit) or "(none)"

    prompt = _RETRIEVAL_PROMPT.format(
        question=question,
        recent_context=recent_context or "(start of conversation)",
        surface_items=surface_text,
        tacit_items=tacit_text,
    )

    start = time.time()
    response = llm.invoke([LCHumanMessage(content=prompt)])
    elapsed = time.time() - start

    content = response.content
    if isinstance(content, list):
        content = "\n".join(b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text")
    content = content.strip()
    if "```" in content:
        content = re.sub(r"```[a-z]*\n?", "", content).replace("```", "").strip()
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if not m:
        return [], elapsed
    parsed = json.loads(m.group(0))
    if not parsed.get("is_genuine", False):
        return [], elapsed
    all_ids = {t.id for t in unrevealed_surface + unrevealed_tacit}
    matched = [id for id in parsed.get("matched_ids", []) if id in all_ids]
    return matched, elapsed


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

CASES = [
    # --- Gate tests (expect 0 items) ---
    {
        "label": "GATE: bare noun phrase",
        "question": "SCIM?",
        "recent_context": "",
        "expect_count": 0,
        "expect_genuine": False,
        "note": "Should fail structural check immediately",
    },
    {
        "label": "GATE: topic reference",
        "question": "What about clusters?",
        "recent_context": "",
        "expect_count": 0,
        "expect_genuine": False,
        "note": "Naming a topic, not asking about it",
    },
    {
        "label": "GATE: catch-all",
        "question": "Is there anything else you can share with me?",
        "recent_context": "",
        "expect_count": 0,
        "expect_genuine": False,
        "note": "Catch-all should be disqualified",
    },
    {
        "label": "GATE: general how-it-works",
        "question": "How does Unity Catalog work?",
        "recent_context": "",
        "expect_count": 0,
        "expect_genuine": False,
        "note": "Asks how technology works in general, not about this client",
    },
    # --- Single item (expect ~1) ---
    {
        "label": "SINGLE: specific question about compute restrictions",
        "question": "Are there any restrictions on who can create or spin up compute in your workspaces?",
        "recent_context": "",
        "expect_count": 1,
        "expect_genuine": True,
        "note": "Should unlock: no restrictions on compute (cluster policies)",
    },
    {
        "label": "SINGLE: specific question about user sync",
        "question": "How are users added to the Databricks platform — is that automated or done manually?",
        "recent_context": "",
        "expect_count": 1,
        "expect_genuine": True,
        "note": "Should unlock: users added manually, no automated sync",
    },
    {
        "label": "SINGLE: follow-up using context",
        "question": "Is that PowerBI connection to acceptance or production?",
        "recent_context": "Consultant: Where is PowerBI connected?\nClient: PowerBI is connected to one of the environments but I would have to check which one.",
        "expect_count": 1,
        "expect_genuine": True,
        "note": "Follow-up resolves via context — should unlock PowerBI on acceptance",
    },
    # --- Multi-item (expect 2-4) ---
    {
        "label": "MULTI: access control breadth question",
        "question": "How is access to data currently controlled — who can see what, and how is that managed?",
        "recent_context": "",
        "expect_count": 3,
        "expect_genuine": True,
        "note": "Covers access grants, row-level, object ownership — should unlock multiple",
    },
    {
        "label": "MULTI: environment separation question",
        "question": "How are your dev, acceptance, and production environments separated — can each workspace only reach its own data?",
        "recent_context": "",
        "expect_count": 3,
        "expect_genuine": True,
        "note": "Covers workspace isolation, folder-based separation, cross-workspace access",
    },
    {
        "label": "MULTI: network and security posture",
        "question": "Is your Databricks platform on a private network or is it accessible over the public internet?",
        "recent_context": "",
        "expect_count": 2,
        "expect_genuine": True,
        "note": "Should unlock: everything on public internet + no private network connection",
    },
    {
        "label": "MULTI: identity and user provisioning",
        "question": "How are users provisioned into Databricks — do you sync from your company identity system or add them manually?",
        "recent_context": "",
        "expect_count": 2,
        "expect_genuine": True,
        "note": "Should unlock: manual user adds + identity sync in progress but not live",
    },
    # --- Over-matching guard (expect low count despite broad phrasing) ---
    {
        "label": "OVERFIT GUARD: broad vague question",
        "question": "Can you tell me about your current setup?",
        "recent_context": "",
        "expect_count": 0,
        "expect_genuine": False,
        "note": "Vague — should not unlock a flood of items",
    },
    {
        "label": "OVERFIT GUARD: slightly more specific but still broad",
        "question": "What security concerns does your team have?",
        "recent_context": "",
        "expect_count": 1,
        "expect_genuine": True,
        "note": "Somewhat specific but broad — should unlock at most 1-2, not everything",
    },
]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    scenario = load_scenario(SCENARIO_PATH)
    surface_items = scenario.surface_items
    tacit_items = scenario.tacit_items
    all_items = {t.id: t for t in surface_items + tacit_items}

    print(f"Scenario loaded: {len(surface_items)} surface items, {len(tacit_items)} tacit items\n")

    models = [make_haiku(), make_gpt_oss("low"), make_gpt_oss("medium")]

    for i, case in enumerate(CASES, 1):
        print(f"{'='*80}")
        print(f"[{i}/{len(CASES)}] {case['label']}")
        print(f"  Question:  {case['question']}")
        print(f"  Expected:  genuine={case['expect_genuine']}  items≈{case['expect_count']}  ({case['note']})")

        for name, llm in models:
            try:
                matched_ids, elapsed = run_retrieval(
                    llm, case["question"], surface_items, tacit_items,
                    recent_context=case["recent_context"]
                )
                genuine = len(matched_ids) > 0 or case["expect_genuine"] is False
                gate_ok = (len(matched_ids) == 0) == (not case["expect_genuine"])
                count_ok = abs(len(matched_ids) - case["expect_count"]) <= 1  # ±1 tolerance
                ok = "✓" if (gate_ok and count_ok) else "✗"
                items_preview = ", ".join(
                    all_items[id].content[:50] + "..." if len(all_items.get(id, ScenarioItem("","","","")).content) > 50
                    else all_items.get(id, ScenarioItem(id, id, "", "")).content
                    for id in matched_ids
                ) or "none"
                print(f"  {name:<20} [{elapsed:.1f}s] {ok}  {len(matched_ids)} items: {items_preview[:120]}")
            except Exception as e:
                print(f"  {name:<20} ERROR: {e}")

    print(f"\n{'='*80}")
    print("Done.")
