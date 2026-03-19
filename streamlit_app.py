"""
Streamlit UI for Revodata — Consultant Interview Training.

Thin UI layer over the existing conversation and evaluation pipeline.
All graph logic lives in graph.py, eval_graph.py, knowledge.py, session_logger.py.
"""

from pathlib import Path
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
        st.markdown(f"**Client:** {scenario.title}")
        st.markdown("**Meeting type:** Initial discovery — first meeting, no prior work done")
        st.markdown("**Your role:** Lead the discovery. Ask questions.")
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
    scenario = get_scenario(st.session_state.scenario_path)
    eval_graph = get_eval_graph(st.session_state.scenario_path)

    tier1_total = sum(
        1 for item in scenario.surface_items + scenario.tacit_items
        if item.tier == "TIER 1"
    )

    with st.spinner("Evaluating your interview..."):
        eval_state = eval_graph.invoke({
            "transcript": st.session_state.lc_messages,
            "revealed_items": st.session_state.revealed_items,
            "scenario_items_total": tier1_total,
            "turn_annotations": [],
            "simulated_alternatives": [],
            "report": "",
        })

    st.session_state.eval_state = eval_state

    log_path = save_session(
        scenario.title,
        st.session_state.lc_messages,
        st.session_state.revealed_items,
        eval_state,
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
                    left, right = st.columns(2)

                    with left:
                        st.markdown("**Original question**")
                        st.markdown(f"> {question}")
                        st.markdown("**Client's response**")
                        st.markdown(f"> {client_response}")

                    with right:
                        if alt:
                            st.markdown("**Alternative question**")
                            st.markdown(f"> {alt['alternative_question']}")
                            st.markdown("**Simulated response**")
                            st.markdown(f"> {alt['simulated_response']}")
                        else:
                            st.markdown("*No alternative generated — question was well-formed and elicited information.*")

    with tab_report:
        if not report:
            st.write("No report generated.")
        else:
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
    _run_evaluation()
elif st.session_state.phase == "evaluation":
    _render_evaluation()
