"""
Replay a completed session log in the Streamlit UI.

Loads a session_*.json log file (as written by session_logger.save_session)
and launches streamlit_app.py directly into the evaluation phase, with
session state populated from the log instead of a live conversation. No
graph or LLM calls are made — the transcript, turn annotations, alternatives
and report are all read straight from the file, so the rendered page is
identical to what the consultant saw at the end of the real session.

Usage:
    python replay_session.py logs/session_2026-04-08_14-38-17.json
"""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import SESSION_LOG_DIR


def main():
    if len(sys.argv) != 2:
        print(f"Usage: python {Path(__file__).name} <path-to-session-log.json>")
        sys.exit(1)

    log_path = Path(sys.argv[1])
    if not log_path.is_absolute():
        # Allow either a full path or a bare filename relative to SESSION_LOG_DIR
        candidates = [log_path, SESSION_LOG_DIR / log_path]
        log_path = next((c for c in candidates if c.exists()), log_path)

    if not log_path.exists():
        print(f"Session log not found: {log_path}")
        sys.exit(1)

    env = {**os.environ, "REPLAY_SESSION_LOG": str(log_path.resolve())}

    subprocess.run(
        [sys.executable, "-m", "streamlit", "run",
         str(Path(__file__).resolve().parent / "streamlit_app.py")],
        env=env,
    )


if __name__ == "__main__":
    main()
