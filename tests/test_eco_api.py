"""Tests for the external ecology interface.

The contract these pin is mostly about what the functions refuse to say. A
function that invents a plausible gradient from a trace with no token counts
would pass a naive test and fail the only property that matters: the answer must
be derived from the artefact, not from the shape of the question.

Nothing here touches the network, and nothing writes outside ``tempfile``.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from audit_sidecar.tools.eco_api import (
    LAWS,
    get_energy_gradient,
    get_evolution_history,
    get_recyclable_waste,
)


class _TraceFixture(unittest.TestCase):
    """Base class that owns a temporary directory for trace files."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)

    def write(self, name: str, records, raw: bool = False) -> str:
        path = os.path.join(self._dir.name, name)
        with open(path, "w", encoding="utf-8") as handle:
            if raw:
                handle.write(records)
            else:
                for record in records:
                    handle.write(json.dumps(record) + "\n")
        return path


# ---------------------------------------------------------------------------
# Law 1 -- energy gradient
# ---------------------------------------------------------------------------


class TestEnergyGradient(_TraceFixture):
    def test_trace_without_cost_fields_is_invalid_not_zero(self):
        """The shipped trace schema stores verdicts, not prices.

        The tempting wrong answer is ``0.0`` tokens per call, which reads as a
        measurement of a free system. The right answer is INVALID: nothing was
        measured.
        """
        path = self.write("t.jsonl", [{"verdict": "PASS"}, {"verdict": "FAIL"}])
        result = get_energy_gradient(path)
        self.assertEqual(result["status"], "INVALID")
        self.assertIsNone(result["tokens"]["per_call"])
        self.assertIsNone(result["seconds"]["per_call"])
        self.assertEqual(result["unmeasured"], ["tokens", "seconds"])
        self.assertIn("missing witness", result["reason"])

    def test_tokens_measured_and_mean_is_arithmetic(self):
        path = self.write(
            "t.jsonl",
            [{"tokens_used": 100}, {"tokens_used": 200}, {"tokens_used": 300}],
        )
        result = get_energy_gradient(path)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["tokens"]["measured_calls"], 3)
        self.assertEqual(result["tokens"]["total"], 600)
        self.assertEqual(result["tokens"]["per_call"], 200)
        # 100, 200, 300 -> slope exactly 100 tokens per call.
        self.assertAlmostEqual(result["tokens"]["gradient_per_call"], 100.0)
        self.assertEqual(result["tokens"]["trend"], "rising")

    def test_falling_trend_is_reported_as_falling(self):
        path = self.write(
            "t.jsonl",
            [{"tokens_used": 300}, {"tokens_used": 200}, {"tokens_used": 100}],
        )
        result = get_energy_gradient(path)
        self.assertAlmostEqual(result["tokens"]["gradient_per_call"], -100.0)
        self.assertEqual(result["tokens"]["trend"], "falling")

    def test_flat_dead_band_does_not_manufacture_a_trend(self):
        path = self.write(
            "t.jsonl", [{"tokens_used": 50}, {"tokens_used": 50}, {"tokens_used": 50}]
        )
        result = get_energy_gradient(path)
        self.assertEqual(result["tokens"]["trend"], "flat")

    def test_single_measurement_has_no_slope(self):
        """One point is not a gradient. Reporting 0.0 would claim a flat trend."""
        path = self.write("t.jsonl", [{"tokens_used": 1000}])
        result = get_energy_gradient(path)
        self.assertIsNone(result["tokens"]["gradient_per_call"])
        self.assertIsNone(result["tokens"]["trend"])

    def test_split_prompt_and_completion_are_summed_not_double_counted(self):
        path = self.write(
            "t.jsonl",
            [
                {"prompt_tokens": 10, "completion_tokens": 5, "tokens_used": 999},
                {"prompt_tokens": 20, "completion_tokens": 5, "tokens_used": 999},
            ],
        )
        result = get_energy_gradient(path)
        # The split pair wins; 999 must not be added on top.
        self.assertEqual(result["tokens"]["total"], 40)

    def test_window_limits_the_sample(self):
        path = self.write(
            "t.jsonl", [{"tokens_used": n} for n in (10, 20, 30, 40, 50)]
        )
        result = get_energy_gradient(path, window=2)
        self.assertEqual(result["samples"], 2)
        self.assertEqual(result["tokens"]["total"], 90)
        self.assertEqual(result["window"], 2)

    def test_window_must_be_positive(self):
        path = self.write("t.jsonl", [{"tokens_used": 1}])
        with self.assertRaises(ValueError):
            get_energy_gradient(path, window=0)
        with self.assertRaises(TypeError):
            get_energy_gradient(path, window=True)

    def test_missing_trace_is_invalid_not_an_exception(self):
        result = get_energy_gradient("/nonexistent/trace.jsonl")
        self.assertEqual(result["status"], "INVALID")
        self.assertIn("no trace", result["reason"])

    def test_unparseable_lines_are_counted_not_silently_dropped(self):
        raw = '{"tokens_used": 10}\nthis is not json\n{"tokens_used": 20}\n'
        path = self.write("t.jsonl", raw, raw=True)
        result = get_energy_gradient(path)
        self.assertEqual(result["skipped_lines"], 1)
        self.assertEqual(result["tokens"]["measured_calls"], 2)

    def test_capacity_headroom_and_calls_before_refusal(self):
        path = self.write("t.jsonl", [{"tokens_used": 100}, {"tokens_used": 100}])
        result = get_energy_gradient(path, capacity=1000)
        capacity = result["capacity"]
        self.assertTrue(capacity["measured"])
        self.assertEqual(capacity["current_per_call"], 100)
        self.assertEqual(capacity["headroom"], 900)
        self.assertEqual(capacity["calls_before_refusal"], 9)

    def test_over_capacity_reports_zero_not_negative(self):
        path = self.write("t.jsonl", [{"tokens_used": 5000}])
        result = get_energy_gradient(path, capacity=1000)
        self.assertLess(result["capacity"]["headroom"], 0)
        self.assertEqual(result["capacity"]["calls_before_refusal"], 0)

    def test_capacity_absent_is_reported_as_unmeasured(self):
        path = self.write("t.jsonl", [{"tokens_used": 100}])
        result = get_energy_gradient(path)
        self.assertFalse(result["capacity"]["measured"])
        self.assertIsNone(result["capacity"]["headroom"])

    def test_negative_capacity_is_rejected(self):
        path = self.write("t.jsonl", [{"tokens_used": 100}])
        with self.assertRaises(ValueError):
            get_energy_gradient(path, capacity=-1)

    def test_duration_milliseconds_are_converted_to_seconds(self):
        path = self.write("t.jsonl", [{"elapsed_ms": 1500}, {"elapsed_ms": 500}])
        result = get_energy_gradient(path)
        self.assertEqual(result["seconds"]["total"], 2.0)
        self.assertEqual(result["seconds"]["per_call"], 1.0)

    def test_status_pass_when_only_one_dimension_measured(self):
        path = self.write("t.jsonl", [{"tokens_used": 10}])
        result = get_energy_gradient(path)
        self.assertEqual(result["status"], "PASS")
        self.assertIn("seconds", result["unmeasured"])

    def test_repeated_calls_do_not_mutate_state(self):
        path = self.write("t.jsonl", [{"tokens_used": 10}, {"tokens_used": 20}])
        first = get_energy_gradient(path)
        second = get_energy_gradient(path)
        self.assertEqual(first, second)


