"""
Comparison test: Databricks GPT-OSS-120B vs Claude Sonnet 4.6 on evaluate_turn.

Runs the same consultant turns through both models and prints results side by side.
Each test case targets one or more of the 14 mistake types, plus clean baselines.
Also tests classify_turn() on statement and proposal cases.

Usage:
    python test_eval_comparison.py
"""

import os
import json
from dotenv import load_dotenv
load_dotenv()

import re
from pathlib import Path
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage

from evaluator_core import (
    MISTAKE_TYPES, EVAL_PROMPT, _extract_content,
    _get_databricks_token, _get_databricks_base_url,
    classify_turn,
)

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

def make_databricks_llm():
    return ChatOpenAI(
        model="databricks-gpt-oss-120b",
        base_url=_get_databricks_base_url(),
        api_key=_get_databricks_token(),
        temperature=0.0,
        extra_body={"reasoning_effort": "high"},
    )

def make_sonnet_llm():
    return ChatAnthropic(model="claude-sonnet-4-6", temperature=0.0)


def call_model(llm, prompt: str, use_extract: bool) -> dict | None:
    try:
        response = llm.invoke([HumanMessage(content=prompt)])
        raw = _extract_content(response) if use_extract else response.content.strip()
        if "```" in raw:
            raw = re.sub(r"```[a-z]*\n?", "", raw).replace("```", "").strip()
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if json_match:
            raw = json_match.group(0)
        return json.loads(raw)
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Test cases
# Each case has:
#   label        — short name
#   target       — which mistake type(s) we expect, or "clean"
#   transcript   — prior conversation context + the question being evaluated
#   question     — the specific consultant turn being evaluated
#   turn_index   — always 1 for simplicity
#   expect_well_formed     — True/False
#   expect_info_elicited   — True/False
# ---------------------------------------------------------------------------

