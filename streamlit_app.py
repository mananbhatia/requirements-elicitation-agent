"""
Streamlit UI for Revodata — Consultant Interview Training.

Thin UI layer over the existing conversation and evaluation pipeline.
All graph logic lives in graph.py, eval_graph.py, knowledge.py, session_logger.py.
"""

import json
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

import streamlit as st
from langchain_core.messages import HumanMessage, AIMessage

from knowledge import load_scenario
from graph import build_graph
from eval_graph import build_eval_graph
from session_logger import save_session
from paths import SCENARIOS_DIR

DEFAULT_SCENARIO = SCENARIOS_DIR / "waste_management_client.md"

# ---------------------------------------------------------------------------
# Cached resources — built once per process, shared across reruns
# ---------------------------------------------------------------------------

@st.cache_resource
def get_scenario(scenario_path: str):
    return load_scenario(scenario_path)


@st.cache_resource
def get_conversation_graph(scenario_path: str):
    scenario = get_scenario(scenario_path)
    return build_graph(scenario)


@st.cache_resource
def get_eval_graph(scenario_path: str):
    conv_graph = get_conversation_graph(scenario_path)
    return build_eval_graph(conv_graph)


# ---------------------------------------------------------------------------
# Session state initialisation
# ---------------------------------------------------------------------------

def _init_session():
    scenario_path = str(DEFAULT_SCENARIO)
    if "phase" not in st.session_state:
        st.session_state.phase = "conversation"
    if "messages" not in st.session_state:
        st.session_state.messages = []       # list of {"role": "consultant"|"client", "content": str}
    if "lc_messages" not in st.session_state:
        st.session_state.lc_messages = []   # LangChain message objects for graph
    if "revealed_items" not in st.session_state:
        st.session_state.revealed_items = []
    if "eval_state" not in st.session_state:
        st.session_state.eval_state = None
    if "log_path" not in st.session_state:
        st.session_state.log_path = None
    if "log_content" not in st.session_state:
        st.session_state.log_content = None
    if "consultant_email" not in st.session_state:
        headers = dict(st.context.headers)
        st.session_state.consultant_email = (
            headers.get("X-Forwarded-Email")
            or headers.get("X-Db-User")
            or "unknown"
        )
    if "scenario_path" not in st.session_state:
        st.session_state.scenario_path = scenario_path

    # Generate client opening on first load
    if not st.session_state.messages:
        scenario_path = st.session_state.scenario_path
        graph = get_conversation_graph(scenario_path)
        opening_prompt = HumanMessage(
            content="[Start of interview. Introduce yourself and state your opening requirement in 2-3 sentences. Be natural and conversational.]"
        )
        state = graph.invoke({"messages": [opening_prompt], "revealed_items": []})
        opening = state["messages"][-1].content
        # Drop the hidden prompt; keep only the client's opening
        st.session_state.lc_messages = [state["messages"][-1]]
        st.session_state.revealed_items = state.get("revealed_items", [])
        st.session_state.messages = [{"role": "client", "content": opening}]


def _reset_session():
    for key in ["phase", "messages", "lc_messages", "revealed_items", "eval_state", "log_path"]:
        if key in st.session_state:
            del st.session_state[key]
    st.rerun()


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

def _render_sidebar():
    scenario = get_scenario(st.session_state.scenario_path)
    with st.sidebar:
        st.markdown("## Revodata")
        st.markdown("**Consultant Interview Training**")
        st.caption("🚧 Alpha — feedback welcome.")
        st.caption("When done, click **End Interview** to run your evaluation.")
        st.divider()

        if st.session_state.phase == "conversation":
            if st.button("End Interview", type="primary", use_container_width=True):
                st.session_state.phase = "evaluating"
                st.rerun()
        elif st.session_state.phase in ("evaluation", "evaluating"):
            if st.button("Start New Interview", type="secondary", use_container_width=True):
                _reset_session()

        st.divider()

        # Briefing
        if scenario.briefing:
            for line in scenario.briefing.splitlines():
                line = line.strip()
                if not line:
                    continue
                if ":" in line:
                    label, _, value = line.partition(":")
                    st.markdown(f"**{label.strip()}:** {value.strip()}")
                else:
                    st.markdown(line)
        else:
            st.markdown(f"**Client:** {scenario.title}")
            st.markdown("**Meeting type:** Initial discovery — first meeting, no prior work done")
            st.markdown("**Your role:** Lead the discovery. Ask questions.")

        # Topic taxonomy
        if scenario.topic_taxonomy:
            st.divider()
            st.markdown("**Topics to cover**")
            taxonomy = scenario.topic_taxonomy
            top_level = {k: v for k, v in taxonomy.items() if "/" not in k}
            for code, display in top_level.items():
                subtopics = [v for k, v in taxonomy.items() if k.startswith(code + "/")]
                st.markdown(f"**{display}**")
                if subtopics:
                    st.caption(" · ".join(subtopics))


