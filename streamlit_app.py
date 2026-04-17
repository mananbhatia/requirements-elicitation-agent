"""
Streamlit UI for Revelio — consultant interview training tool.

Thin UI layer over the existing conversation and evaluation pipeline.
All graph logic lives in graph.py, eval_graph.py, knowledge.py, session_logger.py.
"""

import json
import re
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

import streamlit as st
from langchain_core.messages import HumanMessage, AIMessage

from knowledge import load_scenario
from graph import build_graph
from eval_graph import build_eval_graph
from session_logger import save_session, save_partial_session
from paths import SCENARIOS_DIR

DEFAULT_SCENARIO = SCENARIOS_DIR / "waste_management.md"

# ---------------------------------------------------------------------------
# Cached resources — built once per process, shared across reruns
# ---------------------------------------------------------------------------

@st.cache_resource
def get_scenario(scenario_path: str, persona: str = None):
    return load_scenario(scenario_path, persona=persona)


@st.cache_resource
def get_conversation_graph(scenario_path: str, persona: str = None):
    scenario = get_scenario(scenario_path, persona)
    return build_graph(scenario)


@st.cache_resource
def get_eval_graph(scenario_path: str, persona: str = None):
    conv_graph = get_conversation_graph(scenario_path, persona)
    return build_eval_graph(conv_graph)


# ---------------------------------------------------------------------------
# Branded header — used on every phase
# ---------------------------------------------------------------------------