CASES = [
    {
        "label": "CLEAN — specific, well-targeted question",
        "target": "clean",
        "transcript": (
            "Consultant: How is access currently managed in your Databricks environment?\n"
            "Client: Honestly it is all manual right now. Every time someone needs access, "
            "someone on the team has to go in and grant it. Thomas keeps asking for a better way."
        ),
        "question": "How is access currently managed in your Databricks environment?",
        "expect_well_formed": True,
        "expect_info_elicited": True,
    },
    {
        "label": "CLEAN — consultant explains after client asks for clarification",
        "target": "clean (consultant explaining, not a question)",
        "transcript": (
            "Consultant: Are you using SCIM provisioning?\n"
            "Client: Sorry, what do you mean by SCIM?\n"
            "Consultant: It stands for System for Cross-domain Identity Management — basically "
            "it is a protocol that lets your identity provider, like Azure Active Directory, "
            "automatically sync users and groups into Databricks instead of adding them manually."
        ),
        "question": (
            "It stands for System for Cross-domain Identity Management — basically it is a "
            "protocol that lets your identity provider, like Azure Active Directory, automatically "
            "sync users and groups into Databricks instead of adding them manually."
        ),
        "expect_well_formed": True,
        "expect_info_elicited": True,
    },
    {
        "label": "MULTIPLE REQUIREMENTS — bundled multi-part question",
        "target": "Ask a question that involves multiple kinds of requirements",
        "transcript": (
            "Client: Hi, I am Danny. We are trying to get our Databricks setup in better shape, "
            "particularly around access control."
        ),
        "question": (
            "Can you walk me through how users are onboarded, what roles and permissions exist, "
            "how compute is provisioned, and whether you have any audit logging in place?"
        ),
        "expect_well_formed": False,
        "expect_info_elicited": True,
    },
    {
        "label": "JARGON — technical acronym the client cannot be expected to know",
        "target": "Use jargon",
        "transcript": (
            "Client: Hi, I am Danny. We are trying to get our access control in better shape."
        ),
        "question": "Have you configured SCIM provisioning with your Entra ID tenant?",
        "expect_well_formed": False,
        "expect_info_elicited": False,
    },
    {
        "label": "TECHNICAL QUESTION — requires implementation knowledge to answer",
        "target": "Ask a technical question",
        "transcript": (
            "Client: Hi, I am Danny. We have Unity Catalog set up but I don't think it is "
            "configured correctly."
        ),
        "question": (
            "Are your metastore-level privileges correctly separated from catalog-level grants, "
            "and do your workspace-to-metastore bindings reflect your environment topology?"
        ),
        "expect_well_formed": False,
        "expect_info_elicited": False,
    },
    {
        "label": "ASK FOR SOLUTIONS — asking client to prescribe the fix",
        "target": "Ask for solutions",
        "transcript": (
            "Consultant: How is access granted today?\n"
            "Client: Everything is done manually. Someone on the team has to add each person by hand."
        ),
        "question": "What kind of role-based access control system would you like us to implement?",
        "expect_well_formed": False,
        "expect_info_elicited": False,
    },
    {
        "label": "VAGUE — no reasonable meaning",
        "target": "Ask a vague question which could infer no reasonable meaning",
        "transcript": (
            "Client: Hi, I am Danny. We are trying to sort out our Databricks platform."
        ),
        "question": "Tell me more.",
        "expect_well_formed": False,
        "expect_info_elicited": True,
    },
    {
        "label": "VAGUE — multiple interpretations",
        "target": "Ask a vague question that leads to multiple interpretations",
        "transcript": (
            "Consultant: How many users do you have?\n"
            "Client: Around 500 from the old reporting system, plus our internal team of about six."
        ),
        "question": "And what about the access situation?",
        "expect_well_formed": False,
        "expect_info_elicited": True,
    },
    {
        "label": "GENERIC — domain-independent, could apply to any project",
        "target": "Ask a generic, domain-independent question",
        "transcript": (
            "Client: Hi, I am Danny. We are trying to improve our data platform."
        ),
        "question": "What are your main goals for this engagement?",
        "expect_well_formed": False,
        "expect_info_elicited": True,
    },
    {
        "label": "TOO LONG — overly complex compound question",
        "target": "Ask a question that is too long or articulated",
        "transcript": (
            "Client: Hi, I am Danny. We have about 500 users from our old reporting system "
            "who need to move onto Databricks."
        ),
        "question": (
            "Given that you have approximately 500 users migrating from your legacy OBIEE "
            "reporting environment, and considering that these users are non-technical business "
            "analysts who have no prior exposure to Databricks, and taking into account that "
            "your current manual access provisioning process would clearly not scale to that "
            "volume, and also bearing in mind that your team has expressed concerns about "
            "governance and scalability, I would like to understand what your current thinking "
            "is around how you plan to structure the onboarding of these users in a way that "
            "is both secure and operationally sustainable for your small platform team?"
        ),
        "expect_well_formed": False,
        "expect_info_elicited": True,
    },
    {
        "label": "INAPPROPRIATE TO PROFILE — asks client to evaluate a technical proposal",
        "target": "Ask a question inappropriate to user's profile",
        "transcript": (
            "Consultant: We could use workspace-level isolation with catalog bindings.\n"
            "Client: Okay, that sounds interesting."
        ),
        "question": (
            "Do you think a hub-and-spoke Unity Catalog topology with per-environment "
            "metastore bindings would be the right architecture for your use case?"
        ),
        "expect_well_formed": False,
        "expect_info_elicited": False,
    },
    {
        "label": "FAIL TO ELICIT TACIT — accepts surface answer, misses the deeper process",
        "target": "Fail to elicit tacit knowledge",
        "transcript": (
            "Consultant: How do users get access to the platform?\n"
            "Client: We have a process where they request access and it gets granted.\n"
            "Consultant: Great, and is that process working well for you?"
        ),
        "question": "Great, and is that process working well for you?",
        "expect_well_formed": False,
        "expect_info_elicited": True,
    },
    {
        "label": "FAIL TO CLARIFY UNCLEAR — accepts ambiguous answer without probing",
        "target": "No clarification when unclear",
        "transcript": (
            "Consultant: How is your environment structured?\n"
            "Client: We have a few different setups across the environments.\n"
            "Consultant: Got it. And how many users do you have?"
        ),
        "question": "Got it. And how many users do you have?",
        "expect_well_formed": False,
        "expect_info_elicited": True,
    },
]

# ---------------------------------------------------------------------------
# Classification test cases (for classify_turn)
# These are NOT run through evaluate_turn — they test the classifier directly.
# ---------------------------------------------------------------------------

CLASSIFY_CASES = [
    {
        "label": "UNPRODUCTIVE STATEMENT — value judgement with no inquiry",
        "transcript": (
            "Consultant: Are all your workspaces on the public internet?\n"
            "Client: Yes, everything is on the public internet right now. We haven't set up "
            "any private network connections.\n"
            "Consultant: it means you are screwed"
        ),
        "message": "it means you are screwed",
        "turn_index": 2,
        "expect_type": "unproductive_statement",
    },
    {
        "label": "UNPRODUCTIVE STATEMENT — unprofessional reaction",
        "transcript": (
            "Consultant: Does anyone have restrictions on spinning up compute?\n"
            "Client: No, anyone can create clusters or spin up compute. We know it's not right.\n"
            "Consultant: pretty bad. anyone can hack you"
        ),
        "message": "pretty bad. anyone can hack you",
        "turn_index": 2,
        "expect_type": "unproductive_statement",
    },
    {
        "label": "SOLUTION PROPOSAL — concrete suggestion in response to a problem",
        "transcript": (
            "Consultant: How do users get added to the platform today?\n"
            "Client: Everything is manual — someone on the team has to go in and add each person.\n"
            "Consultant: We could set up SCIM provisioning to sync users automatically from your "
            "identity provider — that would remove the manual work entirely."
        ),
        "message": (
            "We could set up SCIM provisioning to sync users automatically from your "
            "identity provider — that would remove the manual work entirely."
        ),
        "turn_index": 2,
        "expect_type": "solution_proposal",
    },
    {
        "label": "QUESTION — preamble then genuine inquiry",
        "transcript": (
            "Client: We have about 500 users coming from our old reporting system.\n"
            "Consultant: That makes sense. How are those users currently being provisioned "
            "onto Databricks?"
        ),
        "message": "That makes sense. How are those users currently being provisioned onto Databricks?",
        "turn_index": 1,
        "expect_type": "question",
    },
    {
        "label": "EXPLANATION — responding to client clarification request",
        "transcript": (
            "Consultant: Are you using SCIM provisioning?\n"
            "Client: Sorry, what is SCIM?\n"
            "Consultant: It stands for System for Cross-domain Identity Management — it lets your "
            "identity provider automatically sync users into Databricks instead of adding them manually."
        ),
        "message": (
            "It stands for System for Cross-domain Identity Management — it lets your "
            "identity provider automatically sync users into Databricks instead of adding them manually."
        ),
        "turn_index": 2,
        "expect_type": "explanation",
    },
]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_case(case: dict, llm, use_extract: bool) -> dict:
    prompt = EVAL_PROMPT.format(
        mistake_types=MISTAKE_TYPES,
        transcript=case["transcript"],
        turn_index=1,
        question=case["question"],
    )
    return call_model(llm, prompt, use_extract)