# ---------------------------------------------------------------------------
# Conversation phase
# ---------------------------------------------------------------------------

def _render_conversation():
    for msg in st.session_state.messages:
        avatar = "🧑‍💼" if msg["role"] == "consultant" else "👤"
        with st.chat_message(msg["role"], avatar=avatar):
            st.markdown(msg["content"])

    if prompt := st.chat_input("Ask a question..."):
        with st.chat_message("consultant", avatar="🧑‍💼"):
            st.markdown(prompt)
        st.session_state.messages.append({"role": "consultant", "content": prompt})

        lc_msg = HumanMessage(content=prompt)
        st.session_state.lc_messages.append(lc_msg)

        with st.spinner(""):
            graph = get_conversation_graph(st.session_state.scenario_path)
            state = graph.invoke({
                "messages": st.session_state.lc_messages,
                "revealed_items": st.session_state.revealed_items,
            })

        client_response = state["messages"][-1].content
        st.session_state.lc_messages = state["messages"]
        st.session_state.revealed_items = state.get("revealed_items", [])
        st.session_state.messages.append({"role": "client", "content": client_response})

        with st.chat_message("client", avatar="👤"):
            st.markdown(client_response)


# ---------------------------------------------------------------------------
# Evaluation phase — pipeline runner
# ---------------------------------------------------------------------------

def _run_evaluation():
    from alternative_simulator import build_alternative_simulator
    from report_generator import report_generator
    from evaluator_core import format_transcript, format_transcript_up_to, evaluate_turn_routed

    scenario = get_scenario(st.session_state.scenario_path)
    conv_graph = get_conversation_graph(st.session_state.scenario_path)
    alternative_simulator = build_alternative_simulator(conv_graph)

    messages = st.session_state.lc_messages

    consultant_turns = [
        m for m in messages
        if (isinstance(m, HumanMessage) or (isinstance(m, dict) and m.get("type") == "human"))
        and not (m.content if hasattr(m, "content") else m.get("content", "")).startswith("[Start of interview")
    ]
    n_turns = len(consultant_turns)

    all_items = [vars(item) for item in scenario.surface_items + scenario.tacit_items]
    state = {
        "transcript": messages,
        "revealed_items": st.session_state.revealed_items,
        "topic_taxonomy": scenario.topic_taxonomy,
        "scenario_items": all_items,
        "briefing": scenario.briefing,
        "maturity": scenario.maturity,
        "turn_annotations": [],
        "simulated_alternatives": [],
        "topic_coverage": {},
        "stats": {},
        "report": {},
    }

    transcript_text = format_transcript(messages)
    annotations = []
    turn_index = 0

    progress = st.progress(0, text="Step 1 of 3 — Evaluating turns (0%)")

    for message in messages:
        is_human = isinstance(message, HumanMessage) or (
            isinstance(message, dict) and message.get("type") == "human"
        )
        if not is_human:
            continue
        content = message.content if hasattr(message, "content") else message.get("content", "")
        if content.startswith("[Start of interview"):
            continue

        turn_index += 1
        truncated_text = format_transcript_up_to(messages, turn_index)
        annotation = evaluate_turn_routed(
            content, transcript_text, turn_index,
            maturity_level=scenario.maturity,
            briefing=scenario.briefing,
            truncated_transcript_text=truncated_text,
        )
        if annotation is not None:
            annotation["question"] = content
            turn_type = annotation.get("turn_type", "question")
            if turn_type not in ("explanation", "acknowledgment"):
                annotation["information_elicited"] = any(
                    item.get("unlocked_at_turn") == turn_index
                    for item in st.session_state.revealed_items
                )
            annotations.append(annotation)

        pct = int((turn_index / n_turns) * 33) if n_turns else 33
        progress.progress(pct, text=f"Step 1 of 3 — Evaluating turns ({pct}%)")

    state = {**state, "turn_annotations": annotations}

    progress.progress(33, text="Step 2 of 3 — Generating alternatives (33%)")
    state = {**state, **alternative_simulator(state)}

    progress.progress(66, text="Step 3 of 3 — Writing feedback report (66%)")
    state = {**state, **report_generator(state)}

    progress.progress(100, text="Complete (100%)")

    st.session_state.eval_state = state

    log_path, log_content = save_session(
        scenario.title,
        st.session_state.lc_messages,
        st.session_state.revealed_items,
        state,
        consultant_email=st.session_state.get("consultant_email", "unknown"),
    )
    st.session_state.log_path = str(log_path)
    st.session_state.log_content = log_content
    st.session_state.phase = "evaluation"
    st.rerun()


