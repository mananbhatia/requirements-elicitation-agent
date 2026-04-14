"""
Focused tests for the embedding-based retrieval system in knowledge_new.py.

Tests are grouped by what they cover:
  - structural_check: verb/question-word detection
  - intent_check: acknowledgment/catch-all/topic-reference detection
  - needs_context: referential question detection
  - CK parsing: Character Knowledge → CK-01, CK-02, ... ScenarioItems
  - EmbeddingStore._query_vec: thresholding and sorting without API calls
  - retrieve_relevant_knowledge smoke test (requires VOYAGE_API_KEY)

Run with:
    python -m pytest tests/test_embedding_retrieval.py -v
    python -m pytest tests/test_embedding_retrieval.py -v -k "not smoke"  # skip API tests
"""

import os
import sys
from pathlib import Path

import numpy as np
import pytest

# Make sure the parent directory is on sys.path when running as a script.
sys.path.insert(0, str(Path(__file__).parent.parent))

from knowledge import (
    structural_check,
    intent_check,
    needs_context,
    EmbeddingStore,
    load_scenario,
    build_retrieval_index,
    retrieve_relevant_knowledge,
)

SCENARIO_PATH = Path(__file__).parent.parent / "docs" / "scenarios" / "waste_management.md"
VOYAGE_AVAILABLE = bool(os.environ.get("VOYAGE_API_KEY"))


# ---------------------------------------------------------------------------
# structural_check
# ---------------------------------------------------------------------------

class TestStructuralCheck:
    def test_bare_noun_fails(self):
        assert structural_check("SCIM") is False

    def test_bare_noun_with_question_mark_fails(self):
        assert structural_check("SCIM?") is False

    def test_single_topic_word_fails(self):
        assert structural_check("clusters?") is False

    def test_object_ownership_fails(self):
        assert structural_check("object ownership?") is False

    def test_hub_and_spoke_fails(self):
        assert structural_check("hub and spoke") is False

    def test_verb_present_passes(self):
        assert structural_check("How are users added?") is True

    def test_question_word_passes(self):
        assert structural_check("What is the process?") is True

    def test_auxiliary_verb_passes(self):
        assert structural_check("Is there a firewall?") is True

    def test_how_does_passes(self):
        assert structural_check("How does access work here?") is True

    def test_can_you_passes(self):
        assert structural_check("Can you walk me through the setup?") is True

    def test_who_passes(self):
        assert structural_check("Who manages access approvals?") is True


# ---------------------------------------------------------------------------
# intent_check
# ---------------------------------------------------------------------------

class TestIntentCheck:
    def test_acknowledgment_fails(self):
        assert intent_check("okay") is False

    def test_ok_fails(self):
        assert intent_check("ok") is False

    def test_got_it_fails(self):
        assert intent_check("got it") is False

    def test_that_is_interesting_fails(self):
        assert intent_check("that's interesting") is False

    def test_tell_me_more_fails(self):
        assert intent_check("tell me more") is False

    def test_anything_else_fails(self):
        assert intent_check("anything else?") is False

    def test_share_more_fails(self):
        assert intent_check("share more") is False

    def test_what_about_single_word_fails(self):
        # "What about clusters?" — topic reference
        assert intent_check("What about clusters?") is False

    def test_what_about_two_words_fails(self):
        # "What about user provisioning?" — still a topic reference
        assert intent_check("What about user provisioning?") is False

    def test_genuine_question_passes(self):
        assert intent_check("How are users added to the platform?") is True

    def test_specific_question_passes(self):
        assert intent_check("Is there an automated process for removing access when someone leaves?") is True

    def test_what_about_multi_word_passes(self):
        # "What about the way users are provisioned?" has >2 words after "about" → passes
        assert intent_check("What about the way users are provisioned?") is True

    def test_single_word_fails(self):
        assert intent_check("SCIM") is False


# ---------------------------------------------------------------------------
# needs_context
# ---------------------------------------------------------------------------

