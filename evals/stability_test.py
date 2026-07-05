#!/usr/bin/env python3
"""
Evaluator stability test for Revelio.

Measures how consistently the production evaluator assigns turn type,
well-formed verdict, and primary mistake type across N independent runs
on the same 42 gold-set turns.

All 42 turns come from the Danny (LOW maturity) persona in waste_management.md,
so the same briefing and maturity text apply to every turn.

Usage:
  python evals/stability_test.py                   # 5 runs, all 42 turns
  python evals/stability_test.py --runs 3          # 3 runs per turn
  python evals/stability_test.py --rows 1,3,7,18   # specific rows only
  python evals/stability_test.py --output out.json # custom output path
"""

import argparse
import json
import sys
from pathlib import Path

# Allow importing from agent_v2/ root (script lives in evals/)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from evaluator_core import evaluate_turn_routed, format_transcript, format_transcript_up_to
from knowledge import load_scenario
from paths import SESSION_LOG_DIR, SCENARIOS_DIR

SCENARIO_PATH = SCENARIOS_DIR / "waste_management.md"
MANIFEST_PATH = Path(__file__).resolve().parent / "gold_set_manifest.json"

PROCEED_THRESHOLD = 0.85
INSPECT_THRESHOLD = 0.70


# ---------------------------------------------------------------------------
# Input construction
# ---------------------------------------------------------------------------

def load_session_messages(session_file: str) -> list[dict]:
    """Load session JSON and convert messages to format_transcript-compatible dicts."""
    with open(SESSION_LOG_DIR / session_file) as f:
        sess = json.load(f)
    return [
        {
            "type": "human" if m.get("role") == "consultant" else "ai",
            "content": m.get("content", ""),
        }
        for m in sess.get("transcript", [])
    ]


def build_inputs(entry: dict, messages: list[dict] | None) -> tuple[str, str, int]:
    """
    Return (full_transcript_text, truncated_transcript_text, turn_index).

    full_transcript_text:      full conversation history — passed to classify_turn
    truncated_transcript_text: history up to and including the question (no client
                               response) — passed to evaluate_turn to prevent outcome bias
    turn_index:                1-based consultant turn number

    Session turns: derived from session messages using the same format_transcript /
    format_transcript_up_to functions used in production.

    Constructed turns: built from prior_context + question embedded in the manifest.
    turn_index is inferred from "Consultant:" occurrences in prior_context + 1.
    """
    if entry["source"] == "session":
        turn_index = entry["turn_index"]
        full_text = format_transcript(messages)
        truncated_text = format_transcript_up_to(messages, turn_index)
        # Guard: confirm turn_index resolves to the expected question. An off-by-one
        # here would corrupt every session turn's inputs invisibly.
        last_consultant = next(
            (line[len("Consultant: "):] for line in reversed(truncated_text.split("\n"))
             if line.startswith("Consultant: ")),
            None,
        )
        assert last_consultant == entry["question"], (
            f"Row {entry['row_num']}: turn_index {turn_index} resolved to wrong question.\n"
            f"  Got:      {repr(last_consultant[:100] if last_consultant else None)}\n"
            f"  Expected: {repr(entry['question'][:100])}"
        )
    else:
        prior = (entry.get("prior_context") or "").strip()
        question = entry["question"]
        truncated_text = f"{prior}\n\nConsultant: {question}" if prior else f"Consultant: {question}"
        full_text = truncated_text  # no client response follows constructed questions
        turn_index = prior.count("Consultant:") + 1
        # Note: rows 18 and 22 use "Client (Danny):" in their prior_context rather
        # than the production "Client:" label. The input is constant across all N runs
        # so stability is unaffected; keep in mind for accuracy testing later.

    return full_text, truncated_text, turn_index


# ---------------------------------------------------------------------------
# Core runner
# ---------------------------------------------------------------------------

