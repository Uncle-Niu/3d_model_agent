"""Tests for backend.knowledge.error_patterns."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from backend.knowledge import error_patterns as ep


class _FakeLLM:
    """Minimal stand-in for LLMService used by summarize_turn."""

    def __init__(self, response: str):
        self.response = response
        self.calls: list[tuple[str, str, int]] = []

    async def generate(self, user_message: str, system_prompt: str, max_tokens: int = 1024) -> str:
        self.calls.append((user_message, system_prompt, max_tokens))
        return self.response


class TestErrorPatternStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="ep_test_"))
        os.environ["ERROR_PATTERNS_DIR"] = str(self.tmpdir)

    def tearDown(self) -> None:
        os.environ.pop("ERROR_PATTERNS_DIR", None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_normalize_error_strips_numbers_and_quoted_values(self):
        a = ep.normalize_error("Line 47: NameError: name 'foo' is not defined")
        b = ep.normalize_error("Line 12: NameError: name 'bar' is not defined")
        self.assertEqual(a, b)
        # Numbers collapse to 'N'
        self.assertIn("nameerror:", a)
        self.assertNotIn("47", a)

    def test_record_failure_appends_to_log(self):
        ep.record_failure(
            failure_type="syntax_error",
            error_text="SyntaxError: unexpected EOF",
            fix_kind="mechanical",
            succeeded=True,
            iteration=1,
            turn_id="t1",
            model="qwen3.6",
        )
        events = ep.read_log()
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e.failure_type, "syntax_error")
        self.assertEqual(e.fix_kind, "mechanical")
        self.assertTrue(e.succeeded)
        self.assertEqual(e.turn_id, "t1")

    def test_record_failure_handles_empty_inputs(self):
        result = ep.record_failure(failure_type="", error_text="", fix_kind="", succeeded=False)
        self.assertIsNone(result)

    def test_update_failure_outcome_patches_persisted_row(self):
        # Without this, the orchestrator's in-memory mutations of fix_kind
        # and succeeded never reach disk and the log forever shows
        # "pending/false" — which makes the summarizer think nothing ever
        # works.
        ep.record_failure(
            failure_type="syntax_error",
            error_text="SyntaxError: unexpected EOF",
            fix_kind="pending",
            succeeded=False,
            iteration=1,
            turn_id="t-update",
        )
        patched = ep.update_failure_outcome(
            turn_id="t-update", iteration=1, fix_kind="mechanical", succeeded=True
        )
        self.assertTrue(patched)
        events = ep.read_log()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].fix_kind, "mechanical")
        self.assertTrue(events[0].succeeded)

    def test_update_failure_outcome_returns_false_when_no_match(self):
        ep.record_failure(
            failure_type="syntax_error",
            error_text="SyntaxError",
            fix_kind="pending",
            succeeded=False,
            iteration=1,
            turn_id="t-real",
        )
        patched = ep.update_failure_outcome(
            turn_id="t-does-not-exist", iteration=1, fix_kind="llm"
        )
        self.assertFalse(patched)
        # Original row untouched.
        events = ep.read_log()
        self.assertEqual(events[0].fix_kind, "pending")

    def test_update_failure_outcome_targets_most_recent_duplicate(self):
        # The orchestrator never reuses (turn_id, iteration) pairs, but if
        # something pathological does, the patch should hit the latest row.
        for fix in ("pending", "pending"):
            ep.record_failure(
                failure_type="execution_error",
                error_text="boom",
                fix_kind=fix,
                succeeded=False,
                iteration=1,
                turn_id="t-dupe",
            )
        ep.update_failure_outcome(
            turn_id="t-dupe", iteration=1, fix_kind="llm", succeeded=True
        )
        events = ep.read_log()
        self.assertEqual(len(events), 2)
        # Most recent (last in file) gets the update; the older one stays.
        self.assertEqual(events[-1].fix_kind, "llm")
        self.assertTrue(events[-1].succeeded)
        self.assertEqual(events[0].fix_kind, "pending")

    def test_log_rotation_keeps_only_max_lines(self):
        ep.LOG_MAX_LINES = 5  # type: ignore[attr-defined]
        try:
            for i in range(20):
                ep.record_failure(
                    failure_type="execution_error",
                    error_text=f"err {i}: " + "x" * 200,
                    fix_kind="llm",
                    succeeded=False,
                )
            events = ep.read_log()
            self.assertLessEqual(len(events), 5)
        finally:
            ep.LOG_MAX_LINES = 1000  # type: ignore[attr-defined]

    def test_merge_card_increments_frequency_on_duplicate(self):
        cards: list[ep.PitfallCard] = []
        ep._merge_card(cards, "Avoid pushPoints without a base solid", "pushPoints solid", source_events=2)
        ep._merge_card(cards, "Avoid pushPoints without a base solid", "pushPoints solid", source_events=1)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].frequency, 2)
        self.assertEqual(cards[0].source_event_count, 3)

    def test_merge_card_soft_match_via_trigger_hint_substring(self):
        cards: list[ep.PitfallCard] = []
        ep._merge_card(cards, "Original rule", "pushPoints hole", source_events=1)
        # New rule has a hint that is a substring of the existing hint
        ep._merge_card(cards, "Refined rule", "pushPoints", source_events=1)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].frequency, 2)

    def test_evict_to_cap_drops_lowest_score(self):
        cards = [
            ep.PitfallCard(card_id=f"c{i}", rule=f"r{i}", trigger_hint=f"t{i}", frequency=i + 1)
            for i in range(5)
        ]
        kept = ep._evict_to_cap(cards, cap=3)
        self.assertEqual(len(kept), 3)
        # Highest frequencies survive
        self.assertEqual({c.frequency for c in kept}, {3, 4, 5})

    def test_format_pitfalls_renders_only_when_non_empty(self):
        self.assertEqual(ep.format_pitfalls_for_prompt([]), "")
        cards = [ep.PitfallCard(card_id="x", rule="Hoist params above defs", trigger_hint="hoist")]
        out = ep.format_pitfalls_for_prompt(cards)
        self.assertIn("Learned Pitfalls", out)
        self.assertIn("Hoist params above defs", out)

    def test_save_and_load_roundtrip(self):
        cards = [
            ep.PitfallCard(card_id="a", rule="Rule A", trigger_hint="ha", frequency=3, created_at="2026-05-15T10:00:00+00:00", last_used="2026-05-15T10:00:00+00:00"),
        ]
        ep.save_pitfalls(cards)
        loaded = ep.load_pitfalls()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].rule, "Rule A")
        self.assertEqual(loaded[0].frequency, 3)


class TestSummarizerLLMIntegration(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="ep_sumtest_"))
        os.environ["ERROR_PATTERNS_DIR"] = str(self.tmpdir)

    def tearDown(self) -> None:
        os.environ.pop("ERROR_PATTERNS_DIR", None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def test_summarize_turn_creates_pitfall_cards(self):
        # Pretend the LLM returns a well-formed JSON object with one pitfall.
        llm_response = json.dumps({
            "pitfalls": [
                {
                    "rule": "Hoist parameters above any def() that closes over them",
                    "trigger_hint": "NameError closure",
                }
            ]
        })
        llm = _FakeLLM(llm_response)
        # Two repair events — the firing gate requires at least
        # MIN_REPAIRS_FOR_SUMMARY (2) non-pending repairs in the turn before
        # the summarizer LLM is even called. A real recovered turn always
        # has multiple events; matching that floor here keeps the gate
        # under test rather than the placeholder threshold of 1.
        events = [
            ep.FailureEvent(
                timestamp="2026-05-15T10:00:00+00:00",
                failure_type="execution_error",
                error_first_line="NameError: name 'wall' is not defined",
                error_signature="nameerror: name 'x' is not defined",
                fix_kind="llm",
                succeeded=False,
                iteration=1,
                turn_id="t1",
            ),
            ep.FailureEvent(
                timestamp="2026-05-15T10:00:01+00:00",
                failure_type="execution_error",
                error_first_line="NameError: name 'wall' is not defined",
                error_signature="nameerror: name 'x' is not defined",
                fix_kind="llm",
                succeeded=True,
                iteration=2,
                turn_id="t1",
            ),
        ]
        new = await ep.summarize_turn(llm, events, include_recent_log=0)
        self.assertEqual(len(new), 1)
        self.assertIn("Hoist parameters", new[0].rule)
        stored = ep.load_pitfalls()
        self.assertEqual(len(stored), 1)

    async def test_summarize_turn_no_repairs_skips_llm(self):
        # All events have fix_kind="none" → summarizer should bail before calling LLM.
        llm = _FakeLLM("{}")
        events = [
            ep.FailureEvent(
                timestamp="2026-05-15T10:00:00+00:00",
                failure_type="execution_error",
                error_first_line="error",
                error_signature="error",
                fix_kind="none",
                succeeded=False,
            )
        ]
        new = await ep.summarize_turn(llm, events, include_recent_log=0)
        self.assertEqual(new, [])
        self.assertEqual(llm.calls, [])

    async def test_summarize_turn_tolerates_malformed_llm_output(self):
        llm = _FakeLLM("this is not json at all")
        events = [
            ep.FailureEvent(
                timestamp="2026-05-15T10:00:00+00:00",
                failure_type="execution_error",
                error_first_line="err",
                error_signature="err",
                fix_kind="llm",
                succeeded=True,
            )
        ]
        new = await ep.summarize_turn(llm, events, include_recent_log=0)
        self.assertEqual(new, [])

    async def test_schedule_summarization_returns_task_in_event_loop(self):
        llm = _FakeLLM(json.dumps({"pitfalls": []}))
        events = [
            ep.FailureEvent(
                timestamp="2026-05-15T10:00:00+00:00",
                failure_type="execution_error",
                error_first_line="err",
                error_signature="err",
                fix_kind="llm",
                succeeded=True,
            )
        ]
        task = ep.schedule_summarization(llm, events, include_recent_log=0)
        self.assertIsNotNone(task)
        await task


if __name__ == "__main__":
    unittest.main()
