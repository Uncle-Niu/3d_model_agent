"""Tests for the vision-critique parser.

Regression target: the iPhone-holder run on 2026-05-16 ended with
"Failed to parse vision model response (no usable signal recovered)" because
qwen3.6:27b dumped its entire chain-of-thought as markdown narrative and
never reached the JSON closing object. The fallback parser must recover
usable signal from that prose so the repair loop still gets feedback.
"""

import unittest

from backend.vision.critic import (
    _fallback_parse_critique,
    _parse_json_response,
)


class TestStrictJsonParse(unittest.TestCase):

    def test_parses_clean_json(self):
        raw = '{"matches_intent": true, "score": 0.9, "issues": []}'
        parsed = _parse_json_response(raw)
        self.assertEqual(parsed["score"], 0.9)
        self.assertTrue(parsed["matches_intent"])

    def test_unwraps_markdown_fences(self):
        raw = '```json\n{"score": 0.5, "matches_intent": false}\n```'
        parsed = _parse_json_response(raw)
        self.assertEqual(parsed["score"], 0.5)

    def test_finds_json_with_preamble(self):
        raw = (
            "Here is my response:\n\n"
            '{"matches_intent": false, "score": 0.3}'
        )
        parsed = _parse_json_response(raw)
        self.assertFalse(parsed["matches_intent"])


class TestFallbackParseTruncatedJson(unittest.TestCase):
    """Path 1: model started writing JSON but ran out of tokens."""

    def test_recovers_score_and_matches_intent_from_truncation(self):
        raw = (
            '{\n'
            '  "matches_intent": false,\n'
            '  "score": 0.45,\n'
            '  "feature_checklist": [\n'
            '    {"feature": "mounting holes", "present": false},\n'
            '    {"feature": "fillets", "present": "missing"}\n'
            # truncated before closing brace
        )
        out = _fallback_parse_critique(raw)
        self.assertEqual(out["score"], 0.45)
        self.assertFalse(out["matches_intent"])
        descriptions = [i["description"].lower() for i in out["issues"]]
        self.assertTrue(any("mounting holes" in d for d in descriptions))


class TestFallbackParseProseResponse(unittest.TestCase):
    """Path 2: model never wrote JSON — only markdown narration.

    This is the actual failure from scratch/vision_raw_response.txt, captured
    by scripts/debug_vision_parse.py. The parser must scrape verdicts out of
    qwen3-style thinking prose.
    """

    PROSE = """\
The user wants me to verify a 3D CAD model.

**Inspection results:**

*   **Base plate:** Present. Size seems roughly correct.
*   **Side walls:** Present. Two walls visible in Top and Iso views.
*   **Back wall:** Present. Angled.
*   **Front lip:** Present.
*   **Cable slot:** Present.
*   **Corner mounting holes:** **Missing.** I do not see holes in the corners of the base plate in the Top view.
*   **Triangular gussets:** **Missing.** The junctions look like simple T-junctions.
*   **Fillets:** **Missing.** Edges look sharp.

**Matches Intent:** No. Key features missing.
**Score:** 0.4 (Major features present, but details missing).
"""

    def test_recovers_matches_intent_from_markdown(self):
        out = _fallback_parse_critique(self.PROSE)
        self.assertEqual(out["matches_intent"], False)

    def test_recovers_score_from_markdown(self):
        out = _fallback_parse_critique(self.PROSE)
        self.assertAlmostEqual(out["score"], 0.4, places=2)

    def test_recovers_missing_features_from_markdown_bullets(self):
        out = _fallback_parse_critique(self.PROSE)
        descriptions = " | ".join(i["description"] for i in out["issues"])
        self.assertIn("Corner mounting holes", descriptions)
        self.assertIn("Triangular gussets", descriptions)
        self.assertIn("Fillets", descriptions)
        # The "Present" features must NOT be issues.
        self.assertNotIn("Base plate", descriptions)
        self.assertNotIn("Side walls", descriptions)

    def test_handles_double_bold_verdict(self):
        # Real-world variant: `**Name:** **Missing.**` (bolded twice).
        raw = (
            "*   **Corner mounting holes:** **Missing.** I do not see holes.\n"
            "*   **Fillets:** **Missing.** Edges sharp.\n"
        )
        out = _fallback_parse_critique(raw)
        descriptions = " | ".join(i["description"] for i in out["issues"])
        self.assertIn("Corner mounting holes", descriptions)
        self.assertIn("Fillets", descriptions)

    def test_partial_verdicts_become_warnings(self):
        raw = "*   **Front lip:** Partial. Looks a bit off.\n"
        out = _fallback_parse_critique(raw)
        # Partial → warning severity (not error). matches_intent stays None
        # until a strong signal is found, so the helper defaults it later.
        self.assertEqual(out["issues"][0]["severity"], "warning")

    def test_score_percentage_normalized(self):
        raw = "**Score:** 40 (out of 100). **Matches Intent:** No."
        out = _fallback_parse_critique(raw)
        self.assertAlmostEqual(out["score"], 0.4, places=2)

    def test_empty_response_returns_empty_dict(self):
        self.assertEqual(_fallback_parse_critique(""), {})

    def test_pure_garbage_returns_empty_dict(self):
        self.assertEqual(_fallback_parse_critique("blah blah blah"), {})


if __name__ == "__main__":
    unittest.main()
