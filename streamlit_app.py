"""
Streamlit UI for Revodata — Consultant Interview Training.

Thin UI layer over the existing conversation and evaluation pipeline.
All graph logic lives in graph.py, eval_graph.py, knowledge.py, session_logger.py.
"""

from pathlib import Path
from datetime import date
from dotenv import load_dotenv
load_dotenv()

import streamlit as st
from langchain_core.messages import HumanMessage, AIMessage

from knowledge import load_scenario
from graph import build_graph
from eval_graph import build_eval_graph
from session_logger import save_session

DEFAULT_SCENARIO = Path(__file__).parent / "docs" / "scenarios" / "waste_management_client.md"

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
            # Group subtopics under their parent
            top_level = {k: v for k, v in taxonomy.items() if "/" not in k}
            for code, display in top_level.items():
                subtopics = [v for k, v in taxonomy.items() if k.startswith(code + "/")]
                st.markdown(f"**{display}**")
                if subtopics:
                    st.caption(" · ".join(subtopics))

        st.divider()

        if st.session_state.phase == "conversation":
            if st.button("End Interview", type="primary", use_container_width=True):
                st.session_state.phase = "evaluating"
                st.rerun()
        elif st.session_state.phase in ("evaluation", "evaluating"):
            if st.button("Start New Interview", type="secondary", use_container_width=True):
                _reset_session()


# ---------------------------------------------------------------------------
# Conversation phase
# ---------------------------------------------------------------------------

def _render_conversation():
    for msg in st.session_state.messages:
        role_label = "You" if msg["role"] == "consultant" else "Client"
        avatar = "🧑‍💼" if msg["role"] == "consultant" else "👤"
        with st.chat_message(msg["role"], avatar=avatar):
            st.markdown(msg["content"])

    if prompt := st.chat_input("Ask a question..."):
        # Display consultant message immediately
        with st.chat_message("consultant", avatar="🧑‍💼"):
            st.markdown(prompt)
        st.session_state.messages.append({"role": "consultant", "content": prompt})

        # Run through conversation graph
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
# Evaluation phase
# ---------------------------------------------------------------------------

def _run_evaluation():
    from alternative_simulator import build_alternative_simulator
    from report_generator import report_generator
    from evaluator_core import format_transcript, evaluate_turn

    scenario = get_scenario(st.session_state.scenario_path)
    conv_graph = get_conversation_graph(st.session_state.scenario_path)
    alternative_simulator = build_alternative_simulator(conv_graph)

    tier1_total = sum(
        1 for item in scenario.surface_items + scenario.tacit_items
        if item.tier == "TIER 1"
    )

    messages = st.session_state.lc_messages

    # Count consultant turns (excluding hidden opening prompt) for progress tracking.
    consultant_turns = [
        m for m in messages
        if (isinstance(m, HumanMessage) or (isinstance(m, dict) and m.get("type") == "human"))
        and not (m.content if hasattr(m, "content") else m.get("content", "")).startswith("[Start of interview")
    ]
    n_turns = len(consultant_turns)

    state = {
        "transcript": messages,
        "revealed_items": st.session_state.revealed_items,
        "scenario_items_total": tier1_total,
        "turn_annotations": [],
        "simulated_alternatives": [],
        "report": "",
    }

    # Step 1: evaluate each turn individually so progress bar advances per turn.
    # Step 1 occupies 0–33% of the bar.
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
        annotation = evaluate_turn(content, transcript_text, turn_index)
        if annotation is not None:
            annotation["question"] = content
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

    log_path = save_session(
        scenario.title,
        st.session_state.lc_messages,
        st.session_state.revealed_items,
        state,
    )
    st.session_state.log_path = str(log_path)
    st.session_state.phase = "evaluation"
    st.rerun()


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
            # Next message should be the client's response
            if i + 1 < len(messages):
                next_msg = messages[i + 1]
                if hasattr(next_msg, "content"):
                    return next_msg.content
                elif isinstance(next_msg, dict):
                    return next_msg.get("content", "")
    return ""


def _turn_icon(well_formed: bool, info_elicited: bool) -> str:
    if well_formed and info_elicited:
        return "✅"
    if well_formed and not info_elicited:
        return "⚠️"
    return "🔴"


