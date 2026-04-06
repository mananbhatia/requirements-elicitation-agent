"""
Pipeline Orchestrator — Chains phases with human review checkpoints.

This module provides two entry points:
    1. run_from_notes(): Start from raw engagement notes
    2. run_from_scratch(): Start from parameters (no source notes)

Pipeline flow:
    Phase 0 (optional): Generate scenario from parameters
                        → saves phase1_extraction_output.json
    Phase 1:            Extract structured facts from raw notes
                        → saves phase1_extraction_output.json
    Phase 2 (optional): Anonymize identifying information
                        → saves phase2_anonymized_output.json
    Taxonomy:           Generate shared topic taxonomy from full extraction (once per scenario)
                        → saves scenario_taxonomy.json
    Phase 3:            Classify facts + Opus refinement + language rewrite + retag to taxonomy
                        → saves phase3_classified_{persona}.json
    Phase 3.5:          Completeness check + gap filling (Sonnet assess, Opus generate)
                        → saves phase3_5_completeness_{persona}.json
    Phase 4:            Character knowledge narrative generation (Opus)
                        → saves phase4_narrative_{persona}.md
    Phase 5:            Inference path validation with autofix loop
                        → saves phase5_validation_{persona}.json
    Phase 6:            Assembly of final scenario file
                        → saves scenario_assembled_{persona}.md (workspace)
                        → writes docs/scenarios/{name}_{persona}.md (draft)
    Phase 7:            Final review (dedup, revalidation, retag, checklist)
                        → saves scenario_final_reviewed_{persona}.md (workspace)
                        → overwrites docs/scenarios/{name}_{persona}.md (final)
                        → saves phase7_review_checklist_{persona}.md

Resume points: 2, 3, 35 (for 3.5), 4, 5, 6, 7

Design rationale:
    The human-in-the-loop is a feature, not a limitation. The thesis frames this
    as "AI-assisted scenario construction" — the LLM does the heavy lifting, but
    the domain expert validates classifications, enriches narratives, and catches
    unrealistic details. The prompts encode design principles (knowledge gating,
    aha-test, no inference paths) so the LLM's output is principled by default.

    The pipeline is NOT fully automated. Running all phases without review would
    produce a technically valid but potentially unrealistic scenario. The review
    steps are where domain expertise enters the process.

    Both Phase 6 (assembled) and Phase 7 (reviewed) outputs are preserved in the
    workspace for traceability. docs/scenarios/ always holds the most complete
    version — Phase 6 draft until Phase 7 runs, then the reviewed version.
"""

from pathlib import Path
from .config import ensure_workspace, save_phase_output, load_phase_output, WORKSPACE
from .phase0_generate import run_phase0
from .phase1_extract import run_phase1
from .phase2_anonymize import run_phase2
from .phase3_classify import run_phase3, run_scenario_taxonomy
from .phase3_5_completeness import run_phase3_5
from .phase4_narrate import run_phase4
from .phase5_validate import run_phase5, run_phase5_with_autofix
from .phase6_assemble import run_phase6, run_phase6_combine
from .phase7_review import run_phase7


def _prompt_continue(scenario_name: str, file_hint: str, next_phase: int) -> bool:
    """
    Pause for human review between phases.

    Prints the file to review, then waits for input.
    Returns True to continue, False to stop.
    """
    print(f"\n{'─' * 60}")
    print(f"  Review: {file_hint}")
    print(f"  Edit the file if needed, then:")
    print(f"    [Enter]  Continue to Phase {next_phase}")
    print(f"    [n]      Stop here")
    print(f"{'─' * 60}")
    try:
        answer = input("  > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n  Stopped.")
        return False
    if answer == "n":
        print(f"\n  Stopped. Resume later with:")
        print(f"    python -m scenario_generator.cli resume "
              f"--name {scenario_name} --phase {next_phase}")
        return False
    return True