def _render_header():
    st.markdown("""
<div class="revelio-header">
  <div class="revelio-title">Revelio&nbsp;🪄</div>
  <p class="revelio-tagline">Ask the right question. Watch them reveal.</p>
</div>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Session state initialisation
# ---------------------------------------------------------------------------

def _parse_personas_from_file(path: str) -> list | None:
    """
    Parse ## Persona: {name} sections from a multi-persona scenario file.
    Returns list of {"name": str, "description": str} or None for single-persona files.
    Description is the first sentence of the Identity section.
    """
    text = Path(path).read_text()
    if not re.search(r"^## Persona:", text, re.MULTILINE):
        return None

    personas = []
    blocks = re.split(r"^## Persona:\s*", text, flags=re.MULTILINE)
    for block in blocks[1:]:
        lines = block.splitlines()
        name = lines[0].strip() if lines else "Unknown"

        role = ""
        maturity = ""
        in_identity = False
        in_maturity = False

        for line in lines[1:]:
            if re.match(r"^###\s+Identity", line, re.IGNORECASE):
                in_identity, in_maturity = True, False
                continue
            if re.match(r"^###\s+Persona Maturity", line, re.IGNORECASE):
                in_maturity, in_identity = True, False
                continue
            if re.match(r"^###", line):
                in_identity = in_maturity = False
                continue

            if in_identity and not role:
                content = line.strip()
                if content:
                    # Strip "You are {name}, " prefix
                    prefix = f"you are {name.lower()}, "
                    if content.lower().startswith(prefix):
                        content = content[len(prefix):]
                        content = content[:1].upper() + content[1:]
                    # Strip company context: everything from " at " onwards
                    at_idx = content.lower().find(" at ")
                    if at_idx != -1:
                        content = content[:at_idx]
                    role = content.rstrip(".,")

            if in_maturity and not maturity:
                m = re.match(r"Level:\s*(\S+)", line.strip(), re.IGNORECASE)
                if m:
                    maturity = m.group(1).upper()

        personas.append({"name": name, "role": role, "maturity": maturity})

    return personas or None


def _init_session():
    scenario_path = str(DEFAULT_SCENARIO)

    # On first load, detect whether persona selection is needed
    if "phase" not in st.session_state:
        if _parse_personas_from_file(scenario_path):
            st.session_state.phase = "persona_selection"
        else:
            st.session_state.phase = "conversation"
            st.session_state.selected_persona = None

    # Don't initialise conversation until a persona is chosen
    if st.session_state.phase == "persona_selection":
        return

    persona = st.session_state.get("selected_persona")

    if "messages" not in st.session_state:
        st.session_state.messages = []       # list of {"role": "consultant"|"client", "content": str}
    if "lc_messages" not in st.session_state:
        st.session_state.lc_messages = []   # LangChain message objects for graph
    if "revealed_items" not in st.session_state:
        st.session_state.revealed_items = []
    if "retrieval_traces" not in st.session_state:
        st.session_state.retrieval_traces = []
    if "eval_state" not in st.session_state:
        st.session_state.eval_state = None
    if "log_path" not in st.session_state:
        st.session_state.log_path = None
    if "log_content" not in st.session_state:
        st.session_state.log_content = None
    if "session_id" not in st.session_state:
        from datetime import datetime
        st.session_state.session_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
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
        graph = get_conversation_graph(st.session_state.scenario_path, persona)
        opening_prompt = HumanMessage(
            content="[Start of interview. Introduce yourself and state your opening requirement in 2-3 sentences. Be natural and conversational.]"
        )
        state = graph.invoke({"messages": [opening_prompt], "revealed_items": [], "retrieval_traces": []})
        opening = state["messages"][-1].content
        # Drop the hidden prompt; keep only the client's opening
        st.session_state.lc_messages = [state["messages"][-1]]
        st.session_state.revealed_items = state.get("revealed_items", [])
        st.session_state.messages = [{"role": "client", "content": opening}]


def _reset_session():
    for key in ["phase", "messages", "lc_messages", "revealed_items", "retrieval_traces",
                "eval_state", "log_path", "log_content", "selected_persona", "scenario_path"]:
        if key in st.session_state:
            del st.session_state[key]
    st.rerun()


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

def _render_sidebar():
    if st.session_state.phase == "persona_selection":
        return

    persona = st.session_state.get("selected_persona")
    scenario = get_scenario(st.session_state.scenario_path, persona)
    with st.sidebar:
        st.caption("🚧 Alpha. Feedback welcome.")

        if st.session_state.phase == "conversation":
            st.caption("When done, click **End Interview** to run your evaluation. Don't forget 📋")
            if st.button("End Interview", type="primary", use_container_width=True):
                st.session_state.phase = "evaluating"
                st.rerun()
        elif st.session_state.phase in ("evaluation", "evaluating"):
            if st.button("Start New Interview", type="secondary", use_container_width=True):
                _reset_session()

        # Briefing
        st.markdown('<p class="sidebar-label">Consultant Briefing</p>', unsafe_allow_html=True)
        if scenario.briefing:
            rows_html = ""
            for line in scenario.briefing.splitlines():
                line = line.strip()
                if not line:
                    continue
                if ":" in line:
                    lbl, _, val = line.partition(":")
                    rows_html += (f'<div class="sidebar-briefing-row">'
                                  f'<span class="sidebar-briefing-key">{lbl.strip()}:</span> {val.strip()}'
                                  f'</div>')
                else:
                    rows_html += f'<div class="sidebar-briefing-row">{line}</div>'
            st.markdown(f'<div class="sidebar-briefing">{rows_html}</div>', unsafe_allow_html=True)
        else:
            st.markdown(f'<div class="sidebar-briefing">'
                        f'<div class="sidebar-briefing-row"><span class="sidebar-briefing-key">Client:</span> {scenario.title}</div>'
                        f'<div class="sidebar-briefing-row"><span class="sidebar-briefing-key">Meeting type:</span> Initial discovery</div>'
                        f'<div class="sidebar-briefing-row"><span class="sidebar-briefing-key">Your role:</span> Lead the discovery. Ask questions.</div>'
                        f'</div>', unsafe_allow_html=True)

        # Topic taxonomy — orientation for the consultant, not a scored checklist
        if scenario.topic_taxonomy:
            st.markdown('<p class="sidebar-label" style="margin-top:0.75rem;">Topics to cover</p>',
                        unsafe_allow_html=True)
            taxonomy = scenario.topic_taxonomy
            top_level = {k: v for k, v in taxonomy.items() if "/" not in k}
            rows_html = ""
            for code, display in top_level.items():
                subtopics = [v for k, v in taxonomy.items() if k.startswith(code + "/")]
                rows_html += f'<div class="sidebar-briefing-row"><span class="sidebar-briefing-key">{display}</span>'
                if subtopics:
                    rows_html += f'<br><span style="color:#9ca3af;font-size:0.73rem;">{" · ".join(subtopics)}</span>'
                rows_html += '</div>'
            st.markdown(f'<div class="sidebar-briefing" style="margin-top:0.2rem;">{rows_html}</div>',
                        unsafe_allow_html=True)

        if st.session_state.phase == "conversation":
            st.divider()
            if st.button("End Interview", key="end_interview_bottom",
                         type="primary", use_container_width=True):
                st.session_state.phase = "evaluating"
                st.rerun()


# ---------------------------------------------------------------------------
# Persona selection
# ---------------------------------------------------------------------------

def _parse_briefing_from_file(path: str) -> str | None:
    """Extract the Consultant Briefing section from a scenario file."""
    text = Path(path).read_text()
    match = re.search(r"^## Consultant Briefing\s*\n(.*?)(?=^##\s|\Z)", text,
                      re.MULTILINE | re.DOTALL)
    if match:
        return match.group(1).strip()
    return None


def _render_persona_selection():
    personas = _parse_personas_from_file(str(DEFAULT_SCENARIO))
    if not personas:
        # Single-persona file — skip selection
        st.session_state.selected_persona = None
        st.session_state.phase = "conversation"
        st.rerun()
        return

    _render_header()

    # Briefing above the persona cards — Client context first
    briefing = _parse_briefing_from_file(str(DEFAULT_SCENARIO))
    if briefing:
        fields = {}
        for line in briefing.splitlines():
            line = line.strip()
            if not line:
                continue
            if ":" in line:
                label, _, value = line.partition(":")
                fields[label.strip().lower()] = (label.strip(), value.strip())
            else:
                fields[line.lower()] = (line, "")

        ordered_keys = ["client context", "engagement", "what they asked for",
                        "meeting type", "what is known going in", "expected outcome"]
        ordered_items = []
        rendered = set()
        for key in ordered_keys:
            if key in fields:
                ordered_items.append(fields[key])
                rendered.add(key)
        for key, pair in fields.items():
            if key not in rendered:
                ordered_items.append(pair)

        rows_html = "\n".join(
            f'<div class="briefing-card-row"><strong>{lbl}:</strong> {val}</div>' if val
            else f'<div class="briefing-card-row"><strong>{lbl}</strong></div>'
            for lbl, val in ordered_items
        )
        st.markdown(f"""
<div class="briefing-card">
  <div class="briefing-card-label">Consultant Briefing</div>
  {rows_html}
</div>""", unsafe_allow_html=True)

    st.markdown("""
<div style="margin: 0.4rem 0 0 0;">
  <p class="section-heading">Choose who to interview first</p>
  <p class="section-caption">Recommended: start with Danny for business/context discovery. Use Sajith for technical deep dives.</p>
</div>
""", unsafe_allow_html=True)

    _MATURITY_HINTS = {
        "LOW": "Non-technical. Knows symptoms, speaks in business terms.",
        "MEDIUM": "Semi-technical. Familiar with concepts, not deep implementation.",
        "MEDIUM_HIGH": "Technical. Platform background, operational perspective.",
        "HIGH": "Highly technical. Can evaluate proposals and challenge assumptions.",
    }

    cols = st.columns(len(personas))
    for i, persona in enumerate(personas):
        hint = _MATURITY_HINTS.get(persona["maturity"], "")
        with cols[i]:
            st.markdown(f"""
<div class="persona-card">
  <p class="persona-name">{persona['name']}</p>
  <p class="persona-role">{persona['role']}</p>
  <p class="persona-hint">{hint}</p>
</div>
""", unsafe_allow_html=True)
            if st.button("Select", key=f"select_{persona['name']}",
                         use_container_width=True, type="primary"):
                st.session_state.selected_persona = persona["name"]
                st.session_state.phase = "conversation"
                st.rerun()


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
            graph = get_conversation_graph(
                st.session_state.scenario_path,
                st.session_state.get("selected_persona"),
            )
            state = graph.invoke({
                "messages": st.session_state.lc_messages,
                "revealed_items": st.session_state.revealed_items,
            })

        client_response = state["messages"][-1].content
        st.session_state.lc_messages = state["messages"]
        st.session_state.revealed_items = state.get("revealed_items", [])
        st.session_state.retrieval_traces += state.get("retrieval_traces", [])
        st.session_state.messages.append({"role": "client", "content": client_response})

        with st.chat_message("client", avatar="👤"):
            st.markdown(client_response)

        # Best-effort partial save after every turn — captures transcript even
        # if consultant never clicks End Interview
        persona = st.session_state.get("selected_persona")
        scenario = get_scenario(st.session_state.scenario_path, persona)
        save_partial_session(
            scenario_title=scenario.title,
            transcript=st.session_state.lc_messages,
            revealed_items=st.session_state.revealed_items,
            session_id=st.session_state.session_id,
            consultant_email=st.session_state.get("consultant_email", "unknown"),
            retrieval_traces=st.session_state.retrieval_traces,
        )


# ---------------------------------------------------------------------------
# Evaluation phase — pipeline runner
# ---------------------------------------------------------------------------

def _run_evaluation():
    from alternative_simulator import build_alternative_simulator
    from report_generator import report_generator
    from evaluator_core import format_transcript, format_transcript_up_to, evaluate_turn_routed

    persona = st.session_state.get("selected_persona")
    scenario = get_scenario(st.session_state.scenario_path, persona)
    conv_graph = get_conversation_graph(st.session_state.scenario_path, persona)
    alternative_simulator = build_alternative_simulator(conv_graph)

    messages = st.session_state.lc_messages

    consultant_turns = [
        m for m in messages
        if (isinstance(m, HumanMessage) or (isinstance(m, dict) and m.get("type") == "human"))
        and not (m.content if hasattr(m, "content") else m.get("content", "")).startswith("[Start of interview")
    ]
    n_turns = len(consultant_turns)

    state = {
        "transcript": messages,
        "revealed_items": st.session_state.revealed_items,
        "briefing": scenario.briefing,
        "maturity": scenario.maturity,
        "turn_annotations": [],
        "simulated_alternatives": [],
        "stats": {},
        "report": {},
    }

    transcript_text = format_transcript(messages)
    annotations = []
    turn_index = 0

    progress = st.progress(0, text="Step 1 of 3: Evaluating turns (0%)")

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
            annotations.append(annotation)

        pct = int((turn_index / n_turns) * 33) if n_turns else 33
        progress.progress(pct, text=f"Step 1 of 3: Evaluating turns ({pct}%)")

    state = {**state, "turn_annotations": annotations}

    progress.progress(33, text="Step 2 of 3: Generating alternatives (33%)")
    state = {**state, **alternative_simulator(state)}

    progress.progress(66, text="Step 3 of 3: Writing feedback report (66%)")
    state = {**state, **report_generator(state)}

    progress.progress(100, text="Complete (100%)")

    st.session_state.eval_state = state

    log_path, log_content = save_session(
        scenario.title,
        st.session_state.lc_messages,
        st.session_state.revealed_items,
        state,
        consultant_email=st.session_state.get("consultant_email", "unknown"),
        retrieval_traces=st.session_state.retrieval_traces,
        session_id=st.session_state.session_id,
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


def _turn_icon(turn_type: str, well_formed) -> str:
    if turn_type in ("explanation", "acknowledgment"):
        return "➖"
    if turn_type == "solution_proposal":
        return "✅"
    if turn_type == "unproductive_statement":
        return "🔴"
    return "✅" if well_formed else "🔴"


# ---------------------------------------------------------------------------
# Evaluation display — single page, three stacked sections
# ---------------------------------------------------------------------------

def _generate_report_pdf(eval_state: dict, scenario_title: str, persona_name: str | None) -> bytes:
    """Generate a PDF feedback report from eval_state. Returns PDF bytes."""
    from fpdf import FPDF
    from datetime import datetime

    report = eval_state.get("report", {})
    stats = eval_state.get("stats", {})
    annotations = eval_state.get("turn_annotations", [])
    alternatives = {a["turn_index"]: a for a in eval_state.get("simulated_alternatives", [])}

    def _ps(text: str) -> str:
        """
        Sanitize text for Helvetica (Latin-1 only).
        Replace known Unicode, then drop anything still outside Latin-1 so FPDF
        never encounters a character it cannot render.
        """
        text = (
            text
            .replace("\u2014", "-")   # em dash
            .replace("\u2013", "-")   # en dash
            .replace("\u2018", "'")   # left single quote
            .replace("\u2019", "'")   # right single quote
            .replace("\u201c", '"')   # left double quote
            .replace("\u201d", '"')   # right double quote
            .replace("\u2026", "...")  # ellipsis
            .replace("\u00a0", " ")   # non-breaking space
            .replace("\u20ac", "EUR") # euro sign (common in scenario data)
        )
        # Drop any remaining characters outside Latin-1 rather than letting FPDF crash.
        return text.encode("latin-1", errors="ignore").decode("latin-1")

    pdf = FPDF()
    pdf.set_margins(20, 20, 20)
    pdf.add_page()

    def h1(text):
        pdf.set_font("Helvetica", "B", 15)
        pdf.cell(0, 9, _ps(text), ln=True)
        pdf.ln(1)

    def h2(text):
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 8, _ps(text), ln=True)
        pdf.ln(1)

    def body(text, indent=0):
        pdf.set_font("Helvetica", "", 10)
        pdf.set_x(pdf.get_x() + indent)
        pdf.multi_cell(0, 6, _ps(text))

    def label_value(label, value):
        pdf.set_font("Helvetica", "B", 10)
        pdf.write(6, _ps(f"{label}: "))
        pdf.set_font("Helvetica", "", 10)
        pdf.write(6, _ps(value))
        pdf.ln(6)

    def divider():
        pdf.ln(2)
        pdf.set_draw_color(200, 200, 200)
        pdf.line(pdf.get_x(), pdf.get_y(), pdf.get_x() + 170, pdf.get_y())
        pdf.ln(4)

    # --- Header ---
    h1("Interview Feedback Report")
    pdf.set_font("Helvetica", "", 10)
    subtitle = scenario_title
    if persona_name:
        subtitle += f" - {persona_name}"
    pdf.cell(0, 6, _ps(subtitle), ln=True)
    pdf.cell(0, 6, datetime.now().strftime("Generated %d %b %Y, %H:%M"), ln=True)
    divider()

    # --- Stats ---
    h2("Performance Summary")
    q_total = stats.get("questions_total", 0)
    q_well = stats.get("questions_well_formed", 0)
    well_pct = int(q_well / q_total * 100) if q_total else 0
    label_value("Questions asked", str(q_total))
    label_value("Well-formed", f"{q_well} ({well_pct}%)")
    divider()

    # --- Summary + Continue / Stop / Start ---
    if report:
        summary = report.get("summary", "")
        if summary:
            pdf.set_font("Helvetica", "I", 10)
            pdf.multi_cell(0, 6, _ps(summary))
            pdf.ln(4)

        for section, label in [("continue", "Continue"), ("stop", "Stop"), ("start", "Start")]:
            items = report.get(section, [])
            if items:
                h2(label)
                for item in items:
                    turns = item.get("turns", [])
                    turn_ref = f" (T{', '.join(str(t) for t in turns)})" if turns else ""
                    body(f"- {item['point']}{turn_ref}")
                    pdf.ln(1)
        divider()

    # --- Turn-by-turn ---
    h2("Turn-by-Turn Detail")
    for ann in annotations:
        idx = ann.get("turn_index", "?")
        question = ann.get("question", "")
        turn_type = ann.get("turn_type", "question")
        well_formed = ann.get("is_well_formed")
        mistakes = ann.get("mistakes", [])
        alt = alternatives.get(idx)

        wf = "Yes" if well_formed is True else ("No" if well_formed is False else "N/A")

        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 7, f"Turn {idx} [{turn_type}]  |  Well-formed: {wf}", ln=True)

        pdf.set_font("Helvetica", "", 9)
        pdf.multi_cell(0, 5, _ps(f"You: {question}"))

        if mistakes:
            pdf.set_font("Helvetica", "I", 9)
            m = mistakes[0]
            pdf.multi_cell(0, 5, _ps(f"Mistake: {m.get('mistake_type', '')} - {m.get('explanation', '')}"))

        if alt and alt.get("improvement_verdict"):
            pdf.set_font("Helvetica", "I", 9)
            pdf.multi_cell(0, 5, _ps(f"Verdict: {alt['improvement_verdict']}"))

        pdf.ln(3)

    return bytes(pdf.output())


def _render_evaluation():
    eval_state = st.session_state.eval_state
    annotations = eval_state.get("turn_annotations", [])
    alternatives = {
        a["turn_index"]: a
        for a in eval_state.get("simulated_alternatives", [])
    }
    report = eval_state.get("report", {})
    stats = eval_state.get("stats", {})

    # -------------------------------------------------------------------------
    # Section 1: Stats strip
    # -------------------------------------------------------------------------
    q_total = stats.get("questions_total", 0)
    q_well = stats.get("questions_well_formed", 0)
    q_poor = q_total - q_well
    poor_pct = int(q_poor / q_total * 100) if q_total else 0

    st.markdown(f"""
<div class="eval-stats-strip">
  <div class="eval-stat">
    <span class="eval-stat-value">{q_total}</span>
    <span class="eval-stat-label">Questions asked</span>
  </div>
  <div class="eval-stat">
    <span class="eval-stat-value eval-stat-value-green">{q_well}</span>
    <span class="eval-stat-label">Well-formed</span>
  </div>
  <div class="eval-stat">
    <span class="eval-stat-value eval-stat-value-amber">{q_poor}</span>
    <span class="eval-stat-label">Needs work<span class="eval-stat-pct">{poor_pct}%</span></span>
  </div>
</div>
""", unsafe_allow_html=True)

    # -------------------------------------------------------------------------
    # Section 2: Summary + Continue / Stop / Start
    # -------------------------------------------------------------------------
    if report:
        summary = report.get("summary", "")
        if summary:
            st.markdown(f'<div class="eval-summary">{summary}</div>', unsafe_allow_html=True)

        def _report_card(items, card_cls, badge_cls, label, icon):
            if not items:
                body = '<p class="report-item-empty">None identified</p>'
            else:
                body = ""
                for item in items:
                    turns = item.get("turns", [])
                    turn_span = (f' <span class="report-item-turn">T{", ".join(str(t) for t in turns)}</span>'
                                 if turns else "")
                    body += f'<div class="report-item">{item["point"]}{turn_span}</div>'
            return (f'<div class="report-card {card_cls}">'
                    f'<span class="report-card-badge {badge_cls}">{icon}&nbsp;{label}</span>'
                    f'{body}</div>')

        col_c, col_s, col_st = st.columns(3)
        with col_c:
            st.markdown(_report_card(report.get("continue", []),
                        "report-card-continue", "badge-continue", "Continue", "✓"),
                        unsafe_allow_html=True)
        with col_s:
            st.markdown(_report_card(report.get("stop", []),
                        "report-card-stop", "badge-stop", "Stop", "✕"),
                        unsafe_allow_html=True)
        with col_st:
            st.markdown(_report_card(report.get("start", []),
                        "report-card-start", "badge-start", "Start", "→"),
                        unsafe_allow_html=True)

    st.markdown("<div style='margin-top:1.5rem'></div>", unsafe_allow_html=True)

    # -------------------------------------------------------------------------
    # Section 3: Turn-by-turn detail (collapsed) — individual expander per turn
    # -------------------------------------------------------------------------
    def _esc(text: str) -> str:
        return text.replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")

    def _render_comparison_table(question, danny_response, alt, well_formed):
        wf_icon = "✅" if well_formed is True else ("🔴" if well_formed is False else "-")
        wf_label = "Yes" if well_formed is True else ("No" if well_formed is False else "N/A")
        orig_badges = f"Well-formed: {wf_icon} {wf_label}"

        alt_wf_badge = "✅" if alt.get("alt_is_well_formed", True) else "🔴"
        alt_wf_label = "Yes" if alt.get("alt_is_well_formed", True) else "No"
        verdict = alt.get("improvement_verdict", "")
        st.markdown(f"""
<table style="width:100%;border-collapse:collapse;font-size:0.87em;margin-top:6px;">
  <thead>
    <tr>
      <th style="width:50%;border:1px solid rgba(239,68,68,0.2);padding:6px 8px;background:rgba(239,68,68,0.06);color:#991b1b;font-size:0.7em;text-transform:uppercase;letter-spacing:0.07em;">Original</th>
      <th style="width:50%;border:1px solid rgba(16,185,129,0.2);padding:6px 8px;background:rgba(16,185,129,0.06);color:#065f46;font-size:0.7em;text-transform:uppercase;letter-spacing:0.07em;">Alternative</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td style="border:1px solid #e5e7eb;padding:7px 8px;vertical-align:top;"><strong>Question</strong><br>{_esc(question)}<br><small style="color:#9ca3af;">{orig_badges}</small></td>
      <td style="border:1px solid #e5e7eb;padding:7px 8px;vertical-align:top;"><strong>Question</strong><br>{_esc(alt['alternative_question'])}<br><small style="color:#9ca3af;">Well-formed: {alt_wf_badge} {alt_wf_label}</small></td>
    </tr>
    <tr>
      <td style="border:1px solid #e5e7eb;padding:7px 8px;vertical-align:top;color:#4b5563;"><strong>Client's response</strong><br>{_esc(danny_response)}</td>
      <td style="border:1px solid #e5e7eb;padding:7px 8px;vertical-align:top;color:#4b5563;"><strong>Simulated response</strong><br>{_esc(alt['simulated_response'])}</td>
    </tr>
  </tbody>
</table>
""", unsafe_allow_html=True)
        if verdict:
            st.caption(f"**Verdict:** {verdict}")

    st.markdown('<p class="turn-section-label">Turn-by-Turn Detail</p>', unsafe_allow_html=True)
    if not annotations:
        st.write("No consultant turns to display.")
    else:
        for ann in annotations:
            idx = ann.get("turn_index", "?")
            question = ann.get("question", "")
            turn_type = ann.get("turn_type", "question")
            well_formed = ann.get("is_well_formed")
            mistakes = ann.get("mistakes", [])
            icon = _turn_icon(turn_type, well_formed)
            danny_response = _get_client_response(idx)
            alt = alternatives.get(idx)

            q_short = (question[:80] + "…") if len(question) > 80 else question
            with st.expander(f"{icon} Turn {idx} [{turn_type}]: {q_short}", expanded=False):
                wf_icon = "✅" if well_formed is True else ("🔴" if well_formed is False else "-")
                wf_label = "Yes" if well_formed is True else ("No" if well_formed is False else "N/A")
                badges_md = f"**Well-formed:** {wf_icon} {wf_label}"

                if turn_type in ("acknowledgment", "explanation"):
                    st.caption(f"Not evaluated: {turn_type}")
                    st.markdown(f"**Consultant:** {question}")
                    if danny_response:
                        st.markdown(f"**Client:** {danny_response}")

                elif turn_type == "solution_proposal":
                    st.markdown(badges_md, unsafe_allow_html=True)
                    st.markdown(f"**Consultant:** {question}")
                    if danny_response:
                        st.markdown(f"**Client:** {danny_response}")

                elif turn_type == "unproductive_statement":
                    if mistakes:
                        expl = mistakes[0].get("explanation", "")
                        st.markdown(f"**Mistake:** `{mistakes[0]['mistake_type']}`: {expl}")
                    if alt:
                        _render_comparison_table(question, danny_response, alt, well_formed)
                    else:
                        st.markdown(badges_md, unsafe_allow_html=True)
                        st.markdown(f"**Consultant:** {question}")
                        if danny_response:
                            st.markdown(f"**Client:** {danny_response}")

                else:
                    # question turn
                    if well_formed is True:
                        st.markdown(badges_md, unsafe_allow_html=True)
                        st.markdown(f"**Consultant:** {question}")
                        if danny_response:
                            st.markdown(f"**Client:** {danny_response}")
                    else:
                        if mistakes:
                            expl = mistakes[0].get("explanation", "")
                            st.markdown(f"**Mistake:** `{mistakes[0]['mistake_type']}`: {expl}")
                        if alt:
                            _render_comparison_table(question, danny_response, alt, well_formed)
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
    page_title="Revelio",
    page_icon="🎯",
    layout="wide",
)

st.markdown("""
<style>
/* ---- Theme tokens ---- */
:root {
    --color-primary:        #337083;
    --color-primary-hover:  #285B6B;
    --color-primary-soft:   #EAF3F5;
    --color-border-subtle:  #D7E3E7;
    --color-page-bg:        #F8FAFB;
    --color-card-bg:        #FFFFFF;
    --color-text-main:      #1F2937;
    --color-text-secondary: #6B7280;
}

/* ---- Global layout ---- */
.stApp { background-color: var(--color-page-bg); }
.block-container { padding-top: 3rem !important; }

/* ---- Revelio branding ---- */
.revelio-header { margin-bottom: 0.5rem; line-height: 1; }
.revelio-title {
    font-size: 2.8rem;
    font-weight: 800;
    letter-spacing: -0.03em;
    color: var(--color-primary);
    line-height: 1.1;
    margin: 0;
    display: inline-block;
}
.revelio-tagline {
    font-size: 0.88rem;
    color: #9ca3af;
    letter-spacing: 0.04em;
    margin-top: 0.3rem;
    margin-bottom: 0;
}

/* ---- Briefing card (persona selection) ---- */
.briefing-card {
    background: var(--color-primary-soft);
    border: 1px solid var(--color-border-subtle);
    border-radius: 0.75rem;
    padding: 0.9rem 1.2rem 0.75rem;
    margin: 1.1rem 0 1.6rem;
}
.briefing-card-label {
    font-size: 0.68rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--color-primary);
    margin-bottom: 0.55rem;
}
.briefing-card-row {
    font-size: 0.875rem;
    line-height: 1.6;
    margin-bottom: 0.1rem;
    color: var(--color-text-main);
}
.briefing-card-row strong { color: var(--color-text-main); }