class TestNeedsContext:
    # --- Genuinely referential: should return True ---

    def test_subject_pronoun_it(self):
        # "Is it" at start — "it" has no local antecedent
        assert needs_context("Is it manual?") is True

    def test_subject_pronoun_that(self):
        # "Is that" at start — refers to something from prior context
        assert needs_context("Is that done automatically?") is True

    def test_subject_pronoun_they(self):
        # "Do they" at start — no named subject in this question
        assert needs_context("Do they handle access requests?") is True

    def test_subject_pronoun_how_is_that(self):
        # "How is that" at start — "that" is subject-position with no local antecedent
        assert needs_context("How is that usually handled?") is True

    def test_subject_pronoun_does_it(self):
        assert needs_context("Does it apply to the production environment?") is True

    def test_subject_pronoun_are_they(self):
        assert needs_context("Are they using single sign-on?") is True

    def test_follow_up_opener_and(self):
        assert needs_context("And what about the approval process?") is True

    def test_follow_up_opener_but(self):
        assert needs_context("But is that enforced at the workspace level?") is True

    def test_very_short_question(self):
        # 3 words — too short to be self-contained, likely a follow-up
        assert needs_context("Is it live?") is True

    def test_very_short_four_words(self):
        # 4 words — still short enough to trigger
        assert needs_context("Who manages that then?") is True

    # --- Self-contained with pronouns: should return False ---

    def test_self_contained_compound_with_that(self):
        # "that" appears mid-sentence; "How are users added" establishes the topic locally
        assert needs_context(
            "How are users added to the Databricks platform — is that done automatically or manually?"
        ) is False

    def test_self_contained_compound_with_it(self):
        # "it" refers to "your Databricks environment" stated in the same sentence
        assert needs_context(
            "Is your Databricks environment accessible over the public internet, or is it on a private network?"
        ) is False

    def test_self_contained_compound_how_is_that(self):
        # "that" refers to "access to data" named earlier in the same question
        assert needs_context(
            "How is access to data controlled — who can see what, and how is that managed?"
        ) is False

    def test_self_contained_no_pronouns(self):
        assert needs_context(
            "How are users provisioned into Databricks — do you sync from your company identity system?"
        ) is False

    def test_self_contained_no_referential_words(self):
        assert needs_context("Who approves access requests on your platform?") is False

    def test_self_contained_is_there(self):
        # "Is there" is not aux + pronoun — self-contained question
        assert needs_context("Is there a process for removing access when someone leaves?") is False

    def test_self_contained_five_words_named_subject(self):
        # 5 words, starts with "How do" but next word is "users" not a pronoun
        assert needs_context("How do users request access?") is False


# ---------------------------------------------------------------------------
# CK parsing from waste_management.md
# ---------------------------------------------------------------------------

class TestCharacterKnowledgeParsing:
    """Tests for CK paragraph extraction in _load_multi_persona_new."""

    @pytest.fixture(scope="class")
    def scenario(self):
        if not SCENARIO_PATH.exists():
            pytest.skip(f"Scenario file not found: {SCENARIO_PATH}")
        return load_scenario(str(SCENARIO_PATH), persona="Danny")

    def test_ck_items_exist(self, scenario):
        assert len(scenario.character_knowledge) > 0, "Expected CK items to be parsed"

    def test_ck_ids_sequential(self, scenario):
        ids = [item.id for item in scenario.character_knowledge]
        assert ids[0] == "CK-01"
        assert ids[1] == "CK-02"

    def test_ck_no_markdown_headers(self, scenario):
        # No CK item should be just a markdown header line
        for item in scenario.character_knowledge:
            assert not item.content.startswith("#"), (
                f"CK item {item.id} is a raw markdown header: {item.content!r}"
            )

    def test_ck_no_topic_tags_in_content(self, scenario):
        # [topic: ...] tags must be stripped from content
        for item in scenario.character_knowledge:
            assert "[topic:" not in item.content, (
                f"CK item {item.id} still contains a topic tag: {item.content!r}"
            )

    def test_ck_content_nonempty(self, scenario):
        for item in scenario.character_knowledge:
            assert item.content.strip(), f"CK item {item.id} has empty content"

    def test_ck_some_have_topics(self, scenario):
        topics = [item.topic for item in scenario.character_knowledge if item.topic]
        assert len(topics) > 0, "Expected at least some CK items to have topic codes"

    def test_di_items_exist(self, scenario):
        assert len(scenario.discovery_items) > 0

    def test_di_ids_are_di_format(self, scenario):
        for item in scenario.discovery_items:
            assert item.id.startswith("DI-"), f"Expected DI-XX format, got {item.id!r}"

    def test_character_text_no_ck_section(self, scenario):
        # Character Knowledge section should NOT appear verbatim in character_text
        # (it was extracted into CK items instead)
        assert "#### Organizational History" not in scenario.character_text
        assert "#### Strategic Context" not in scenario.character_text


