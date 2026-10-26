"""Public acceptance regression for the directional-metrics summary example.

The oracle is literal: each truth->prediction cell carries its own expected
``false_accept`` and ``negative_total`` rather than a test-side copy of the
production predicate.  A symmetric 16-row aggregate stays insensitive to the
reversed predicate, which is why the per-cell assertions are retained.
"""

import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest


SUMMARY_PATH = (
    Path(__file__).resolve().parents[1] / "assets" / "directional-metrics" / "summary.py"
)

TRUTHS = ("alpha", "beta", "outside", "unknown")
PREDICTIONS = ("alpha", "beta", "outside", None)

# Literal (truth, prediction, false_accept, negative_total) oracle for all 16 cells.
EXPECTED_CELLS = (
    ("alpha", "alpha", 0, 0),
    ("alpha", "beta", 0, 0),
    ("alpha", "outside", 0, 0),
    ("alpha", None, 0, 0),
    ("beta", "alpha", 0, 0),
    ("beta", "beta", 0, 0),
    ("beta", "outside", 0, 0),
    ("beta", None, 0, 0),
    ("outside", "alpha", 1, 1),
    ("outside", "beta", 1, 1),
    ("outside", "outside", 0, 1),
    ("outside", None, 0, 1),
    ("unknown", "alpha", 0, 0),
    ("unknown", "beta", 0, 0),
    ("unknown", "outside", 0, 0),
    ("unknown", None, 0, 0),
)

FORWARD_PREDICATE = 'if truth == "outside" and prediction in ("alpha", "beta"):'
REVERSE_PREDICATE = 'if truth in ("alpha", "beta") and prediction == "outside":'


def load_summary(path, name):
    """Load an actual summary.py source file under a private module name."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load summary module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DirectionalMetricsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.summary = load_summary(SUMMARY_PATH, "_directional_summary_real")

    def assert_direction_cell(self, summarize, truth, prediction, false_accept, negative_total):
        """Reusable public cell assertion: one call to ``summarize`` per pair."""
        cell = f"{truth}->{prediction}"
        counts = summarize([{"truth": truth, "prediction": prediction}])
        self.assertEqual(
            counts["false_accept"],
            false_accept,
            f"false_accept for {cell}",
        )
        self.assertEqual(
            counts["negative_total"],
            negative_total,
            f"negative_total for {cell}",
        )

    def test_direction_cells_use_literal_pair_expectations(self):
        for truth, prediction, false_accept, negative_total in EXPECTED_CELLS:
            with self.subTest(truth=truth, prediction=prediction):
                self.assert_direction_cell(
                    self.summary.summarize, truth, prediction, false_accept, negative_total
                )

    def test_asymmetric_denominator_case(self):
        rows = [
            {"truth": "outside", "prediction": "alpha"},
            {"truth": "outside", "prediction": "alpha"},
            {"truth": "outside", "prediction": "beta"},
            {"truth": "outside", "prediction": None},
            {"truth": "alpha", "prediction": "outside"},
            {"truth": "unknown", "prediction": "alpha"},
            {"truth": "unknown", "prediction": None},
            {"truth": "beta", "prediction": "beta"},
        ]
        self.assertEqual(
            self.summary.summarize(rows),
            {
                "total": 8,
                "false_accept": 3,
                "negative_total": 4,
                "unknown_truth": 2,
                "undecided": 2,
            },
        )
        self.assertEqual(
            self.summary.summarize([]),
            {
                "total": 0,
                "false_accept": 0,
                "negative_total": 0,
                "unknown_truth": 0,
                "undecided": 0,
            },
        )

    def test_reverse_predicate_mutation_is_caught_by_same_assertion(self):
        original_bytes = SUMMARY_PATH.read_bytes()
        original_digest = hashlib.sha256(original_bytes).hexdigest()
        with tempfile.TemporaryDirectory(prefix="directional-metrics-mutant-") as tmp:
            copy_path = Path(tmp) / "summary.py"
            source = original_bytes.decode("utf-8")
            self.assertEqual(
                source.count(FORWARD_PREDICATE),
                1,
                "forward false_accept predicate must be unique in the source",
            )
            mutated_source = source.replace(FORWARD_PREDICATE, REVERSE_PREDICATE)
            self.assertEqual(mutated_source.count(REVERSE_PREDICATE), 1)
            self.assertNotEqual(mutated_source, source)
            copy_path.write_text(mutated_source, encoding="utf-8")

            mutant = load_summary(copy_path, "_directional_summary_mutant")
            self.assertEqual(
                mutant.summarize([{"truth": "alpha", "prediction": "outside"}])["false_accept"],
                1,
                "mutant alpha->outside false_accept",
            )
            self.assertEqual(
                mutant.summarize([{"truth": "outside", "prediction": "alpha"}])["false_accept"],
                0,
                "mutant outside->alpha false_accept",
            )

            with self.assertRaises(AssertionError) as caught:
                self.assert_direction_cell(mutant.summarize, "outside", "alpha", 1, 1)
            message = str(caught.exception)
            self.assertIn("false_accept", message)
            self.assertIn("outside->alpha", message)

            # Symmetric aggregate control: the pooled check cannot see the reversal.
            symmetric_rows = [
                {"truth": truth, "prediction": prediction}
                for truth in TRUTHS
                for prediction in PREDICTIONS
            ]
            for module, label in ((self.summary, "real"), (mutant, "mutant")):
                counts = module.summarize(symmetric_rows)
                self.assertEqual(
                    counts["false_accept"], 2, f"pooled false_accept ({label})"
                )
                self.assertEqual(
                    counts["negative_total"], 4, f"pooled negative_total ({label})"
                )

            copy_path.write_bytes(original_bytes)
            restored = load_summary(copy_path, "_directional_summary_restored")
            self.assert_direction_cell(restored.summarize, "outside", "alpha", 1, 1)
            self.assertEqual(
                hashlib.sha256(copy_path.read_bytes()).hexdigest(), original_digest
            )

        self.assertEqual(SUMMARY_PATH.read_bytes(), original_bytes)
        self.assertEqual(
            hashlib.sha256(SUMMARY_PATH.read_bytes()).hexdigest(), original_digest
        )


if __name__ == "__main__":
    unittest.main()