# ---------------------------------------------------------------------------
# Evaluation display helpers
# ---------------------------------------------------------------------------

def _get_client_response(turn_index: int) -> str:
    """Return the client's response to the consultant's nth turn from lc_messages."""
    messages = st.session_state.lc_messages
    consultant_count = 0
    for i, m in enumerate(messages):
        is_human = isinstance(m, HumanMessage) or (
            isinstance(m, dict) and m.get("type") == "human"
        )
        if not is_human:
            continue
        content = m.content if hasattr(m, "content") else m.get("content", "")
        if content.startswith("[Start of interview"):
            continue
        consultant_count += 1
        if consultant_count == turn_index:
            if i + 1 < len(messages):
                next_msg = messages[i + 1]
                if hasattr(next_msg, "content"):
                    return next_msg.content
                elif isinstance(next_msg, dict):
                    return next_msg.get("content", "")
    return ""


def _turn_icon(turn_type: str, well_formed, info_elicited) -> str:
    if turn_type in ("explanation", "acknowledgment"):
        return "➖"
    if turn_type == "solution_proposal":
        return "✅" if info_elicited else "⚠️"
    if turn_type == "unproductive_statement":
        return "🔴"
    if well_formed and info_elicited:
        return "✅"
    if well_formed and not info_elicited:
        return "⚠️"
    return "🔴"


# ---------------------------------------------------------------------------
# Evaluation display — single page, three stacked sections
# ---------------------------------------------------------------------------