# ---------------------------------------------------------------------------
# Law 2 / Law 4 -- succession and keystone
# ---------------------------------------------------------------------------


class TestEvolutionHistory(_TraceFixture):
    def test_sequence_and_counts_preserve_order(self):
        path = self.write(
            "t.jsonl",
            [
                {"tool": "a", "verdict": "FAIL"},
                {"tool": "a", "verdict": "PASS"},
                {"tool": "b", "verdict": "INVALID"},
            ],
        )
        result = get_evolution_history(path)
        self.assertEqual(result["sequence"], ["FAIL", "PASS", "INVALID"])
        self.assertEqual(result["counts"]["PASS"], 1)
        self.assertEqual(result["counts"]["FAIL"], 1)
        self.assertEqual(result["counts"]["INVALID"], 1)
        self.assertEqual(result["counts"]["NOT RUN"], 0)
        self.assertEqual(result["total"], 3)

    def test_tool_that_started_failing_and_now_passes_has_graduated(self):
        path = self.write(
            "t.jsonl",
            [
                {"tool": "write_file", "verdict": "FAIL"},
                {"tool": "write_file", "verdict": "PASS"},
            ],
        )
        result = get_evolution_history(path)
        self.assertEqual(len(result["graduated"]), 1)
        entry = result["graduated"][0]
        self.assertEqual(entry["tool"], "write_file")
        self.assertTrue(entry["graduated"])
        self.assertFalse(entry["regressed"])

    def test_a_tool_that_passed_first_did_not_graduate(self):
        """Passing on the first call is not a success at anything."""
        path = self.write(
            "t.jsonl",
            [{"tool": "x", "verdict": "PASS"}, {"tool": "x", "verdict": "PASS"}],
        )
        result = get_evolution_history(path)
        self.assertEqual(result["graduated"], [])

    def test_regression_is_reported_separately_from_graduation(self):
        path = self.write(
            "t.jsonl",
            [{"tool": "x", "verdict": "PASS"}, {"tool": "x", "verdict": "FAIL"}],
        )
        result = get_evolution_history(path)
        self.assertEqual(len(result["regressions"]), 1)
        self.assertTrue(result["regressions"][0]["regressed"])
        self.assertEqual(result["graduated"], [])

    def test_keystone_is_the_tool_carrying_most_passes(self):
        path = self.write(
            "t.jsonl",
            [
                {"tool": "a", "verdict": "PASS"},
                {"tool": "b", "verdict": "PASS"},
                {"tool": "b", "verdict": "PASS"},
                {"tool": "b", "verdict": "FAIL"},
            ],
        )
        result = get_evolution_history(path)
        self.assertEqual(result["keystone"]["tool"], "b")
        self.assertEqual(result["keystone"]["passes"], 2)
        self.assertAlmostEqual(result["keystone"]["share_of_passes"], 2 / 3)

    def test_no_passes_means_no_keystone(self):
        path = self.write("t.jsonl", [{"tool": "a", "verdict": "FAIL"}])
        result = get_evolution_history(path)
        self.assertIsNone(result["keystone"])

    def test_unrecognised_verdict_is_skipped_not_bucketed_as_invalid(self):
        """An out-of-date reader must not fabricate a failure."""
        path = self.write(
            "t.jsonl",
            [{"verdict": "PASS"}, {"verdict": "SOMETHING_NEW"}],
        )
        result = get_evolution_history(path)
        self.assertEqual(result["sequence"], ["PASS"])
        self.assertEqual(result["counts"]["INVALID"], 0)
        self.assertEqual(result["skipped_lines"], 1)

    def test_missing_trace_is_invalid(self):
        result = get_evolution_history("/nonexistent/t.jsonl")
        self.assertEqual(result["status"], "INVALID")
        self.assertEqual(result["sequence"], [])

    def test_empty_trace_is_invalid_with_zero_counts(self):
        path = self.write("t.jsonl", "")
        result = get_evolution_history(path)
        self.assertEqual(result["status"], "INVALID")
        self.assertEqual(result["total"], 0)

    def test_non_string_tool_falls_back_to_unknown(self):
        path = self.write("t.jsonl", [{"tool": 17, "verdict": "PASS"}])
        result = get_evolution_history(path)
        self.assertEqual(result["succession"][0]["tool"], "unknown")