def run_from_notes(
    notes_path: str,
    scenario_name: str,
    anonymize: bool = False,
    interview_stage: str = "initial_discovery",
    persona_name: str = "Danny",
    persona_role: str = "manager of the data platform team",
    persona_maturity: str = "LOW",
    auto_run: bool = False,
    interactive: bool = False,
    personas: list = None,
) -> None:
    """
    Run the full pipeline starting from raw engagement notes.

    Args:
        notes_path: Path to the raw engagement notes text file.
        scenario_name: Identifier for this scenario run.
        anonymize: Whether to run Phase 2 (anonymization).
        persona_name: Single-persona name (ignored if personas list is provided).
        persona_role: Single-persona role (ignored if personas list is provided).
        persona_maturity: Single-persona maturity (ignored if personas list is provided).
        auto_run: If True, run all phases without pausing. Use for testing only.
                  For real scenario construction, always review between phases.
        interactive: If True, pause between phases for human review.
        personas: Optional list of persona configs for multi-persona generation.
                  Each entry: {'name': str, 'role': str, 'maturity': str}.
                  If provided, Phase 3-7 runs once per persona, then Phase 6 combine
                  produces a single multi-persona scenario file.
                  If None, falls back to single-persona behavior using persona_name/role/maturity.
    """
    # Normalise to a list — single-persona is just a list of one
    if personas is None:
        personas = [{"name": persona_name, "role": persona_role, "maturity": persona_maturity}]

    ws = ensure_workspace(scenario_name)
    print(f"=== Scenario Generator: {scenario_name} ===")
    print(f"Workspace: {ws}")
    persona_summary = ", ".join(f"{p['name']} ({p['maturity']})" for p in personas)
    print(f"Personas: {persona_summary}")
    print(f"Starting from notes: {notes_path}\n")

    # Phase 1: Extraction (runs once — shared across all personas)
    print("=" * 60)
    print("PHASE 1: Structured Extraction")
    print("=" * 60)
    notes_text = Path(notes_path).read_text()
    run_phase1(notes_text, scenario_name, interview_stage=interview_stage)

    if not auto_run and not interactive:
        print("\n>>> Review and edit phase1_extraction_output.json, then call one per persona:")
        source = "--phase 2" if anonymize else "--phase 3"
        for p in personas:
            print(f"    python -m scenario_generator.cli resume --name {scenario_name} "
                  f"{source} --persona-name {p['name']} --persona-maturity {p['maturity']}")
        if len(personas) > 1:
            print(f"\n  After all personas complete Phase 7, run the combine step:")
            print(f"    from scenario_generator.pipeline import combine_personas")
            print(f"    combine_personas('{scenario_name}', personas)")
        return

    next_phase = 2 if anonymize else 3
    if interactive:
        ws_path = WORKSPACE / scenario_name
        file_hint = str(ws_path / "phase1_extraction_output.json")
        if not _prompt_continue(scenario_name, file_hint, next_phase):
            return

    # Phase 2 (optional): Anonymization (runs once — shared)
    if anonymize:
        print("\n" + "=" * 60)
        print("PHASE 2: Anonymization")
        print("=" * 60)
        run_phase2(scenario_name)
        if interactive:
            ws_path = WORKSPACE / scenario_name
            if not _prompt_continue(scenario_name, str(ws_path / "phase2_anonymized_output.json"), 3):
                return

    source_phase = "phase2_anonymized" if anonymize else "phase1_extraction"

    # Scenario Taxonomy: generate once from full extraction, shared across all personas
    print("\n" + "=" * 60)
    print("SCENARIO TAXONOMY: Shared Topic Taxonomy")
    print("=" * 60)
    run_scenario_taxonomy(scenario_name, source_phase=source_phase)

    # Phases 3–7: run once per persona
    for p in personas:
        if len(personas) > 1:
            print(f"\n{'═' * 60}")
            print(f"  Starting persona: {p['name']} ({p['role']}, maturity: {p['maturity']})")
            print(f"{'═' * 60}")
        _run_phases_3_to_7(
            scenario_name, p["name"], p["role"], p["maturity"],
            auto_run, interactive, source_phase=source_phase,
        )

    # Combine step: only runs when all personas complete Phase 7
    if auto_run or interactive:
        if len(personas) > 1:
            print("\n" + "=" * 60)
            print("PHASE 6 COMBINE: Multi-Persona Assembly")
            print("=" * 60)
        combine_personas(scenario_name, personas)


