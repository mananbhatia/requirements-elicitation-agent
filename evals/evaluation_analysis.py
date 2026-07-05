"""
evaluation_analysis.py
======================
Reproducible analysis for the LLM-as-judge evaluation (thesis Chapter 7).

Phases
------
1. Normalisation + input loading  (map three label vocabularies -> canonical 7 types)
2. Stratified train/test split      (14-turn few-shot pool / 28-turn test set, fixed seed)
3. Inter-annotator agreement        (Cohen's kappa, Paul vs Joost, with bootstrap 95% CIs)
4. Zero-shot evaluator accuracy      (majority label over 5 stability runs vs each annotator)

Inputs (all in the working directory):
    gold_set_manifest.json      - researcher key + turn source (used ONLY for stratification)
    annotation_sheet_Paul.xlsx  - annotator A, all 42 turns
    annotation_sheet_Joost.xlsx - annotator B, all 42 turns
    stability_results.json      - 5 zero-shot evaluator runs per turn

Sander's partial (10-turn) sheet is deliberately NOT loaded here: excluded from all
quantitative results by design; his comments are used only as qualitative evidence.

Outputs:
    evaluation_analysis_results.json  - machine-readable results
    (prints a human-readable report to stdout)
"""

import json
import re
import numpy as np
import openpyxl

SEED = 42
rng = np.random.default_rng(SEED)

# --------------------------------------------------------------------------- #
# PHASE 1: NORMALISATION
# --------------------------------------------------------------------------- #
# Canonical taxonomy. Every source string is mapped to one of these integers,
# or to None (no mistake / not a question).
CANON = {
    1: "Fail to probe assumptions",
    2: "Fail to explore alternatives",
    3: "Fail to follow up",
    4: "Ask a vague or generic question",
    5: "Ask a question inappropriate to client's level",
    6: "Ask for solutions",
    7: "Bundle distinct topics",
}

# Keyword -> canonical id. Order matters (checked after explicit numeric prefix).
_KEYWORDS = [
    ("probe assumption", 1), ("assumption", 1),
    ("explore alternative", 2), ("alternative", 2),
    ("follow up", 3), ("follow-up", 3),
    ("vague", 4), ("generic", 4),
    ("inappropriate", 5), ("level", 5),
    ("solution", 6),
    ("bundle", 7),
]

def normalize_type(s):
    """Map any mistake-type string (sheet / manifest / evaluator) to a canonical id 1-7 or None."""
    if s is None:
        return None
    t = str(s).strip()
    if not t or t.lower() in ("none", "no", "nan"):
        return None
    # Non-question sentinel emitted by the evaluator for statements/acks:
    if "unproductive" in t.lower():
        return None
    # 1) explicit leading number  ("1 - ...", "Type 1: ...", "Type1")
    m = re.match(r"\s*(?:type\s*)?([1-7])\b", t, re.IGNORECASE)
    if m:
        return int(m.group(1))
    # 2) keyword fallback
    low = t.lower()
    for kw, cid in _KEYWORDS:
        if kw in low:
            return cid
    return None  # unmapped -> flagged in report

def yesno_to_problem(v):
    """Sheet Q1 'Yes'/'No' -> has_problem boolean (True = has a problem)."""
    if v is None:
        return None
    return str(v).strip().lower() == "yes"

# --------------------------------------------------------------------------- #
# LOAD INPUTS
# --------------------------------------------------------------------------- #
def load_sheet(path):
    ws = openpyxl.load_workbook(path, data_only=True)["Annotations"]
    out = {}
    for r in ws.iter_rows(min_row=2, max_row=43, values_only=True):
        n = int(r[0])
        out[n] = {
            "has_problem": yesno_to_problem(r[3]),      # Q1
            "type": normalize_type(r[4]),               # Q2 (canonical id or None)
            "type_raw": r[4],
        }
    return out

paul = load_sheet("annotation_sheet_Paul.xlsx")
joost = load_sheet("annotation_sheet_Joost.xlsx")

manifest = {e["row_num"]: e for e in json.load(open("gold_set_manifest.json"))}
stability = {t["row_num"]: t for t in json.load(open("stability_results.json"))["per_turn_stability"]}