/* ---- Persona selection section ---- */
.section-heading {
    font-size: 1.35rem;
    font-weight: 700;
    color: var(--color-text-main);
    letter-spacing: -0.01em;
    margin: 0 0 0.2rem 0;
}
.section-caption {
    font-size: 0.82rem;
    color: #9ca3af;
    margin: 0 0 1.2rem 0;
}
.persona-card {
    background: var(--color-card-bg);
    border: 1px solid var(--color-border-subtle);
    border-radius: 0.75rem;
    padding: 1.1rem 1.25rem;
    min-height: 115px;
    margin-bottom: 10px;
    transition: border-color 0.15s ease, background 0.15s ease;
}
.persona-card:hover {
    background: var(--color-primary-soft);
    border-color: var(--color-primary);
}
.persona-name {
    font-size: 1.05rem;
    font-weight: 700;
    color: var(--color-primary);
    margin: 0 0 4px 0;
}
.persona-role { font-size: 0.875rem; color: var(--color-text-main); margin: 0 0 8px 0; }
.persona-hint { font-size: 0.78rem; color: #9ca3af; margin: 0; }

/* ---- Primary buttons — teal instead of Streamlit's default red ---- */
div[data-testid="stButton"] > button[kind="primary"],
div[data-testid="stBaseButton-primary"] {
    background-color: var(--color-primary) !important;
    border-color: var(--color-primary) !important;
    color: white !important;
}
div[data-testid="stButton"] > button[kind="primary"]:hover,
div[data-testid="stBaseButton-primary"]:hover {
    background-color: var(--color-primary-hover) !important;
    border-color: var(--color-primary-hover) !important;
}
div[data-testid="stButton"] > button[kind="primary"]:active,
div[data-testid="stBaseButton-primary"]:active {
    background-color: var(--color-primary-hover) !important;
    border-color: var(--color-primary-hover) !important;
}

/* ---- Links ---- */
a { color: var(--color-primary) !important; }
a:hover { color: var(--color-primary-hover) !important; }

/* ---- Input focus ---- */
input:focus, textarea:focus, [data-testid="stChatInput"] textarea:focus {
    border-color: var(--color-primary) !important;
    box-shadow: 0 0 0 1px var(--color-primary) !important;
    outline: none;
}

/* ---- Tabs active state ---- */
[data-testid="stTabs"] [aria-selected="true"] {
    color: var(--color-primary) !important;
    border-bottom-color: var(--color-primary) !important;
}

/* ---- Sidebar ---- */
.sidebar-label {
    font-size: 0.68rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--color-text-secondary);
    margin-bottom: 0.3rem;
    margin-top: 0.6rem;
}