def run_from_scratch(
    scenario_name: str,
    company_name: str,
    industry: str,
    platform_maturity: str = "LOW",
    engagement_type: str = "platform_review",
    interview_stage: str = "initial_discovery",
    cloud_platform: str = "azure",
    problem_clusters: list = None,
    team_size: str = "5-6 FTE",
    persona_name: str = "Danny",
    persona_role: str = "manager of the data platform team",
    persona_maturity: str = "LOW",
    auto_run: bool = False,
    interactive: bool = False,
    personas: list = None,
) -> None:
    """
    Run the full pipeline starting from parameters (no source notes).

    Args:
        scenario_name: Identifier for this scenario run.
        company_name: Fictional company name.
        industry: Industry/sector.
        platform_maturity: LOW, MEDIUM, MEDIUM_HIGH, or HIGH — the org's data platform state.
        engagement_type: Type of engagement.
        cloud_platform: azure, aws, or gcp.
        problem_clusters: List of problem areas.
        team_size: Approximate team size.
        persona_name: Single-persona name (ignored if personas list is provided).
        persona_role: Single-persona role (ignored if personas list is provided).
        persona_maturity: Single-persona maturity (ignored if personas list is provided).
        auto_run: If True, run without pausing. Testing only.
        interactive: If True, pause between phases for human review.
        personas: Optional list of persona configs for multi-persona generation.
                  Each entry: {'name': str, 'role': str, 'maturity': str}.
    """
    if problem_clusters is None:
        problem_clusters = ["iam", "governance"]

    # Normalise to a list
    if personas is None:
        personas = [{"name": persona_name, "role": persona_role, "maturity": persona_maturity}]

    # Phase 0 uses the first persona's maturity for note generation context
    first_persona_maturity = personas[0]["maturity"]
    first_persona_role = personas[0]["role"]

    ws = ensure_workspace(scenario_name)
    print(f"=== Scenario Generator: {scenario_name} (from scratch) ===")
    print(f"Workspace: {ws}")
    persona_summary = ", ".join(f"{p['name']} ({p['maturity']})" for p in personas)
    print(f"Personas: {persona_summary}")
    print(f"Parameters: {company_name} / {industry} / platform_maturity={platform_maturity}\n")

    # Phase 0: Generation (runs once — produces shared extraction)
    print("=" * 60)
    print("PHASE 0: From-Scratch Generation")
    print("=" * 60)
    run_phase0(
        company_name=company_name,
        industry=industry,
        platform_maturity=platform_maturity,
        engagement_type=engagement_type,
        interview_stage=interview_stage,
        cloud_platform=cloud_platform,
        problem_clusters=problem_clusters,
        team_size=team_size,
        persona_role=first_persona_role,
        scenario_name=scenario_name,
        persona_maturity=first_persona_maturity,
    )

    if not auto_run and not interactive:
        print("\n>>> Review and edit phase1_extraction_output.json, then call one per persona:")
        for p in personas:
            print(f"    python -m scenario_generator.cli resume --name {scenario_name} "
                  f"--phase 3 --persona-name {p['name']} --persona-maturity {p['maturity']}")
        if len(personas) > 1:
            print(f"\n  After all personas complete Phase 7, run the combine step:")
            print(f"    from scenario_generator.pipeline import combine_personas")
            print(f"    combine_personas('{scenario_name}', personas)")
        return

    if interactive:
        ws_path = WORKSPACE / scenario_name
        if not _prompt_continue(scenario_name, str(ws_path / "phase1_extraction_output.json"), 3):
            return

    # Scenario Taxonomy: generate once from full extraction, shared across all personas
    print("\n" + "=" * 60)
    print("SCENARIO TAXONOMY: Shared Topic Taxonomy")
    print("=" * 60)
    run_scenario_taxonomy(scenario_name)

    # Phases 3–7: run once per persona
    for p in personas:
        if len(personas) > 1:
            print(f"\n{'═' * 60}")
            print(f"  Starting persona: {p['name']} ({p['role']}, maturity: {p['maturity']})")
            print(f"{'═' * 60}")
        _run_phases_3_to_7(
            scenario_name, p["name"], p["role"], p["maturity"],
            auto_run, interactive,
        )

    # Combine step
    if auto_run or interactive:
        if len(personas) > 1:
            print("\n" + "=" * 60)
            print("PHASE 6 COMBINE: Multi-Persona Assembly")
            print("=" * 60)
        combine_personas(scenario_name, personas)