def _render_evaluation():
    eval_state = st.session_state.eval_state
    annotations = eval_state.get("turn_annotations", [])
    alternatives = {
        a["turn_index"]: a
        for a in eval_state.get("simulated_alternatives", [])
    }
    report = eval_state.get("report", {})
    stats = eval_state.get("stats", {})
    coverage = eval_state.get("topic_coverage", {})
    scenario = get_scenario(st.session_state.scenario_path)
    taxonomy = scenario.topic_taxonomy

    # -------------------------------------------------------------------------
    # Section 1: Stats bar + Coverage grid
    # -------------------------------------------------------------------------
    q_total = stats.get("questions_total", 0)
    q_well = stats.get("questions_well_formed", 0)
    q_info = stats.get("questions_information_elicited", 0)
    n_sub = coverage.get("subtopics_covered", 0)
    n_sub_total = coverage.get("subtopics_total", 0)
    well_pct = int(q_well / q_total * 100) if q_total else 0
    info_pct = int(q_info / q_total * 100) if q_total else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Questions asked", q_total)
    c2.metric("Well-formed", f"{q_well} ({well_pct}%)")
    c3.metric("Info elicited", f"{q_info} ({info_pct}%)")
    c4.metric("Subtopics covered", f"{n_sub} / {n_sub_total}")

    # Coverage grid — 2 columns, compact inline subtopics
    if coverage.get("parent_to_subtopics"):
        st.markdown("**Topic Coverage**")
        covered_set = set(coverage.get("subtopics_covered_list", []))
        parent_to_subtopics = coverage.get("parent_to_subtopics", {})
        # Preserve taxonomy order (insertion order), not alphabetical
        parent_codes = [k for k in taxonomy if "/" not in k and k in parent_to_subtopics]
        cols = st.columns(2)
        for i, parent_code in enumerate(parent_codes):
            subtopics = parent_to_subtopics[parent_code]
            parent_display = taxonomy.get(parent_code, parent_code)
            n_covered = sum(1 for s in subtopics if s in covered_set)
            n_total = len(subtopics)
            if n_covered == n_total:
                frac_color = "#2e7d32"   # green — fully covered
            elif n_covered == 0:
                frac_color = "#c62828"   # red — nothing covered
            else:
                frac_color = "#e65100"   # orange — partial
            frac_html = f'<span style="color:{frac_color};font-weight:normal;">({n_covered}/{n_total})</span>'
            sub_parts = [
                f"{'✅' if s in covered_set else '❌'} {taxonomy.get(s, s)}"
                for s in subtopics
            ]
            with cols[i % 2]:
                st.markdown(f"**{parent_display}** {frac_html}", unsafe_allow_html=True)
                st.caption(" · ".join(sub_parts))

    st.divider()

    # -------------------------------------------------------------------------
    # Section 2: Summary + Continue / Stop / Start
    # -------------------------------------------------------------------------
    if report:
        summary = report.get("summary", "")
        if summary:
            st.markdown(f"*{summary}*")
            st.markdown("")

        col_c, col_s, col_st = st.columns(3)

        with col_c:
            st.subheader("✅ Continue")
            for item in report.get("continue", []):
                turns = item.get("turns", [])
                turn_ref = f" *(T{', '.join(str(t) for t in turns)})*" if turns else ""
                st.markdown(f"- {item['point']}{turn_ref}")

        with col_s:
            st.subheader("🛑 Stop")
            for item in report.get("stop", []):
                turns = item.get("turns", [])
                turn_ref = f" *(T{', '.join(str(t) for t in turns)})*" if turns else ""
                st.markdown(f"- {item['point']}{turn_ref}")

        with col_st:
            st.subheader("🚀 Start")
            for item in report.get("start", []):
                turns = item.get("turns", [])
                turn_ref = f" *(T{', '.join(str(t) for t in turns)})*" if turns else ""
                st.markdown(f"- {item['point']}{turn_ref}")

    st.divider()

    # -------------------------------------------------------------------------
    # Section 3: Turn-by-turn detail (collapsed) — individual expander per turn
    # -------------------------------------------------------------------------
    def _esc(text: str) -> str:
        return text.replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")

    def _render_comparison_table(question, danny_response, alt, well_formed, info_elicited):
        wf_icon = "✅" if well_formed is True else ("🔴" if well_formed is False else "—")
        ie_icon = "✅" if info_elicited is True else ("⚠️" if info_elicited is False else "—")
        wf_label = "Yes" if well_formed is True else ("No" if well_formed is False else "N/A")
        ie_label = "Yes" if info_elicited is True else ("No" if info_elicited is False else "N/A")
        orig_badges = f"Well-formed: {wf_icon} {wf_label} &nbsp;&nbsp; Info elicited: {ie_icon} {ie_label}"

        alt_wf_badge = "✅" if alt.get("alt_is_well_formed", True) else "🔴"
        alt_ie_badge = "✅" if alt.get("alt_information_elicited", True) else "⚠️"
        alt_wf_label = "Yes" if alt.get("alt_is_well_formed", True) else "No"
        alt_ie_label = "Yes" if alt.get("alt_information_elicited", True) else "No"
        verdict = alt.get("improvement_verdict", "")
        st.markdown(f"""
<table style="width:100%;border-collapse:collapse;font-size:0.88em;margin-top:4px;">
  <thead>
    <tr>
      <th style="width:50%;border:1px solid #ddd;padding:5px 6px;background:#f8f9fa;">Original</th>
      <th style="width:50%;border:1px solid #ddd;padding:5px 6px;background:#f8f9fa;">Alternative</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td style="border:1px solid #ddd;padding:5px 6px;vertical-align:top;"><strong>Question</strong><br>{_esc(question)}<br><small>{orig_badges}</small></td>
      <td style="border:1px solid #ddd;padding:5px 6px;vertical-align:top;"><strong>Question</strong><br>{_esc(alt['alternative_question'])}<br><small>Well-formed: {alt_wf_badge} {alt_wf_label} &nbsp;&nbsp; Info elicited: {alt_ie_badge} {alt_ie_label}</small></td>
    </tr>
    <tr>
      <td style="border:1px solid #ddd;padding:5px 6px;vertical-align:top;"><strong>Client's response</strong><br>{_esc(danny_response)}</td>
      <td style="border:1px solid #ddd;padding:5px 6px;vertical-align:top;"><strong>Simulated response</strong><br>{_esc(alt['simulated_response'])}</td>
    </tr>
  </tbody>
</table>
""", unsafe_allow_html=True)
        if verdict:
            st.caption(f"**Verdict:** {verdict}")

    st.markdown("#### Turn-by-Turn Detail")
    if not annotations:
        st.write("No consultant turns to display.")
    else:
        for ann in annotations:
            idx = ann.get("turn_index", "?")
            question = ann.get("question", "")
            turn_type = ann.get("turn_type", "question")
            well_formed = ann.get("is_well_formed")
            info_elicited = ann.get("information_elicited")
            mistakes = ann.get("mistakes", [])
            icon = _turn_icon(turn_type, well_formed, info_elicited)
            danny_response = _get_client_response(idx)
            alt = alternatives.get(idx)

            q_short = (question[:80] + "…") if len(question) > 80 else question
            with st.expander(f"{icon} Turn {idx} [{turn_type}]: {q_short}", expanded=False):
                wf_icon = "✅" if well_formed is True else ("🔴" if well_formed is False else "—")
                ie_icon = "✅" if info_elicited is True else ("⚠️" if info_elicited is False else "—")
                wf_label = "Yes" if well_formed is True else ("No" if well_formed is False else "N/A")
                ie_label = "Yes" if info_elicited is True else ("No" if info_elicited is False else "N/A")
                badges_md = f"**Well-formed:** {wf_icon} {wf_label} &nbsp;&nbsp;&nbsp; **Info elicited:** {ie_icon} {ie_label}"

                if turn_type in ("acknowledgment", "explanation"):
                    st.caption(f"Not evaluated — {turn_type}")
                    st.markdown(f"**Consultant:** {question}")
                    if danny_response:
                        st.markdown(f"**Client:** {danny_response}")

                elif turn_type == "solution_proposal":
                    if info_elicited is True:
                        st.markdown(badges_md, unsafe_allow_html=True)
                        st.markdown(f"**Consultant:** {question}")
                        if danny_response:
                            st.markdown(f"**Client:** {danny_response}")
                    elif alt:
                        _render_comparison_table(question, danny_response, alt, well_formed, info_elicited)
                    else:
                        st.markdown(badges_md, unsafe_allow_html=True)
                        st.markdown(f"**Consultant:** {question}")
                        if danny_response:
                            st.markdown(f"**Client:** {danny_response}")

                elif turn_type == "unproductive_statement":
                    if mistakes:
                        expl = mistakes[0].get("explanation", "")
                        st.markdown(f"**Mistake:** `{mistakes[0]['mistake_type']}` — {expl}")
                    if alt:
                        _render_comparison_table(question, danny_response, alt, well_formed, info_elicited)
                    else:
                        st.markdown(badges_md, unsafe_allow_html=True)
                        st.markdown(f"**Consultant:** {question}")
                        if danny_response:
                            st.markdown(f"**Client:** {danny_response}")

                else:
                    # question turn
                    is_clean = well_formed is True and info_elicited is True
                    if is_clean:
                        st.markdown(badges_md, unsafe_allow_html=True)
                        st.markdown(f"**Consultant:** {question}")
                        if danny_response:
                            st.markdown(f"**Client:** {danny_response}")
                    else:
                        if mistakes:
                            expl = mistakes[0].get("explanation", "")
                            st.markdown(f"**Mistake:** `{mistakes[0]['mistake_type']}` — {expl}")
                        elif info_elicited is False:
                            st.caption("Well-formed — did not elicit new information.")
                        if alt:
                            _render_comparison_table(question, danny_response, alt, well_formed, info_elicited)
                        else:
                            st.markdown(badges_md, unsafe_allow_html=True)
                            st.markdown(f"**Consultant:** {question}")
                            if danny_response:
                                st.markdown(f"**Client:** {danny_response}")

    # -------------------------------------------------------------------------
    # Download session log
    # -------------------------------------------------------------------------
    if st.session_state.get("log_content"):
        log_name = Path(st.session_state.log_path).name if st.session_state.log_path else "session.json"
        st.download_button(
            label="Download session log",
            data=st.session_state.log_content,
            file_name=log_name,
            mime="application/json",
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Revodata — Interview Training",
    page_icon="🎯",
    layout="wide",
)

st.title("Revodata — Consultant Interview Training")
st.divider()

_init_session()
_render_sidebar()

if st.session_state.phase == "conversation":
    _render_conversation()
elif st.session_state.phase == "evaluating":
    _render_conversation()
    st.divider()
    _run_evaluation()
elif st.session_state.phase == "evaluation":
    _render_evaluation()