TURNS = list(range(1, 43))

# Zero-shot evaluator majority labels (mode over the 5 runs) --------------- #
def majority(xs):
    xs = [x for x in xs]
    vals, counts = np.unique([str(x) for x in xs], return_counts=True)
    return vals[np.argmax(counts)]

evaluator = {}
for n in TURNS:
    st = stability[n]
    wf_majority = majority(st["run_wf"]) == "True"      # True = well-formed
    # majority normalized mistake type over runs that flagged a mistake
    norm_runs = [normalize_type(m) for m in st["run_mistake_types"]]
    norm_flagged = [x for x in norm_runs if x is not None]
    mtype = None
    if norm_flagged:
        vals, counts = np.unique(norm_flagged, return_counts=True)
        mtype = int(vals[np.argmax(counts)])
    evaluator[n] = {
        "has_problem": (not wf_majority),               # has_problem = not well-formed
        "type": mtype,
    }

# --------------------------------------------------------------------------- #
# PHASE 2: STRATIFIED SPLIT (fixed seed)
# --------------------------------------------------------------------------- #
# Stratify on the researcher-key design category so both pool and test cover
# every type + the well-formed / borderline groups proportionally.
def stratum_of(n):
    e = manifest[n]
    if e["label"] == "mistake":
        return f"T{normalize_type(e['mistake_type'])}"
    if e["label"].startswith("borderline"):
        return "borderline"
    return "wf"

strata = {}
for n in TURNS:
    strata.setdefault(stratum_of(n), []).append(n)

POOL_FRACTION = 1/3
pool, test = [], []
for stratum in sorted(strata):
    members = sorted(strata[stratum])
    rng.shuffle(members)
    k = round(len(members) * POOL_FRACTION)          # ~1/3 to pool
    pool += members[:k]
    test += members[k:]
pool, test = sorted(pool), sorted(test)

split_report = {
    "seed": SEED, "pool_fraction": POOL_FRACTION,
    "n_pool": len(pool), "n_test": len(test),
    "pool_turns": pool, "test_turns": test,
    "per_stratum": {s: {"n": len(v),
                        "pool": sorted(set(v) & set(pool)),
                        "test": sorted(set(v) & set(test))}
                    for s, v in strata.items()},
}

# --------------------------------------------------------------------------- #
# PHASE 3: INTER-ANNOTATOR AGREEMENT (Cohen's kappa, Paul vs Joost)
# --------------------------------------------------------------------------- #
def cohen_kappa(a, b):
    """Cohen's kappa for paired categorical labels a,b (lists, equal length)."""
    a = list(a); b = list(b); N = len(a)
    cats = sorted(set(a) | set(b), key=lambda x: str(x))
    po = sum(1 for x, y in zip(a, b) if x == y) / N
    from collections import Counter
    ca, cb = Counter(a), Counter(b)
    pe = sum((ca[c]/N) * (cb[c]/N) for c in cats)
    kappa = (po - pe) / (1 - pe) if pe < 1 else 1.0
    return po, pe, kappa

def bootstrap_kappa_ci(a, b, n_boot=10000, seed=SEED):
    a = np.array(a, dtype=object); b = np.array(b, dtype=object)
    N = len(a); r = np.random.default_rng(seed)
    ks = []
    for _ in range(n_boot):
        idx = r.integers(0, N, N)
        try:
            _, _, k = cohen_kappa(a[idx].tolist(), b[idx].tolist())
            ks.append(k)
        except Exception:
            pass
    lo, hi = np.percentile(ks, [2.5, 97.5])
    return float(lo), float(hi)

def landis_koch(k):
    if k < 0: return "poor"
    if k <= .20: return "slight"
    if k <= .40: return "fair"
    if k <= .60: return "moderate"
    if k <= .80: return "substantial"
    return "almost perfect"

# --- Q1: has-problem (binary), all 42 turns
q1_p = [paul[n]["has_problem"] for n in TURNS]
q1_j = [joost[n]["has_problem"] for n in TURNS]
q1_po, q1_pe, q1_k = cohen_kappa(q1_p, q1_j)
q1_lo, q1_hi = bootstrap_kappa_ci(q1_p, q1_j)