def run_stability_test(
    manifest: list[dict],
    briefing: str,
    maturity: str,
    n_runs: int,
) -> list[dict]:
    results = []
    total = len(manifest)

    for idx, entry in enumerate(manifest):
        row_num = entry["row_num"]
        source_label = entry["source_label"]
        question = entry["question"]

        print(f"[{idx+1:2d}/{total}] Row {row_num:2d}  {source_label:<22}", end="", flush=True)

        messages = None
        if entry["source"] == "session":
            messages = load_session_messages(entry["session_file"])

        full_text, truncated_text, turn_index = build_inputs(entry, messages)

        run_annotations = []
        for _ in range(n_runs):
            ann = evaluate_turn_routed(
                content=question,
                transcript_text=full_text,
                turn_index=turn_index,
                maturity_level=maturity,
                briefing=briefing,
                truncated_transcript_text=truncated_text,
            )
            if ann is None:
                ann = {"turn_type": "ERROR", "mistakes": [], "is_well_formed": None}
            run_annotations.append(ann)
            print(".", end="", flush=True)

        first = run_annotations[0]
        print(f"  {first.get('turn_type', '?'):<26} wf={str(first.get('is_well_formed'))}")

        results.append({
            "row_num": row_num,
            "source_label": source_label,
            "source": entry["source"],
            "question_preview": question[:70],
            "manifest_label": entry["label"],
            "manifest_mistake_type": entry.get("mistake_type"),
            "run_annotations": run_annotations,
        })

    return results


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _primary_mistake(ann: dict) -> str | None:
    mistakes = ann.get("mistakes") or []
    return mistakes[0].get("mistake_type") if mistakes else None


def compute_metrics(results: list[dict], n_runs: int) -> tuple[dict, list[dict]]:
    """
    Compute three aggregate stability metrics and per-turn stability records.

    turn_type_agreement:
        Fraction of turns where all N runs return the same turn_type.

    well_formed_agreement:
        Fraction of turns where all N runs return the same is_well_formed value
        (True / False / None treated as distinct).

    mistake_type_agreement:
        Among turns where at least one run returned is_well_formed=False,
        fraction where all N runs agree on the primary mistake type.
        A run that returned well-formed (None mistake) counts as a distinct value,
        so disagreement between "flagged" and "clean" runs is captured here.
    """
    n = len(results)
    type_agree = wf_agree = mistake_agree_count = mistake_eligible = 0
    per_turn: list[dict] = []

    for r in results:
        anns = r["run_annotations"]

        types = [a.get("turn_type") for a in anns]
        t_ok = len(set(types)) == 1

        wf_vals = [a.get("is_well_formed") for a in anns]
        wf_ok = len({str(v) for v in wf_vals}) == 1

        has_mistake = any(a.get("is_well_formed") is False for a in anns)
        mt_ok: bool | None = None
        if has_mistake:
            mistake_eligible += 1
            primaries = [_primary_mistake(a) for a in anns]
            mt_ok = len(set(primaries)) == 1
            if mt_ok:
                mistake_agree_count += 1

        if t_ok:
            type_agree += 1
        if wf_ok:
            wf_agree += 1

        per_turn.append({
            "row_num": r["row_num"],
            "manifest_label": r["manifest_label"],
            "type_stable": t_ok,
            "wf_stable": wf_ok,
            "mt_stable": mt_ok,
            "run_types": types,
            "run_wf": wf_vals,
            "run_mistake_types": [_primary_mistake(a) for a in anns],
        })

    metrics = {
        "n_turns": n,
        "n_runs": n_runs,
        "turn_type_agreement": round(type_agree / n, 4) if n else 0.0,
        "well_formed_agreement": round(wf_agree / n, 4) if n else 0.0,
        "mistake_type_agreement": (
            round(mistake_agree_count / mistake_eligible, 4)
            if mistake_eligible else None
        ),
        "turn_type_agree_count": type_agree,
        "well_formed_agree_count": wf_agree,
        "mistake_agree_count": mistake_agree_count,
        "mistake_eligible_count": mistake_eligible,
    }
    return metrics, per_turn


# ---------------------------------------------------------------------------
# Summary printer
# ---------------------------------------------------------------------------

def _status_label(rate: float | None) -> str:
    if rate is None:
        return "N/A"
    if rate >= PROCEED_THRESHOLD:
        return f"{rate:.1%} [PASS]"
    if rate >= INSPECT_THRESHOLD:
        return f"{rate:.1%} [INSPECT]"
    return f"{rate:.1%} [FAIL]"


