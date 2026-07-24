"""
fewshot_comparison.py
=====================
Zero-shot vs few-shot evaluator comparison on the 28-turn held-out test set
(thesis Chapter 7, few-shot experiment). Companion to evaluation_analysis.py.

Inputs: stability_results.json (zero-shot, 5 runs x 42 turns),
        fewshot_results.json   (few-shot, 5 runs x 28 test turns),
        annotation_sheet_Paul.xlsx / annotation_sheet_Joost.xlsx.

Protocol: majority label over valid runs (ERROR runs excluded); Q2 type
accuracy scored only where a ground-truth type exists (both-flagged turns for
single annotators; consensus-typed turns for the agreed subset). Exact
McNemar tests on paired Q1 correctness.
"""
import json, re
from collections import Counter
from math import comb
import openpyxl

def read_sheet(p):
    ws = openpyxl.load_workbook(p, data_only=True)['Annotations']
    return {int(r[0]): (r[3], r[4]) for r in ws.iter_rows(min_row=2, max_row=43, values_only=True)}

def norm(x):
    if x is None: return None
    s = str(x)
    if 'unproductive' in s.lower(): return None
    m = re.match(r'\s*(?:type\s*)?([1-7])\b', s, re.I)
    if m: return int(m.group(1))
    low = s.lower()
    for kw, c in [('probe',1),('assumption',1),('alternativ',2),('follow',3),('vague',4),
                  ('generic',4),('inappropriate',5),('level',5),('solution',6),('bundle',7)]:
        if kw in low: return c
    raise ValueError(f"unmapped label: {s!r}")

def majority(per_turn):
    ev = {}
    for e in per_turn:
        wf_runs = [w for w in e['run_wf'] if w is not None]   # ERROR runs excluded
        assert len(wf_runs) >= 3, f"turn {e['row_num']}: <3 valid runs"
        wf = sum(wf_runs) > len(wf_runs) / 2
        types = [norm(m) for m in e['run_mistake_types'] if m is not None]
        ev[e['row_num']] = {'has_problem': not wf,
                            'type': Counter(types).most_common(1)[0][0] if types else None}
    return ev

def mcnemar(pairs):
    b = sum(1 for z, f in pairs if z and not f)
    c = sum(1 for z, f in pairs if f and not z)
    n = b + c
    p = 1.0 if n == 0 else min(1.0, sum(comb(n, k) for k in range(min(b, c) + 1)) * 2 / 2 ** n)
    return b, c, p

P = read_sheet('annotation_sheet_Paul.xlsx')
J = read_sheet('annotation_sheet_Joost.xlsx')
ZS = majority(json.load(open('stability_results.json'))['per_turn_stability'])
FS = majority(json.load(open('fewshot_results.json'))['per_turn_stability'])
TEST = sorted(FS.keys())
assert len(TEST) == 28

def q1(ev, turns, truth):  # truth: {n: bool}
    c = sum(1 for n in turns if ev[n]['has_problem'] == truth[n])
    over = sum(1 for n in turns if ev[n]['has_problem'] and not truth[n])
    under = sum(1 for n in turns if not ev[n]['has_problem'] and truth[n])
    return {'correct': c, 'n': len(turns), 'accuracy': c/len(turns), 'over': over, 'under': under}

def q2(ev, turns, truth_flag, truth_type):
    both = [n for n in turns if ev[n]['has_problem'] and truth_flag[n] and truth_type[n] is not None]
    exc = [n for n in turns if ev[n]['has_problem'] and truth_flag[n] and truth_type[n] is None]
    c = sum(1 for n in both if ev[n]['type'] == truth_type[n])
    return {'correct': c, 'n': len(both), 'accuracy': (c/len(both) if both else None),
            'excluded_no_ground_truth': exc}

out = {'test_turns': TEST, 'comparisons': {}}
for name, H in [('AnnotatorA_Joost', J), ('AnnotatorB_Paul', P)]:
    flag = {n: H[n][0] == 'Yes' for n in TEST}
    typ = {n: norm(H[n][1]) for n in TEST}
    res = {}
    for lbl, ev in [('zero_shot', ZS), ('few_shot', FS)]:
        res[lbl] = {'Q1': q1(ev, TEST, flag), 'Q2': q2(ev, TEST, flag, typ)}
    pairs = [(ZS[n]['has_problem'] == flag[n], FS[n]['has_problem'] == flag[n]) for n in TEST]
    b, c, p = mcnemar(pairs)
    res['mcnemar_Q1'] = {'zs_only_correct': b, 'fs_only_correct': c, 'exact_p': p}
    out['comparisons'][name] = res

agreed = [n for n in TEST if (P[n][0] == 'Yes') == (J[n][0] == 'Yes')]
flag = {n: P[n][0] == 'Yes' for n in agreed}
typ = {n: (norm(P[n][1]) if norm(P[n][1]) == norm(J[n][1]) else None) for n in agreed}
res = {'n_agreed': len(agreed)}
for lbl, ev in [('zero_shot', ZS), ('few_shot', FS)]:
    res[lbl] = {'Q1': q1(ev, agreed, flag), 'Q2': q2(ev, agreed, flag, typ)}
pairs = [(ZS[n]['has_problem'] == flag[n], FS[n]['has_problem'] == flag[n]) for n in agreed]
b, c, p = mcnemar(pairs)
res['mcnemar_Q1'] = {'zs_only_correct': b, 'fs_only_correct': c, 'exact_p': p}
out['comparisons']['consensus_agreed_subset'] = res

out['verdict_changes'] = [
    {'turn': n, 'zero_shot': ZS[n], 'few_shot': FS[n],
     'paul': {'flag': P[n][0], 'type': norm(P[n][1])},
     'joost': {'flag': J[n][0], 'type': norm(J[n][1])}}
    for n in TEST if ZS[n] != FS[n]]
out['flag_counts_test_set'] = {
    'zero_shot': sum(1 for n in TEST if ZS[n]['has_problem']),
    'few_shot': sum(1 for n in TEST if FS[n]['has_problem']),
    'paul': sum(1 for n in TEST if P[n][0] == 'Yes'),
    'joost': sum(1 for n in TEST if J[n][0] == 'Yes')}

json.dump(out, open('fewshot_comparison_results.json', 'w'), indent=2)
for k, v in out['comparisons'].items():
    zs, fs = v['zero_shot'], v['few_shot']
    print(f"{k}: Q1 {zs['Q1']['accuracy']:.3f} -> {fs['Q1']['accuracy']:.3f} | "
          f"Q2 {zs['Q2']['accuracy']:.3f} -> {fs['Q2']['accuracy']:.3f} | "
          f"McNemar p={v['mcnemar_Q1']['exact_p']:.3f}")
print("Flag counts:", out['flag_counts_test_set'])
print("Saved -> fewshot_comparison_results.json")