# confusion
yy = sum(1 for n in TURNS if paul[n]["has_problem"] and joost[n]["has_problem"])
yn = sum(1 for n in TURNS if paul[n]["has_problem"] and not joost[n]["has_problem"])
ny = sum(1 for n in TURNS if not paul[n]["has_problem"] and joost[n]["has_problem"])
nn = sum(1 for n in TURNS if not paul[n]["has_problem"] and not joost[n]["has_problem"])

# --- Q2: mistake type, on turns where BOTH flagged a problem
both_yes = [n for n in TURNS if paul[n]["has_problem"] and joost[n]["has_problem"]]
q2_p = [paul[n]["type"] for n in both_yes]
q2_j = [joost[n]["type"] for n in both_yes]
q2_po, q2_pe, q2_k = cohen_kappa(q2_p, q2_j)
q2_lo, q2_hi = bootstrap_kappa_ci(q2_p, q2_j)

agreement = {
    "Q1_has_problem": {
        "n": 42, "raw_agreement": q1_po, "expected_agreement": q1_pe,
        "cohen_kappa": q1_k, "kappa_ci95": [q1_lo, q1_hi],
        "interpretation": landis_koch(q1_k),
        "confusion": {"both_yes": yy, "paulYes_joostNo": yn,
                      "paulNo_joostYes": ny, "both_no": nn},
        "paul_flagged": yy + yn, "joost_flagged": yy + ny,
    },
    "Q2_mistake_type_on_both_flagged": {
        "n": len(both_yes), "raw_agreement": q2_po, "expected_agreement": q2_pe,
        "cohen_kappa": q2_k, "kappa_ci95": [q2_lo, q2_hi],
        "interpretation": landis_koch(q2_k),
        "disagreements": [
            {"turn": n, "paul": paul[n]["type"], "joost": joost[n]["type"]}
            for n in both_yes if paul[n]["type"] != joost[n]["type"]
        ],
    },
}

# --------------------------------------------------------------------------- #
# PHASE 4: ZERO-SHOT EVALUATOR ACCURACY (on the 28-turn TEST set)
# --------------------------------------------------------------------------- #
def q1_accuracy(turns, human):
    correct = sum(1 for n in turns if evaluator[n]["has_problem"] == human[n]["has_problem"])
    over = sum(1 for n in turns if evaluator[n]["has_problem"] and not human[n]["has_problem"])
    under = sum(1 for n in turns if (not evaluator[n]["has_problem"]) and human[n]["has_problem"])
    return {"n": len(turns), "accuracy": correct/len(turns),
            "over_flag": over, "under_flag": under}

def q2_accuracy(turns, human, require_type=False):
    """Type match on turns where BOTH evaluator and human flagged a problem.

    require_type=True (used for the consensus comparison): turns whose human
    'type' is None -- i.e. the two annotators flagged a problem but disagreed
    on its type, so no consensus type exists -- are EXCLUDED from the
    denominator rather than auto-scored as evaluator errors. They are listed
    separately so the exclusion is transparent.
    """
    both = [n for n in turns if evaluator[n]["has_problem"] and human[n]["has_problem"]]
    excluded = []
    if require_type:
        excluded = [n for n in both if human[n]["type"] is None]
        both = [n for n in both if human[n]["type"] is not None]
    if not both:
        return {"n": 0, "accuracy": None, "confusions": [], "excluded_no_consensus": excluded}
    correct = sum(1 for n in both if evaluator[n]["type"] == human[n]["type"])
    conf = [{"turn": n, "evaluator": evaluator[n]["type"], "human": human[n]["type"]}
            for n in both if evaluator[n]["type"] != human[n]["type"]]
    return {"n": len(both), "accuracy": correct/len(both), "confusions": conf,
            "excluded_no_consensus": excluded}

# agreed-subset consensus within test set (turns where Paul==Joost on Q1)
test_agreed = [n for n in test if paul[n]["has_problem"] == joost[n]["has_problem"]]
consensus = {n: {"has_problem": paul[n]["has_problem"],
                 "type": (paul[n]["type"] if paul[n]["type"] == joost[n]["type"] else None)}
             for n in test_agreed}