def fmt_result(result: dict | None) -> str:
    if result is None or "error" in result:
        return f"ERROR: {result.get('error', 'unknown') if result else 'None'}"
    wf = "✅" if result.get("is_well_formed") else "🔴"
    ie = "✅" if result.get("information_elicited") else "⚠️"
    mistakes = result.get("mistakes", [])
    mistake_str = ", ".join(m["mistake_type"] for m in mistakes) if mistakes else "none"
    return f"well_formed={wf}  info_elicited={ie}  mistakes=[{mistake_str}]"


def expected_str(case: dict) -> str:
    wf = "✅" if case["expect_well_formed"] else "🔴"
    ie = "✅" if case["expect_info_elicited"] else "⚠️"
    return f"well_formed={wf}  info_elicited={ie}"


def matches_expected(result: dict, case: dict) -> bool:
    if result is None or "error" in result:
        return False
    return (
        result.get("is_well_formed") == case["expect_well_formed"]
        and result.get("information_elicited") == case["expect_info_elicited"]
    )


if __name__ == "__main__":
    print("Loading models...")
    db_llm = make_databricks_llm()
    sonnet_llm = make_sonnet_llm()

    db_correct = 0
    sonnet_correct = 0
    total = len(CASES)

    for i, case in enumerate(CASES, 1):
        print(f"\n{'='*80}")
        print(f"[{i}/{total}] {case['label']}")
        print(f"  Target mistake: {case['target']}")
        print(f"  Question: {case['question'][:100]}{'...' if len(case['question']) > 100 else ''}")
        print(f"  Expected:  {expected_str(case)}")

        db_result = run_case(case, db_llm, use_extract=True)
        sonnet_result = run_case(case, sonnet_llm, use_extract=False)

        db_match = matches_expected(db_result, case)
        sonnet_match = matches_expected(sonnet_result, case)
        if db_match:
            db_correct += 1
        if sonnet_match:
            sonnet_correct += 1

        print(f"  GPT-OSS:   {fmt_result(db_result)}  {'✓' if db_match else '✗'}")
        print(f"  Sonnet:    {fmt_result(sonnet_result)}  {'✓' if sonnet_match else '✗'}")

    print(f"\n{'='*80}")
    print(f"SCORE SUMMARY — evaluate_turn")
    print(f"  GPT-OSS-120B:      {db_correct}/{total} correct")
    print(f"  Claude Sonnet 4.6: {sonnet_correct}/{total} correct")

    # ---------------------------------------------------------------------------
    # Classification tests
    # ---------------------------------------------------------------------------
    print(f"\n{'='*80}")
    print(f"CLASSIFICATION TESTS — classify_turn (Claude Haiku)")
    print(f"{'='*80}")

    classify_correct = 0
    classify_total = len(CLASSIFY_CASES)

    for i, case in enumerate(CLASSIFY_CASES, 1):
        print(f"\n[{i}/{classify_total}] {case['label']}")
        print(f"  Message: {case['message'][:100]}{'...' if len(case['message']) > 100 else ''}")
        print(f"  Expected type: {case['expect_type']}")

        result = classify_turn(case["message"], case["transcript"], case["turn_index"])
        if result:
            got_type = result.get("turn_type", "?")
            reasoning = result.get("reasoning", "")
            match = got_type == case["expect_type"]
            if match:
                classify_correct += 1
            print(f"  Got type:      {got_type}  {'✓' if match else '✗'}")
            print(f"  Reasoning:     {reasoning}")
        else:
            print(f"  ERROR: classification failed")

    print(f"\n{'='*80}")
    print(f"CLASSIFICATION SCORE: {classify_correct}/{classify_total} correct")
