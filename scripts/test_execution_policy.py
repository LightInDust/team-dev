#!/usr/bin/env python3
"""Unit tests for execution_policy.resolve_policy."""

import unittest

from execution_policy import _DEFAULT_ASSESSMENT, resolve_policy

PROFILE_EXPECTED = {
    "low": {
        "warn_seconds": 120,
        "stall_seconds": 300,
        "first_write_seconds": 420,
        "timeout_seconds": 900,
    },
    "high": {
        "warn_seconds": 240,
        "stall_seconds": 600,
        "first_write_seconds": 900,
        "timeout_seconds": 1800,
    },
    "max": {
        "warn_seconds": 360,
        "stall_seconds": 900,
        "first_write_seconds": 1200,
        "timeout_seconds": 2400,
    },
}

DEFAULT_ASSESSMENT_EXPECTED = {
    "by": "default",
    "reason": "No coordinator assessment supplied; compatibility default.",
}


class ResolvePolicyTest(unittest.TestCase):
    def test_auto_profile_defaults(self):
        for effort, expected in PROFILE_EXPECTED.items():
            with self.subTest(effort=effort):
                result = resolve_policy({"worker_thinking": effort})
                self.assertEqual(result["policy_version"], 2)
                self.assertEqual(result["worker_thinking"], effort)
                self.assertEqual(result["budget_mode"], "auto")
                for field, value in expected.items():
                    if field == "timeout_seconds":
                        self.assertEqual(result["timeout_seconds"], value)
                    else:
                        self.assertEqual(result["progress"][field], value)

    def test_high_auto_replaces_stale_low_numbers(self):
        stale = {
            "worker_thinking": "high",
            "budget_mode": "auto",
            "progress": {
                "warn_seconds": 120,
                "stall_seconds": 300,
                "first_write_seconds": 420,
            },
            "timeout_seconds": 900,  # stale low timeout, intentionally replaced
        }
        result = resolve_policy(stale)
        self.assertEqual(result["budget_mode"], "auto")
        self.assertEqual(result["timeout_seconds"], 1800)
        self.assertEqual(result["progress"]["warn_seconds"], 240)
        self.assertEqual(result["progress"]["stall_seconds"], 600)
        self.assertEqual(result["progress"]["first_write_seconds"], 900)
        # Input untouched.
        self.assertEqual(stale["timeout_seconds"], 900)
        self.assertEqual(stale["progress"]["warn_seconds"], 120)

    def test_custom_partial_override_preserves_chosen_defaults(self):
        result = resolve_policy(
            {
                "worker_thinking": "max",
                "budget_mode": "custom",
                "progress": {"warn_seconds": 500},
            }
        )
        self.assertEqual(result["budget_mode"], "custom")
        self.assertEqual(result["progress"]["warn_seconds"], 500)
        self.assertEqual(result["progress"]["stall_seconds"], 900)
        self.assertEqual(result["progress"]["first_write_seconds"], 1200)
        self.assertEqual(result["timeout_seconds"], 2400)

    def test_custom_preserves_explicit_timeout(self):
        result = resolve_policy(
            {
                "worker_thinking": "low",
                "budget_mode": "custom",
                "timeout_seconds": 600,
            }
        )
        self.assertEqual(result["budget_mode"], "custom")
        self.assertEqual(result["timeout_seconds"], 600)
        self.assertEqual(result["progress"]["warn_seconds"], 120)

    def test_unspecified_mode_legacy_numeric_stays_custom(self):
        result = resolve_policy({"worker_thinking": "high", "timeout_seconds": 500})
        self.assertEqual(result["budget_mode"], "custom")
        self.assertEqual(result["timeout_seconds"], 500)
        self.assertEqual(result["progress"]["stall_seconds"], 600)

        result2 = resolve_policy(
            {"worker_thinking": "low", "progress": {"warn_seconds": 90}}
        )
        self.assertEqual(result2["budget_mode"], "custom")
        self.assertEqual(result2["progress"]["warn_seconds"], 90)
        self.assertEqual(result2["timeout_seconds"], 900)

    def test_explicit_shorter_timeout_takes_precedence(self):
        # stall/first_write may exceed the explicit total; they are not
        # required to fit within it.
        result = resolve_policy(
            {
                "worker_thinking": "low",
                "budget_mode": "custom",
                "progress": {"stall_seconds": 3000, "first_write_seconds": 3500},
                "timeout_seconds": 500,
            }
        )
        self.assertEqual(result["progress"]["stall_seconds"], 3000)
        self.assertEqual(result["progress"]["first_write_seconds"], 3500)
        self.assertEqual(result["timeout_seconds"], 500)

    def test_unsupported_thinking_rejected(self):
        for bad in ("off", "medium", "auto", 123, None, ["high"]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    resolve_policy({"worker_thinking": bad})

    def test_unknown_budget_mode_rejected(self):
        for bad in ("whatever", "CUSTOM", 1, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    resolve_policy({"worker_thinking": "high", "budget_mode": bad})

    def test_bad_assessment_rejected(self):
        bad = [
            {"by": "nope", "reason": "why"},
            {"by": "astra", "reason": ""},
            {"by": "astra", "reason": "   "},
            {"by": "astra", "reason": "x" * 2001},
            {"by": "astra"},  # missing reason
            {"reason": "ok"},  # missing by -> by None
            "astra",  # wrong type
        ]
        for case in bad:
            with self.subTest(case=case):
                with self.assertRaises(ValueError):
                    resolve_policy(
                        {"worker_thinking": "high", "effort_assessment": case}
                    )

    def test_assessment_extra_fields_preserved_deep_copied(self):
        result = resolve_policy(
            {
                "worker_thinking": "low",
                "effort_assessment": {
                    "by": "user",
                    "reason": "short",
                    "nested": {"a": [1, 2]},
                },
            }
        )
        self.assertEqual(result["effort_assessment"]["by"], "user")
        self.assertEqual(result["effort_assessment"]["nested"], {"a": [1, 2]})

    def test_default_assessment(self):
        result = resolve_policy({"worker_thinking": "low"})
        self.assertEqual(result["effort_assessment"], DEFAULT_ASSESSMENT_EXPECTED)

    def test_bad_budgets_rejected(self):
        bad_numeric = [
            float("nan"),
            float("inf"),
            float("-inf"),
            True,
            False,
            -5,
            0,
            "5",
            None,
        ]
        for bad in bad_numeric:
            with self.subTest(what="warn", bad=bad):
                with self.assertRaises(ValueError):
                    resolve_policy(
                        {
                            "worker_thinking": "high",
                            "budget_mode": "custom",
                            "progress": {"warn_seconds": bad},
                        }
                    )
            with self.subTest(what="stall", bad=bad):
                with self.assertRaises(ValueError):
                    resolve_policy(
                        {
                            "worker_thinking": "high",
                            "budget_mode": "custom",
                            "progress": {"stall_seconds": bad},
                        }
                    )

        for warn, stall in ((300, 300), (400, 200)):
            with self.subTest(warn=warn, stall=stall):
                with self.assertRaises(ValueError):
                    resolve_policy(
                        {
                            "worker_thinking": "high",
                            "budget_mode": "custom",
                            "progress": {"warn_seconds": warn, "stall_seconds": stall},
                        }
                    )

        for bad in (True, False, 9, 3601, 1.5, "30", float("inf")):
            with self.subTest(timeout=bad):
                with self.assertRaises(ValueError):
                    resolve_policy(
                        {
                            "worker_thinking": "high",
                            "budget_mode": "custom",
                            "timeout_seconds": bad,
                        }
                    )

        # Unknown progress key rejected in both modes.
        for mode in ("custom", "auto"):
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError):
                    resolve_policy(
                        {
                            "worker_thinking": "high",
                            "budget_mode": mode,
                            "progress": {"bogus_seconds": 1},
                        }
                    )

    def test_auto_missing_effort_defaults_low(self):
        result = resolve_policy({})
        self.assertEqual(result["worker_thinking"], "low")
        self.assertEqual(result["budget_mode"], "auto")
        self.assertEqual(result["progress"]["warn_seconds"], 120)
        self.assertEqual(result["timeout_seconds"], 900)

    def test_no_alias_and_input_unmutated(self):
        task = {
            "worker_thinking": "high",
            "budget_mode": "custom",
            "progress": {"warn_seconds": 250},
            "timeout_seconds": 1200,
            "effort_assessment": {
                "by": "sol",
                "reason": "context-heavy",
                "extra": {"confidence": 0.9},
            },
        }
        result_a = resolve_policy(task)
        result_b = resolve_policy(task)

        # Mutating a result must not leak into the input or the other result.
        result_a["progress"]["warn_seconds"] = 99999
        result_a["timeout_seconds"] = 99999
        result_a["effort_assessment"]["extra"]["confidence"] = 0.0
        result_a["effort_assessment"]["reason"] = "mutated"

        self.assertEqual(task["progress"], {"warn_seconds": 250})
        self.assertEqual(task["timeout_seconds"], 1200)
        self.assertEqual(task["effort_assessment"]["extra"], {"confidence": 0.9})
        self.assertEqual(task["effort_assessment"]["reason"], "context-heavy")

        self.assertEqual(result_b["progress"]["warn_seconds"], 250)
        self.assertEqual(result_b["timeout_seconds"], 1200)
        self.assertEqual(result_b["effort_assessment"]["reason"], "context-heavy")
        self.assertEqual(result_b["effort_assessment"]["extra"], {"confidence": 0.9})

        # Default assessment must not alias the module-level default.
        dflt = resolve_policy({"worker_thinking": "low"})
        dflt["effort_assessment"]["reason"] = "changed"
        self.assertNotEqual(dflt["effort_assessment"]["reason"], _DEFAULT_ASSESSMENT["reason"])
        again = resolve_policy({"worker_thinking": "low"})
        self.assertEqual(again["effort_assessment"], DEFAULT_ASSESSMENT_EXPECTED)


if __name__ == "__main__":
    unittest.main()