# ---------------------------------------------------------------------------
# EmbeddingStore._query_vec (no API calls)
# ---------------------------------------------------------------------------

class TestEmbeddingStoreQuery:
    """Test thresholding and ranking using _from_arrays / _query_vec."""

    def _make_store(self):
        """Three unit vectors at known angles."""
        embeddings = np.array([
            [1.0, 0.0, 0.0],  # id="A" — along x
            [0.0, 1.0, 0.0],  # id="B" — along y
            [0.6, 0.8, 0.0],  # id="C" — at ~53° from x (cos=0.6)
        ], dtype=np.float32)
        return EmbeddingStore._from_arrays(["A", "B", "C"], embeddings)

    def test_query_returns_above_threshold(self):
        store = self._make_store()
        qvec = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        results = store._query_vec(qvec, top_k=3, threshold=0.5)
        ids = [id_ for id_, _ in results]
        # A has cos=1.0, C has cos≈0.6 — both above 0.5
        # B has cos=0.0 — below 0.5
        assert "A" in ids
        assert "C" in ids
        assert "B" not in ids

    def test_query_sorted_descending(self):
        store = self._make_store()
        qvec = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        results = store._query_vec(qvec, top_k=3, threshold=-1.0)
        scores = [score for _, score in results]
        assert scores == sorted(scores, reverse=True)

    def test_query_top_k_caps_results(self):
        store = self._make_store()
        qvec = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        results = store._query_vec(qvec, top_k=1, threshold=-1.0)
        assert len(results) == 1
        assert results[0][0] == "A"

    def test_query_threshold_excludes_all(self):
        store = self._make_store()
        qvec = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        results = store._query_vec(qvec, top_k=3, threshold=0.99)
        # Only A (cos=1.0) passes threshold=0.99
        assert len(results) == 1
        assert results[0][0] == "A"

    def test_empty_store_returns_empty(self):
        store = EmbeddingStore._from_arrays([], np.empty((0, 3), dtype=np.float32))
        qvec = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        results = store._query_vec(qvec, top_k=5, threshold=0.0)
        assert results == []

    def test_score_values_in_range(self):
        store = self._make_store()
        qvec = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        results = store._query_vec(qvec, top_k=3, threshold=-1.0)
        for _, score in results:
            assert -1.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# retrieve_relevant_knowledge smoke test (requires VOYAGE_API_KEY)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not VOYAGE_AVAILABLE, reason="VOYAGE_API_KEY not set")