def resume(
    scenario_name: str,
    from_phase: int,
    persona_name: str = "Danny",
    persona_role: str = "manager of the data platform team",
    persona_maturity: str = "LOW",
    source_phase: str = None,
    auto_run: bool = False,
    interactive: bool = False,
) -> None:
    """
    Resume the pipeline from a specific phase.

    Use this after reviewing and editing intermediate outputs.

    Args:
        scenario_name: Identifier for this scenario run.
        from_phase: Phase number to resume from (2, 3, 35, 4, 5, 6, 7).
                    Use 35 to resume from Phase 3.5.
        persona_name: Name of the persona.
        persona_role: Role description.
        source_phase: Override the source phase for Phase 3 input.
        auto_run: If True, run without pausing. Testing only.
    """
    ws = WORKSPACE / scenario_name

    if from_phase == 2:
        print("=" * 60)
        print("PHASE 2: Anonymization")
        print("=" * 60)
        run_phase2(scenario_name)
        if not auto_run and not interactive:
            print("\n>>> Review phase2_anonymized_output.json, then call:")
            print(f"    python -m scenario_generator.cli resume --name {scenario_name} "
                  f"--phase 3 --source-phase phase2_anonymized --persona-name {persona_name}")
            return
        if interactive and not _prompt_continue(scenario_name, str(ws / "phase2_anonymized_output.json"), 3):
            return
        source_phase = "phase2_anonymized"
        from_phase = 3

    if from_phase == 3:
        if source_phase is None:
            source_phase = "phase1_extraction"
        # Ensure scenario taxonomy exists — generate if missing (e.g. resuming mid-pipeline)
        if not (WORKSPACE / scenario_name / "scenario_taxonomy.json").exists():
            print("\n" + "=" * 60)
            print("SCENARIO TAXONOMY: Shared Topic Taxonomy (auto-generating)")
            print("=" * 60)
            run_scenario_taxonomy(scenario_name, source_phase=source_phase)
        print("\n" + "=" * 60)
        print("PHASE 3: Knowledge Classification")
        print("=" * 60)
        run_phase3(
            scenario_name,
            source_phase=source_phase,
            persona_name=persona_name,
            persona_role=persona_role,
            persona_maturity=persona_maturity,
        )
        classified_filename = f"phase3_classified_{persona_name.lower()}.json"
        if not auto_run and not interactive:
            print(f"\n>>> Review {classified_filename}, then call:")
            print(f"    python -m scenario_generator.cli resume --name {scenario_name} --phase 35 --persona-name {persona_name}")
            return
        if interactive and not _prompt_continue(scenario_name, str(ws / classified_filename), 35):
            return
        from_phase = 35

    if from_phase == 35:
        print("\n" + "=" * 60)
        print("PHASE 3.5: Completeness Check and Gap Fill")
        print("=" * 60)
        run_phase3_5(scenario_name, persona_name=persona_name)
        completeness_filename = f"phase3_5_completeness_{persona_name.lower()}.json"
        if not auto_run and not interactive:
            print(f"\n>>> Review {completeness_filename} (check [generated] facts), then call:")
            print(f"    python -m scenario_generator.cli resume --name {scenario_name} --phase 4 --persona-name {persona_name}")
            return
        if interactive and not _prompt_continue(scenario_name, str(ws / completeness_filename), 4):
            return
        from_phase = 4

    if from_phase == 4:
        print("\n" + "=" * 60)
        print("PHASE 4: Character Narrative Generation")
        print("=" * 60)
        run_phase4(scenario_name, persona_name=persona_name)
        narrative_filename = f"phase4_narrative_{persona_name.lower()}.md"
        if not auto_run and not interactive:
            print(f"\n>>> Review {narrative_filename}, then call:")
            print(f"    python -m scenario_generator.cli resume --name {scenario_name} --phase 5 --persona-name {persona_name}")
            return
        if interactive and not _prompt_continue(scenario_name, str(ws / narrative_filename), 5):
            return
        from_phase = 5

    if from_phase == 5:
        print("\n" + "=" * 60)
        print("PHASE 5: Inference Path Validation")
        print("=" * 60)
        run_phase5_with_autofix(scenario_name, persona_name=persona_name)
        validation_filename = f"phase5_validation_{persona_name.lower()}.json"
        if not auto_run and not interactive:
            print(f"\n>>> When satisfied, call:")
            print(f"    python -m scenario_generator.cli resume --name {scenario_name} --phase 6 --persona-name {persona_name}")
            return
        if interactive and not _prompt_continue(scenario_name, str(ws / validation_filename), 6):
            return
        from_phase = 6

    if from_phase == 6:
        print("\n" + "=" * 60)
        print("PHASE 6: Assembly")
        print("=" * 60)
        run_phase6(scenario_name, persona_name, persona_role)
        assembled_filename = f"scenario_assembled_{persona_name.lower()}.md"
        if not auto_run and not interactive:
            print(f"\n>>> Review the assembled scenario, then call:")
            print(f"    python -m scenario_generator.cli resume --name {scenario_name} --phase 7 --persona-name {persona_name}")
            return
        if interactive and not _prompt_continue(scenario_name, str(ws / assembled_filename), 7):
            return
        from_phase = 7

    if from_phase == 7:
        print("\n" + "=" * 60)
        print("PHASE 7: Final Review")
        print("=" * 60)
        run_phase7(scenario_name, persona_name)
        print("\n=== Pipeline complete ===")