def print_summary(results: list[dict], metrics: dict, per_turn: list[dict]) -> None:
    print()
    print("=" * 72)
    print("EVALUATOR STABILITY RESULTS")
    print("=" * 72)
    print()
    print(f"  Turns tested : {metrics['n_turns']:d}")
    print(f"  Runs per turn: {metrics['n_runs']:d}")
    print()
    print(f"  Turn-type agreement    {_status_label(metrics['turn_type_agreement']):<20}"
          f"({metrics['turn_type_agree_count']}/{metrics['n_turns']})")
    print(f"  Well-formed agreement  {_status_label(metrics['well_formed_agreement']):<20}"
          f"({metrics['well_formed_agree_count']}/{metrics['n_turns']})")
    mt = metrics.get("mistake_type_agreement")
    print(f"  Mistake-type agreement {_status_label(mt):<20}"
          f"({metrics['mistake_agree_count']}/{metrics['mistake_eligible_count']} eligible turns)")
    print()

    rates = [metrics["turn_type_agreement"], metrics["well_formed_agreement"]]
    if mt is not None:
        rates.append(mt)
    lowest = min(rates)
    if lowest >= PROCEED_THRESHOLD:
        print("  VERDICT: All metrics >= 85% — proceed to accuracy testing.")
    elif lowest >= INSPECT_THRESHOLD:
        print("  VERDICT: 70-85% range — inspect unstable turns below before proceeding.")
    else:
        print("  VERDICT: Below 70% — evaluator prompt needs revision before accuracy testing.")
    print()

    unstable = [
        (r, pt) for r, pt in zip(results, per_turn)
        if not pt["type_stable"] or not pt["wf_stable"] or pt["mt_stable"] is False
    ]

    if not unstable:
        print("  All turns stable across all runs.")
    else:
        print(f"  UNSTABLE TURNS ({len(unstable)}):")
        print()
        for r, pt in unstable:
            ml = r.get("manifest_label") or ""
            severity = "borderline" if "borderline" in ml else "CLEAR-CUT"
            print(f"  Row {r['row_num']:2d}  {r['source_label']:<22}  "
                  f"label={ml:<14}  [{severity}]")
            print(f"         Q: {r['question_preview']!r}")
            if not pt["type_stable"]:
                print(f"         ! turn_type      across runs: {pt['run_types']}")
            if not pt["wf_stable"]:
                print(f"         ! is_well_formed across runs: {pt['run_wf']}")
            if pt["mt_stable"] is False:
                print(f"         ! mistake_type   across runs: {pt['run_mistake_types']}")
            print()

    print("=" * 72)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Revelio evaluator stability test")
    parser.add_argument(
        "--runs", type=int, default=5,
        help="Independent evaluator runs per turn (default: 5)",
    )
    parser.add_argument(
        "--output", default=None,
        help="Output JSON path (default: evals/stability_results.json)",
    )
    parser.add_argument(
        "--rows", default=None,
        help="Comma-separated row numbers to test (default: all 42)",
    )
    args = parser.parse_args()

    output_path = (
        Path(args.output)
        if args.output
        else MANIFEST_PATH.parent / "stability_results.json"
    )

    manifest: list[dict] = json.loads(MANIFEST_PATH.read_text())
    if args.rows:
        row_filter = {int(x.strip()) for x in args.rows.split(",")}
        manifest = [e for e in manifest if e["row_num"] in row_filter]
        print(f"Filtered to {len(manifest)} rows: {sorted(row_filter)}")
        print()

    print(f"Loading scenario {SCENARIO_PATH.name} persona=Danny ... ", end="", flush=True)
    scenario = load_scenario(SCENARIO_PATH, persona="Danny")
    print("done")
    print(f"  briefing: {len(scenario.briefing)} chars   maturity: {len(scenario.maturity)} chars")
    print()

    total_calls = args.runs * len(manifest)
    print(f"{args.runs} runs × {len(manifest)} turns = {total_calls} evaluations "
          f"(each = 1 classify + 1 evaluate LLM call)")
    print()

    results = run_stability_test(
        manifest=manifest,
        briefing=scenario.briefing,
        maturity=scenario.maturity,
        n_runs=args.runs,
    )

    metrics, per_turn = compute_metrics(results, args.runs)
    print_summary(results, metrics, per_turn)

    output_data = {
        "n_runs": args.runs,
        "n_turns": len(results),
        "scenario_file": SCENARIO_PATH.name,
        "persona": "Danny",
        "metrics": metrics,
        "per_turn_stability": per_turn,
        "turns": results,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output_data, indent=2, ensure_ascii=False))
    print(f"\nResults written to {output_path}")


if __name__ == "__main__":
    main()