class TestRetrieveSmokeTest:
    """
    End-to-end smoke test with real embeddings.
    Verifies the function returns the right shape and respects already_revealed_ids.
    Does NOT assert which specific items are returned — thresholds need calibration.
    """

    @pytest.fixture(scope="class")
    def scenario_and_indices(self):
        if not SCENARIO_PATH.exists():
            pytest.skip(f"Scenario file not found: {SCENARIO_PATH}")
        scenario = load_scenario(str(SCENARIO_PATH), persona="Danny")
        char_index, disc_index = build_retrieval_index(scenario)
        return scenario, char_index, disc_index

    def test_genuine_question_returns_tuple(self, scenario_and_indices):
        scenario, char_index, disc_index = scenario_and_indices
        char_pars, new_disc, trace = retrieve_relevant_knowledge(
            "How are users added and removed from the Databricks platform?",
            char_index, disc_index, scenario,
            already_revealed_ids=[],
        )
        assert isinstance(char_pars, list)
        assert isinstance(new_disc, list)
        assert isinstance(trace, dict)

    def test_trace_has_required_keys(self, scenario_and_indices):
        scenario, char_index, disc_index = scenario_and_indices
        _, _, trace = retrieve_relevant_knowledge(
            "How are users added and removed from the Databricks platform?",
            char_index, disc_index, scenario,
            already_revealed_ids=[],
        )
        for key in ("retrieval_mode", "retrieved_ck_items", "matched_di_items",
                    "newly_revealed_di_ids", "excluded_already_revealed_di_ids"):
            assert key in trace, f"trace missing key: {key!r}"

    def test_trace_mode_on_blocked_question(self, scenario_and_indices):
        scenario, char_index, disc_index = scenario_and_indices
        _, _, trace = retrieve_relevant_knowledge(
            "SCIM?", char_index, disc_index, scenario, already_revealed_ids=[],
        )
        assert trace["retrieval_mode"] == "blocked"
        assert trace["retrieved_ck_items"] == []
        assert trace["matched_di_items"] == []

    def test_structural_gate_blocks_bare_noun(self, scenario_and_indices):
        scenario, char_index, disc_index = scenario_and_indices
        char_pars, new_disc, trace = retrieve_relevant_knowledge(
            "SCIM?",
            char_index, disc_index, scenario,
            already_revealed_ids=[],
        )
        assert char_pars == []
        assert new_disc == []

    def test_intent_gate_blocks_catchall(self, scenario_and_indices):
        scenario, char_index, disc_index = scenario_and_indices
        char_pars, new_disc, trace = retrieve_relevant_knowledge(
            "tell me more",
            char_index, disc_index, scenario,
            already_revealed_ids=[],
        )
        assert char_pars == []
        assert new_disc == []

    def test_already_revealed_ids_excluded(self, scenario_and_indices):
        scenario, char_index, disc_index = scenario_and_indices
        # First call: get any newly revealed DI items
        _, first_disc, _ = retrieve_relevant_knowledge(
            "How are users added to the Databricks platform — is that automated or done manually?",
            char_index, disc_index, scenario,
            already_revealed_ids=[],
        )
        if not first_disc:
            pytest.skip("No items revealed on first call — threshold may need tuning")

        revealed_ids = [item.id for item in first_disc]

        # Second call: those items must appear in excluded_already_revealed_di_ids, not newly_revealed
        _, second_disc, trace = retrieve_relevant_knowledge(
            "How are users added to the Databricks platform — is that automated or done manually?",
            char_index, disc_index, scenario,
            already_revealed_ids=revealed_ids,
        )
        second_ids = [item.id for item in second_disc]
        for id_ in revealed_ids:
            assert id_ not in second_ids, (
                f"Item {id_} was already revealed but appeared again in new_disc"
            )
            assert id_ in trace["excluded_already_revealed_di_ids"], (
                f"Item {id_} should be in excluded list in trace"
            )

    def test_returns_scenario_item_objects(self, scenario_and_indices):
        from knowledge import ScenarioItem
        scenario, char_index, disc_index = scenario_and_indices
        char_pars, new_disc, trace = retrieve_relevant_knowledge(
            "How is access to data controlled — who can see what?",
            char_index, disc_index, scenario,
            already_revealed_ids=[],
        )
        for item in char_pars + new_disc:
            assert isinstance(item, ScenarioItem)
            assert item.id
            assert item.content