def _render_evaluation():
    eval_state = st.session_state.eval_state
    annotations = eval_state.get("turn_annotations", [])
    alternatives = {
        a["turn_index"]: a
        for a in eval_state.get("simulated_alternatives", [])
    }
    report = eval_state.get("report", "")

    if st.session_state.log_path:
        st.info(f"Session logged to {st.session_state.log_path}")

    tab_turns, tab_report = st.tabs(["Turn-by-Turn Analysis", "Feedback Report"])

    with tab_turns:
        if not annotations:
            st.write("No consultant turns found to evaluate.")
        else:
            for ann in annotations:
                idx = ann.get("turn_index", "?")
                question = ann.get("question", "")
                mistakes = ann.get("mistakes", [])
                well_formed = ann.get("is_well_formed", True)
                info_elicited = ann.get("information_elicited", True)
                icon = _turn_icon(well_formed, info_elicited)
                alt = alternatives.get(idx)

                with st.expander(f"{icon} Turn {idx}: {question[:80]}{'...' if len(question) > 80 else ''}"):
                    # Assessment badges
                    col1, col2 = st.columns(2)
                    with col1:
                        status = "✅ Yes" if well_formed else "🔴 No"
                        st.markdown(f"**Well-formed:** {status}")
                    with col2:
                        status = "✅ Yes" if info_elicited else "⚠️ No"
                        st.markdown(f"**Information elicited:** {status}")

                    if mistakes:
                        st.markdown("**Mistakes:**")
                        for m in mistakes:
                            st.markdown(f"- `{m['mistake_type']}` — {m['explanation']}")

                    st.divider()

                    client_response = alt["original_response"] if alt else _get_client_response(idx)

                    def _esc(text: str) -> str:
                        return text.replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")

                    if alt:
                        alt_wf = alt.get("alt_is_well_formed", True)
                        alt_ie = alt.get("alt_information_elicited", True)
                        verdict = alt.get("improvement_verdict", "")
                        alt_wf_badge = "✅ Yes" if alt_wf else "🔴 No"
                        alt_ie_badge = "✅ Yes" if alt_ie else "⚠️ No"
                        right_q_cell = (
                            f"{_esc(alt['alternative_question'])}<br><br>"
                            f"<small><strong>Well-formed:</strong> {alt_wf_badge} &nbsp; "
                            f"<strong>Info elicited:</strong> {alt_ie_badge}</small>"
                        )
                        right_r_cell = _esc(alt["simulated_response"])
                    else:
                        right_q_cell = "—"
                        right_r_cell = "—"
                        verdict = ""

                    st.markdown(f"""
<table style="width:100%; border-collapse:collapse; font-size:0.9em;">
  <thead>
    <tr>
      <th style="width:50%; border:1px solid #ddd; padding:8px; background:#f8f9fa;">Original</th>
      <th style="width:50%; border:1px solid #ddd; padding:8px; background:#f8f9fa;">Alternative</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td style="border:1px solid #ddd; padding:8px; vertical-align:top;"><strong>Question</strong><br><br>{_esc(question)}</td>
      <td style="border:1px solid #ddd; padding:8px; vertical-align:top;"><strong>Question</strong><br><br>{right_q_cell}</td>
    </tr>
    <tr>
      <td style="border:1px solid #ddd; padding:8px; vertical-align:top;"><strong>Client's response</strong><br><br>{_esc(client_response)}</td>
      <td style="border:1px solid #ddd; padding:8px; vertical-align:top;"><strong>Simulated response</strong><br><br>{right_r_cell}</td>
    </tr>
  </tbody>
</table>
""", unsafe_allow_html=True)
                    if verdict:
                        st.caption(f"**Verdict:** {verdict}")
                    elif not alt:
                        st.caption("No alternative generated — question was well-formed and elicited information.")

    with tab_report:
        if not report:
            st.write("No report generated.")
        else:
            st.download_button(
                label="Download report",
                data=report,
                file_name=f"interview_feedback_{date.today()}.md",
                mime="text/markdown",
            )
            # Render section headers as visual separators
            for line in report.split("\n"):
                stripped = line.strip()
                if stripped in ("SUMMARY", "CONTINUE", "STOP", "START"):
                    st.markdown(f"### {stripped}")
                else:
                    st.markdown(line)


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