accuracy = {
    "test_set_n": len(test),
    "zero_shot_vs_Paul":  {"Q1": q1_accuracy(test, paul),  "Q2": q2_accuracy(test, paul)},
    "zero_shot_vs_Joost": {"Q1": q1_accuracy(test, joost), "Q2": q2_accuracy(test, joost)},
    "zero_shot_vs_consensus": {
        "n_agreed_turns": len(test_agreed),
        "Q1": q1_accuracy(test_agreed, consensus),
        "Q2": q2_accuracy(test_agreed, consensus, require_type=True),
    },
}

# --------------------------------------------------------------------------- #
# WRITE + REPORT
# --------------------------------------------------------------------------- #
results = {"phase2_split": split_report, "phase3_agreement": agreement,
           "phase4_zero_shot_accuracy": accuracy}
json.dump(results, open("evaluation_analysis_results.json", "w"), indent=2)

def pct(x): return "n/a" if x is None else f"{x*100:.1f}%"

print("="*70)
print("PHASE 2  STRATIFIED SPLIT  (seed=%d)" % SEED)
print("="*70)
print(f"Few-shot pool: {split_report['n_pool']} turns -> {pool}")
print(f"Test set:      {split_report['n_test']} turns -> {test}")
print("Per stratum (n | pool | test):")
for s in sorted(split_report["per_stratum"]):
    d = split_report["per_stratum"][s]
    print(f"  {s:<12} n={d['n']:<2} pool={d['pool']} test={d['test']}")

print("\n" + "="*70)
print("PHASE 3  INTER-ANNOTATOR AGREEMENT  (Paul vs Joost, Cohen's kappa)")
print("="*70)
a1 = agreement["Q1_has_problem"]
print(f"Q1 has-problem (n=42): raw={pct(a1['raw_agreement'])}  "
      f"kappa={a1['cohen_kappa']:.3f} [{a1['kappa_ci95'][0]:.3f}, {a1['kappa_ci95'][1]:.3f}]  "
      f"({a1['interpretation']})")
print(f"   confusion: both-yes={yy}, PaulYes/JoostNo={yn}, PaulNo/JoostYes={ny}, both-no={nn}")
print(f"   Paul flagged {a1['paul_flagged']} / Joost flagged {a1['joost_flagged']}")
a2 = agreement["Q2_mistake_type_on_both_flagged"]
print(f"Q2 mistake type (n={a2['n']} both-flagged): raw={pct(a2['raw_agreement'])}  "
      f"kappa={a2['cohen_kappa']:.3f} [{a2['kappa_ci95'][0]:.3f}, {a2['kappa_ci95'][1]:.3f}]  "
      f"({a2['interpretation']})")
print("   type disagreements (turn: Paul vs Joost):")
for d in a2["disagreements"]:
    print(f"     turn {d['turn']:>2}: T{d['paul']} vs T{d['joost']}")

print("\n" + "="*70)
print("PHASE 4  ZERO-SHOT EVALUATOR ACCURACY  (28-turn test set)")
print("="*70)
for name in ["zero_shot_vs_Paul", "zero_shot_vs_Joost"]:
    d = accuracy[name]
    print(f"{name.replace('zero_shot_','').replace('_',' ')}:")
    print(f"   Q1 accuracy={pct(d['Q1']['accuracy'])} "
          f"(over-flag={d['Q1']['over_flag']}, under-flag={d['Q1']['under_flag']})")
    print(f"   Q2 type accuracy={pct(d['Q2']['accuracy'])} on n={d['Q2']['n']} both-flagged")
dc = accuracy["zero_shot_vs_consensus"]
print(f"vs agreed consensus ({dc['n_agreed_turns']} agreed test turns):")
print(f"   Q1 accuracy={pct(dc['Q1']['accuracy'])} "
      f"(over-flag={dc['Q1']['over_flag']}, under-flag={dc['Q1']['under_flag']})")
print(f"   Q2 type accuracy={pct(dc['Q2']['accuracy'])} on n={dc['Q2']['n']} "
      f"consensus-typed turns (excluded, no consensus type: "
      f"{dc['Q2']['excluded_no_consensus']})")
print("\nSaved -> evaluation_analysis_results.json")
