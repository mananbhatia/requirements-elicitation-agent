"""
Scenario Generator — AI-assisted scenario construction pipeline.

This package implements a phased pipeline for constructing training scenarios
for a synthetic client simulation system. Each phase transforms structured data
through an LLM call with human review checkpoints between phases.

Phases:
    0 (optional): Generate scenario from parameters (no source notes)
    1:            Extract structured facts from raw engagement notes
    2 (optional): Anonymize identifying information
    3:            Classify facts into character knowledge vs discovery items
    3.5:          Completeness check and gap fill (Sonnet assess + Opus generate)
    4:            Generate character knowledge narrative
    5:            Validate narrative against discovery items for inference paths
    6:            Assemble into final scenario file
    7:            Final review (dedup, revalidation, retag, checklist)

Usage:
    from scenario_generator import pipeline
    pipeline.run_from_notes("path/to/notes.txt", "my_scenario")
    # Then review/edit intermediate outputs between phases
"""