# ---------------------------------------------------------------------------
# Law 3 -- recyclable waste
# ---------------------------------------------------------------------------

_ERRATA_FIXTURE = """# ERRATA

Intro prose.

## 1. Packaging — a version mismatch

**The Mistake.** Two numbers described one artefact.

**How it was caught.** Read the artefact back.

**The Fix.** Bump all three together.

**Evidence.** *No surviving artefact.*

## 2. Testing — a silent skip

**The Mistake.** It reported OK while running a tenth of the suite.

**The Fix.** Name the start directory.

## Addendum — a note

Prose that records no failure.
"""


class TestRecyclableWaste(_TraceFixture):
    def write_errata(self, text: str = _ERRATA_FIXTURE) -> str:
        path = os.path.join(self._dir.name, "ERRATA.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_counts_entries_and_extracted_rules(self):
        result = get_recyclable_waste(self.write_errata())
        self.assertEqual(result["entries"], 2)
        self.assertEqual(result["rules_extracted"], 2)
        self.assertEqual(result["unrecycled"], 0)
        self.assertEqual(result["recycle_rate"], 1.0)
        self.assertEqual(result["status"], "PASS")

    def test_prose_section_is_not_counted_as_an_entry(self):
        result = get_recyclable_waste(self.write_errata())
        headings = [entry["heading"] for entry in result["detail"]]
        self.assertTrue(all("Addendum" not in heading for heading in headings))

    def test_entry_without_a_fix_is_counted_as_unrecycled(self):
        text = _ERRATA_FIXTURE + (
            "\n## 3. Unknown — no fix yet\n\n"
            "**The Mistake.** Something went wrong.\n"
        )
        result = get_recyclable_waste(self.write_errata(text))
        self.assertEqual(result["rules_extracted"], 2)
        self.assertEqual(result["unrecycled"], 1)
        # 2 of 3 failures were converted into rules.
        self.assertAlmostEqual(result["recycle_rate"], 2 / 3)

    def test_unwitnessed_entry_is_flagged(self):
        result = get_recyclable_waste(self.write_errata())
        self.assertEqual(result["unwitnessed"], 1)
        flagged = [e for e in result["detail"] if e["unwitnessed"]]
        self.assertEqual(len(flagged), 1)
        self.assertIn("1. Packaging", flagged[0]["heading"])

    def test_missing_errata_is_invalid_not_an_exception(self):
        result = get_recyclable_waste("/nonexistent/ERRATA.md")
        self.assertEqual(result["status"], "INVALID")
        self.assertEqual(result["rules_extracted"], 0)
        self.assertIn("no errata file", result["reason"])

    def test_empty_file_is_invalid(self):
        result = get_recyclable_waste(self.write_errata(""))
        self.assertEqual(result["status"], "INVALID")

    def test_path_type_is_enforced(self):
        with self.assertRaises(TypeError):
            get_recyclable_waste(17)

    def test_real_errata_file_in_this_repository(self):
        """The function must parse the errata this repository actually ships."""
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(here, "ERRATA.md")
        if not os.path.exists(path):
            self.skipTest("ERRATA.md not present")
        result = get_recyclable_waste(path)
        self.assertEqual(result["status"], "PASS")
        self.assertGreaterEqual(result["entries"], 6)
        self.assertEqual(result["rules_extracted"], result["entries"])
        self.assertGreaterEqual(result["unwitnessed"], 2)


# ---------------------------------------------------------------------------
# provenance of the five laws
# ---------------------------------------------------------------------------


class TestLawProvenance(unittest.TestCase):
    def test_three_laws_are_specified_and_two_are_declared_additions(self):
        """A spec ending in "etc." has not defined its fourth and fifth law.

        The payload says which are which rather than presenting five as if all
        five had been requested.
        """
        self.assertEqual(len(LAWS), 5)
        specified = [law for law in LAWS if law["specified"]]
        added = [law for law in LAWS if not law["specified"]]
        self.assertEqual(len(specified), 3)
        self.assertEqual(len(added), 2)
        self.assertEqual(
            [law["name"] for law in specified],
            ["energy_gradient", "edge_to_mainstream", "recyclable_waste"],
        )

    def test_every_law_names_the_function_that_answers_it(self):
        for law in LAWS:
            self.assertIn(
                law["source"],
                {
                    "get_energy_gradient",
                    "get_evolution_history",
                    "get_recyclable_waste",
                },
            )


if __name__ == "__main__":
    unittest.main()
