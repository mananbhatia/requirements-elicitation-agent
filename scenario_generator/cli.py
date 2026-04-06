#!/usr/bin/env python3
"""
Command-line interface for the Scenario Generator pipeline.

Usage examples:

    # From raw engagement notes:
    python -m scenario_generator.cli from-notes \\
        --notes path/to/notes.txt \\
        --name example_scenario \\
        --persona-name Danny \\
        --persona-maturity LOW

    # From scratch (no notes):
    python -m scenario_generator.cli from-scratch \\
        --name example_scenario \\
        --company "NovaTech Solutions" \\
        --industry "financial services" \\
        --platform-maturity LOW \\
        --persona-maturity LOW \\
        --engagement platform_review \\
        --cloud azure \\
        --problems iam governance security

    # Resume from a specific phase:
    python -m scenario_generator.cli resume \\
        --name example_scenario \\
        --phase 3 \\
        --persona-maturity LOW

    # Run all phases without pausing (testing only):
    python -m scenario_generator.cli from-notes \\
        --notes path/to/notes.txt \\
        --name test_run \\
        --auto
"""

import argparse
import sys


def main():
    parser = argparse.ArgumentParser(
        description="Scenario Generator — AI-assisted training scenario construction"
    )
    subparsers = parser.add_subparsers(dest="command", help="Pipeline entry point")

    # --- from-notes ---
    notes_parser = subparsers.add_parser(
        "from-notes", help="Start from raw engagement notes"
    )
    notes_parser.add_argument("--notes", required=True, help="Path to notes file")
    notes_parser.add_argument("--name", required=True, help="Scenario name")
    notes_parser.add_argument("--anonymize", action="store_true", help="Run anonymization")
    notes_parser.add_argument("--persona-name", default="Danny", help="Persona name")
    notes_parser.add_argument("--persona-role", default="manager of the data platform team",
                              help="Persona role description")
    notes_parser.add_argument("--persona-maturity", default="LOW", choices=["LOW", "MEDIUM", "MEDIUM_HIGH", "HIGH"],
                              help="How technically knowledgeable the simulated persona is")
    notes_parser.add_argument("--auto", action="store_true", help="Run all phases without pausing (testing)")
    notes_parser.add_argument("--interactive", action="store_true", help="Run all phases with review prompts between each")

    # --- from-scratch ---
    scratch_parser = subparsers.add_parser(
        "from-scratch", help="Generate scenario from parameters"
    )
    scratch_parser.add_argument("--name", required=True, help="Scenario name")
    scratch_parser.add_argument("--company", required=True, help="Company name")
    scratch_parser.add_argument("--industry", required=True, help="Industry")
    scratch_parser.add_argument("--platform-maturity", default="LOW", choices=["LOW", "MEDIUM", "MEDIUM_HIGH", "HIGH"],
                                help="The organisation's data platform maturity state")
    scratch_parser.add_argument("--persona-maturity", default="LOW", choices=["LOW", "MEDIUM", "MEDIUM_HIGH", "HIGH"],
                                help="How technically knowledgeable the simulated persona is")
    scratch_parser.add_argument("--engagement", default="platform_review")
    scratch_parser.add_argument("--cloud", default="azure", choices=["azure", "aws", "gcp"])
    scratch_parser.add_argument("--problems", nargs="+", default=["iam", "governance"])
    scratch_parser.add_argument("--team-size", default="5-6 FTE")
    scratch_parser.add_argument("--persona-name", default="Danny")
    scratch_parser.add_argument("--persona-role", default="manager of the data platform team")
    scratch_parser.add_argument("--auto", action="store_true", help="Run all phases without pausing (testing)")
    scratch_parser.add_argument("--interactive", action="store_true", help="Run all phases with review prompts between each")

    # --- resume ---
    resume_parser = subparsers.add_parser(
        "resume", help="Resume from a specific phase"
    )
    resume_parser.add_argument("--name", required=True, help="Scenario name")
    resume_parser.add_argument("--phase", required=True, type=int, help="Phase to resume from (3, 35, 4, 5, 6, 7)")
    resume_parser.add_argument("--source-phase", default=None, help="Override source phase")
    resume_parser.add_argument("--persona-name", default="Danny")
    resume_parser.add_argument("--persona-role", default="manager of the data platform team")
    resume_parser.add_argument("--persona-maturity", default="LOW", choices=["LOW", "MEDIUM", "MEDIUM_HIGH", "HIGH"],
                               help="How technically knowledgeable the simulated persona is")
    resume_parser.add_argument("--auto", action="store_true", help="Run remaining phases without pausing (testing)")
    resume_parser.add_argument("--interactive", action="store_true", help="Run remaining phases with review prompts between each")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    from .pipeline import run_from_notes, run_from_scratch, resume

    if args.command == "from-notes":
        run_from_notes(
            notes_path=args.notes,
            scenario_name=args.name,
            anonymize=args.anonymize,
            persona_name=args.persona_name,
            persona_role=args.persona_role,
            persona_maturity=args.persona_maturity,
            auto_run=args.auto,
            interactive=args.interactive,
        )

    elif args.command == "from-scratch":
        run_from_scratch(
            scenario_name=args.name,
            company_name=args.company,
            industry=args.industry,
            platform_maturity=args.platform_maturity,
            engagement_type=args.engagement,
            cloud_platform=args.cloud,
            problem_clusters=args.problems,
            team_size=args.team_size,
            persona_name=args.persona_name,
            persona_role=args.persona_role,
            persona_maturity=args.persona_maturity,
            auto_run=args.auto,
            interactive=args.interactive,
        )

    elif args.command == "resume":
        resume(
            scenario_name=args.name,
            from_phase=args.phase,
            persona_name=args.persona_name,
            persona_role=args.persona_role,
            persona_maturity=args.persona_maturity,
            source_phase=args.source_phase,
            auto_run=args.auto,
            interactive=args.interactive,
        )


if __name__ == "__main__":
    main()