def _run_phases_3_to_7(
    scenario_name: str,
    persona_name: str,
    persona_role: str,
    persona_maturity: str,
    auto_run: bool,
    interactive: bool = False,
    source_phase: str = "phase1_extraction",
) -> None:
    """Run Phases 3 through 7 in sequence for a single persona."""
    resume(
        scenario_name,
        from_phase=3,
        persona_name=persona_name,
        persona_role=persona_role,
        persona_maturity=persona_maturity,
        source_phase=source_phase,
        auto_run=auto_run,
        interactive=interactive,
    )


def combine_personas(scenario_name: str, personas: list) -> str:
    """
    Combine per-persona Phase 7 outputs into a single multi-persona scenario file.

    Call this after all personas have completed their individual Phase 7 runs.
    This is the final step in a multi-persona pipeline run.

    Args:
        scenario_name: Identifier for the scenario.
        personas: List of persona configs: [{'name': str, 'role': str, 'maturity': str}, ...]

    Returns:
        The combined scenario markdown string.

    Example:
        personas = [
            {'name': 'Danny', 'role': 'manager of the data platform team', 'maturity': 'LOW'},
            {'name': 'Sajith', 'role': 'solutions architect for the data platform', 'maturity': 'HIGH'},
        ]
        combine_personas('my_scenario', personas)
    """
    print(f"=== Combining {len(personas)} persona(s) for scenario '{scenario_name}' ===")
    for p in personas:
        print(f"  - {p['name']} ({p['role']}, maturity: {p['maturity']})")
    print()
    return run_phase6_combine(scenario_name, personas)
