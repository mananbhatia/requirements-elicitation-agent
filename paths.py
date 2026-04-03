"""
Centralised path resolution for the project.

All paths resolve relative to this file's location (project root),
so they work correctly regardless of the working directory — both
in local development and when deployed to Databricks Apps.

SESSION_LOG_DIR can be overridden via environment variable for deployment
environments where writing to the local filesystem is not appropriate.
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

DOCS_DIR            = ROOT / "docs"
SCENARIOS_DIR       = DOCS_DIR / "scenarios"
BEHAVIOR_RULES_FILE = DOCS_DIR / "behavior_rules.md"
MISTAKE_TYPES_FILE  = DOCS_DIR / "mistake_types.md"

# Write session logs to SESSION_LOG_DIR if set (deployment), else local logs/
SESSION_LOG_DIR = Path(os.environ.get("SESSION_LOG_DIR", str(ROOT / "logs")))
