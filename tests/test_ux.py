"""Pytest suite for visually-impaired UX testing."""

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.ux_testing import (  # noqa: E402
    CommandTestCase,
    UXTestSuite,
    accessibility_checklist,
    analyze_pain_points,
    collect_user_feedback,
    simulate_user_journey,
    test_voice_commands,
    BETA_TESTER_GUIDELINES,
)
from modules.voice import COMMAND_VOCAB  # noqa: E402


@pytest.fixture
def suite(tmp_path):
    return UXTestSuite(db_path=str(tmp_path / "ux_metrics.db"))


# ---------------------------------------------------------------------------
# Command tests
# ---------------------------------------------------------------------------

def test_canonical_phrases_all_match(suite):
    """Every canonical phrase in COMMAND_VOCAB should match itself."""
    cases = [
        CommandTestCase(canonical=c, spoken=p)
        for c, phrases in COMMAND_VOCAB.items() for p in phrases
    ]
    report = suite.test_voice_commands(cases=cases, persist=False)
    assert report["accuracy_pct"] >= 95.0, report["failures"][:5]


def test_full_default_corpus_runs(suite):
    report = suite.test_voice_commands(persist=True)
    assert report["total"] > 30
    assert "by_accent" in report
    assert "by_noise" in report
    assert "by_language" in report


def test_accent_variations_reasonable(suite):
    report = suite.test_voice_commands(persist=False)
    accent_acc = report["by_accent"].get("indian-en")
    # Accent variants should still exceed 70% substring-match accuracy
    if accent_acc is not None:
        assert accent_acc >= 70.0


# ---------------------------------------------------------------------------
# Journey simulation
# ---------------------------------------------------------------------------

def test_simulate_known_scenario_all_steps(suite):
    result = suite.simulate_user_journey("start_topic_practice", persist=False)
    assert len(result.steps) == 7
    assert result.total_secs >= 0
    # Each step has timing
    for step in result.steps:
        assert step.elapsed_secs >= 0.0


def test_simulate_unknown_scenario_raises(suite):
    with pytest.raises(ValueError):
        suite.simulate_user_journey("bogus_scenario", persist=False)


def test_custom_handler_is_invoked(suite):
    called = []

    def fake_handler(text):
        called.append(text)
        return "menu shown"

    steps = [{"name": "open", "input": "menu", "expect_contains": "menu"}]
    result = suite.simulate_user_journey(
        "start_topic_practice", steps=steps, handler=fake_handler, persist=False,
    )
    assert called == ["menu"]
    assert result.success is True


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------

def test_collect_feedback_clarity(suite):
    fb_id = suite.collect_user_feedback(
        session_id="s1", kind="clarity", yes_no=True, comment="crystal clear",
    )
    assert isinstance(fb_id, str) and len(fb_id) > 10


def test_collect_feedback_satisfaction(suite):
    fb_id = suite.collect_user_feedback(
        session_id="s2", kind="satisfaction", rating=4, comment="good pace",
    )
    assert fb_id


def test_collect_feedback_problem(suite):
    suite.collect_user_feedback(
        session_id="s3", kind="problem", comment="Tamil pronunciation off",
    )
    report = suite.analyze_pain_points()
    assert any("Tamil pronunciation off" in p["comment"]
               for p in report["recent_problems"])


# ---------------------------------------------------------------------------
# Pain-point analysis
# ---------------------------------------------------------------------------

def test_analyze_returns_structured_report(suite):
    suite.test_voice_commands(persist=True)
    suite.collect_user_feedback(session_id="s", kind="clarity", yes_no=False)
    suite.collect_user_feedback(session_id="s", kind="satisfaction", rating=2)
    report = suite.analyze_pain_points()
    for key in (
        "command_accuracy", "worst_commands", "noise_impact",
        "scenario_stats", "unclear_explanations", "satisfaction",
        "recent_problems", "suggestions",
    ):
        assert key in report


# ---------------------------------------------------------------------------
# Accessibility checklist
# ---------------------------------------------------------------------------

def test_accessibility_checklist_structure():
    report = accessibility_checklist()
    assert report["total"] > 0
    assert 0 <= report["pass_rate_pct"] <= 100
    assert all({"category", "item", "pass", "note"} <= set(c.keys())
               for c in report["checks"])


def test_accessibility_core_items_pass():
    """Core VI-essential items must all pass out-of-the-box."""
    report = accessibility_checklist()
    core = {"Adjustable speech rates (slow/medium/fast)",
            "Repeat-last-response command available",
            "Bilingual command vocabulary (Tamil + English)",
            "Inter-sentence pause for comprehension"}
    passed = {c["item"] for c in report["checks"] if c["pass"]}
    missing = core - passed
    assert not missing, f"Core a11y items failing: {missing}"


# ---------------------------------------------------------------------------
# Beta-tester guidelines sanity
# ---------------------------------------------------------------------------

def test_beta_guidelines_complete():
    for section in ("target_profile", "recruitment_channels",
                    "session_format", "data_collected", "ethics"):
        assert BETA_TESTER_GUIDELINES[section], f"{section} is empty"