/* ---- Eval stats strip ---- */
.eval-stats-strip {
    display: flex;
    gap: 0;
    align-items: stretch;
    padding: 0.85rem 1.25rem;
    background: var(--color-card-bg);
    border: 1px solid var(--color-border-subtle);
    border-radius: 0.75rem;
    margin-bottom: 1.25rem;
}
.eval-stat {
    flex: 1;
    display: flex;
    flex-direction: column;
    padding: 0 1.25rem;
    border-right: 1px solid var(--color-border-subtle);
}
.eval-stat:first-child { padding-left: 0; }
.eval-stat:last-child { border-right: none; }
.eval-stat-value { font-size: 1.9rem; font-weight: 800; line-height: 1; color: var(--color-text-secondary); }
.eval-stat-value-green { color: #059669; }
.eval-stat-value-amber { color: #d97706; }
.eval-stat-label { font-size: 0.7rem; color: #9ca3af; text-transform: uppercase; letter-spacing: 0.07em; margin-top: 0.25rem; }
.eval-stat-pct { font-size: 0.78rem; color: #d97706; font-weight: 600; margin-left: 0.3rem; }

/* ---- Eval summary pull-quote ---- */
.eval-summary {
    font-size: 0.92rem;
    color: var(--color-text-main);
    font-style: italic;
    padding: 0.65rem 1rem;
    border-left: 3px solid var(--color-primary);
    background: var(--color-primary-soft);
    border-radius: 0 0.5rem 0.5rem 0;
    margin-bottom: 1.1rem;
    line-height: 1.6;
}

/* ---- Report section cards ---- */
.report-card { border-radius: 0.75rem; padding: 1rem 1.1rem; border: 1px solid; min-height: 80px; background: var(--color-card-bg); }
.report-card-continue { background: rgba(16, 185, 129, 0.04); border-color: rgba(16, 185, 129, 0.22); }
.report-card-stop     { background: rgba(239, 68, 68, 0.04);  border-color: rgba(239, 68, 68, 0.22); }
.report-card-start    { background: rgba(245, 158, 11, 0.04);  border-color: rgba(245, 158, 11, 0.22); }
.report-card-badge {
    display: inline-block;
    font-size: 0.67rem; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.09em;
    padding: 0.18rem 0.55rem; border-radius: 9999px; margin-bottom: 0.65rem;
}
.badge-continue { background: rgba(16, 185, 129, 0.13); color: #059669; }
.badge-stop     { background: rgba(239, 68, 68, 0.13);  color: #dc2626; }
.badge-start    { background: rgba(245, 158, 11, 0.13); color: #d97706; }
.report-item {
    font-size: 0.86rem; color: var(--color-text-main); line-height: 1.6;
    margin-bottom: 0.55rem; padding-left: 0.85rem; position: relative;
}
.report-item::before { content: "·"; position: absolute; left: 0; color: #9ca3af; font-size: 1.1em; }
.report-item-empty { font-size: 0.82rem; color: #9ca3af; font-style: italic; }
.report-item-turn { font-size: 0.74rem; color: #9ca3af; font-style: italic; }

/* ---- Turn-by-turn section label ---- */
.turn-section-label {
    font-size: 0.68rem; font-weight: 700; text-transform: uppercase;
    letter-spacing: 0.1em; color: #9ca3af; margin: 0.5rem 0 0.5rem 0;
}

/* ---- Sidebar compact briefing ---- */
.sidebar-briefing { font-size: 0.78rem; line-height: 1.65; color: #4b5563; margin-top: 0.2rem; }
.sidebar-briefing-row { margin-bottom: 0.12rem; }
.sidebar-briefing-key { font-weight: 600; color: var(--color-text-main); }

/* ---- Print styles ---- */
@media print {
    header[data-testid="stHeader"],
    [data-testid="stToolbar"],
    [data-testid="stSidebar"],
    [data-testid="stChatInput"],
    [data-testid="stDownloadButton"],
    button { display: none !important; }

    .main .block-container {
        padding: 0 !important;
        max-width: 100% !important;
    }

    details { display: block !important; }
    details > *:not(summary) { display: block !important; }
    .stExpander { page-break-inside: avoid; }
}
</style>
""", unsafe_allow_html=True)

_init_session()
_render_sidebar()

if st.session_state.phase == "persona_selection":
    _render_persona_selection()
elif st.session_state.phase == "conversation":
    _render_header()
    st.divider()
    _render_conversation()
elif st.session_state.phase == "evaluating":
    _render_header()
    st.divider()
    _render_conversation()
    st.divider()
    _run_evaluation()
elif st.session_state.phase == "evaluation":
    _render_header()
    st.divider()
    _render_evaluation()
