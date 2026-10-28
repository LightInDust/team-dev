"""Public acceptance regression for the known-truth outcome report example.

The oracle is literal and independent of the implementation: each
truth->prediction case carries its own expected observations/correct/wrong/
undecided counts.  The same reusable assertion checks the public
``build_report`` summary and the visible labels/cells of the saved HTML, so a
renderer that recomputes ``Wrong`` cannot hide behind a correct summary.  Two
isolated source mutations show that both complement-count faults fail that
same assertion, and the restored copy passes it again.
"""

import hashlib
from html.parser import HTMLParser
import importlib.util
from pathlib import Path
import tempfile
import unittest


OUTCOMES_PATH = (
    Path(__file__).resolve().parents[1]
    / "assets"
    / "directional-metrics"
    / "outcomes.py"
)

FIELDS = ("observations", "correct", "wrong", "undecided")

LABELS = {
    "observations": "Observations",
    "correct": "Correct",
    "wrong": "Wrong",
    "undecided": "Undecided",
}

# Literal (truth, prediction, observations, correct, wrong, undecided) oracle.
EXPECTED_CELLS = (
    ("alpha", "alpha", 1, 1, 0, 0),
    ("alpha", "beta", 1, 0, 1, 0),
    ("alpha", None, 1, 0, 0, 1),
    ("beta", "alpha", 1, 0, 1, 0),
    ("beta", "beta", 1, 1, 0, 0),
    ("beta", None, 1, 0, 0, 1),
)

EMPTY_EXPECTED = (0, 0, 0, 0)

ZERO_WRONG_ROWS = (
    {"truth": "alpha", "prediction": "alpha"},
    {"truth": "beta", "prediction": "beta"},
    {"truth": "alpha", "prediction": None},
    {"truth": "beta", "prediction": None},
)
ZERO_WRONG_EXPECTED = (4, 2, 0, 2)

MIXED_ROWS = (
    {"truth": "alpha", "prediction": "alpha"},
    {"truth": "alpha", "prediction": "alpha"},
    {"truth": "beta", "prediction": "beta"},
    {"truth": "alpha", "prediction": "beta"},
    {"truth": "alpha", "prediction": None},
    {"truth": "beta", "prediction": None},
)
MIXED_EXPECTED = (6, 3, 1, 2)

# Exact single-replacement targets for the two fault subcases.
WRONG_COMPLEMENT_TARGET = '"wrong": wrong,'
WRONG_COMPLEMENT_REPLACEMENT = '"wrong": observations - correct,'
RENDER_RECOMPUTE_TARGET = '_row("Wrong", summary["wrong"])'
RENDER_RECOMPUTE_REPLACEMENT = '_row("Wrong", summary["observations"] - summary["correct"])'


def load_outcomes(path, name):
    """Load an actual outcomes.py source file under a private module name."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load outcomes module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OutcomeTableParser(HTMLParser):
    """Collect the visible ``th`` label / ``td`` value pairs of the report table."""

    def __init__(self):
        super().__init__()
        self.rows = []
        self._label = None
        self._value = None
        self._in_label = False
        self._in_value = False

    def handle_starttag(self, tag, attrs):
        if tag == "th":
            self._label = []
            self._in_label = True
        elif tag == "td":
            self._value = []
            self._in_value = True

    def handle_endtag(self, tag):
        if tag == "th" and self._in_label:
            self._in_label = False
        elif tag == "td" and self._in_value:
            self._in_value = False
            self.rows.append(
                ("".join(self._label or ()).strip(), "".join(self._value).strip())
            )

    def handle_data(self, data):
        if self._in_label:
            self._label.append(data)
        elif self._in_value:
            self._value.append(data)


class OutcomeReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.outcomes = load_outcomes(OUTCOMES_PATH, "_outcome_report_real")

    def assert_report(self, module, rows, expected, case):
        """Public oracle: one build_report call, then its saved HTML table."""
        expected_summary = dict(zip(FIELDS, expected))
        with tempfile.TemporaryDirectory(prefix="outcome-report-") as tmp:
            report = module.build_report(list(rows))
            self.assertIn("summary", report, f"{case}: build_report has summary")
            self.assertIn("html", report, f"{case}: build_report has html")
            self.assertEqual(
                set(report["summary"]), set(FIELDS), f"{case}: summary keys"
            )
            for field in FIELDS:
                self.assertEqual(
                    report["summary"][field],
                    expected_summary[field],
                    f"{case}: summary[{field}]",
                )

            html_path = Path(tmp) / "report.html"
            html_path.write_text(report["html"], encoding="utf-8")
            parser = OutcomeTableParser()
            parser.feed(html_path.read_text(encoding="utf-8"))
            parser.close()

            self.assertEqual(len(parser.rows), 4, f"{case}: visible row count")
            visible = dict(parser.rows)
            self.assertEqual(len(visible), 4, f"{case}: unique visible labels")
            for field in FIELDS:
                label = LABELS[field]
                self.assertIn(label, visible, f"{case}: visible label {label}")
                self.assertEqual(
                    visible[label],
                    str(expected_summary[field]),
                    f"{case}: HTML label {label}",
                )

    def test_literal_outcome_cells(self):
        for truth, prediction, observations, correct, wrong, undecided in EXPECTED_CELLS:
            with self.subTest(truth=truth, prediction=prediction):
                self.assert_report(
                    self.outcomes,
                    [{"truth": truth, "prediction": prediction}],
                    (observations, correct, wrong, undecided),
                    f"{truth}->{prediction}",
                )

    def test_empty_rows_are_zero(self):
        self.assert_report(self.outcomes, [], EMPTY_EXPECTED, "empty")

    def test_zero_wrong_case_keeps_undecided_out_of_wrong(self):
        self.assert_report(
            self.outcomes, ZERO_WRONG_ROWS, ZERO_WRONG_EXPECTED, "zero-wrong"
        )

    def test_asymmetric_mixed_case(self):
        self.assert_report(self.outcomes, MIXED_ROWS, MIXED_EXPECTED, "mixed")

    def test_complement_count_faults_are_caught_by_same_oracle(self):
        original_bytes = OUTCOMES_PATH.read_bytes()
        original_digest = hashlib.sha256(original_bytes).hexdigest()
        source = original_bytes.decode("utf-8")
        self.assertEqual(
            source.count(WRONG_COMPLEMENT_TARGET),
            1,
            "summarize wrong target must be unique in the source",
        )
        self.assertEqual(
            source.count(RENDER_RECOMPUTE_TARGET),
            1,
            "render Wrong target must be unique in the source",
        )

        with tempfile.TemporaryDirectory(prefix="outcome-mutant-") as tmp:
            copy_path = Path(tmp) / "outcomes.py"

            # (i) summarize reports wrong = observations - correct.
            summary_mutant_source = source.replace(
                WRONG_COMPLEMENT_TARGET, WRONG_COMPLEMENT_REPLACEMENT
            )
            self.assertNotEqual(summary_mutant_source, source)
            copy_path.write_text(summary_mutant_source, encoding="utf-8")
            summary_mutant = load_outcomes(copy_path, "_outcomes_summary_mutant")

            mutant_counts = summary_mutant.summarize(list(ZERO_WRONG_ROWS))
            self.assertEqual(
                mutant_counts["wrong"], 2, "summary mutant changed wrong count"
            )
            self.assertEqual(mutant_counts["observations"], 4)
            self.assertEqual(mutant_counts["correct"], 2)
            self.assertEqual(mutant_counts["undecided"], 2)

            with self.assertRaises(AssertionError) as caught:
                self.assert_report(
                    summary_mutant,
                    ZERO_WRONG_ROWS,
                    ZERO_WRONG_EXPECTED,
                    "summary mutant",
                )
            self.assertIn("wrong", str(caught.exception))

            # (ii) render_summary recomputes displayed Wrong from the summary.
            copy_path.write_text(
                source.replace(RENDER_RECOMPUTE_TARGET, RENDER_RECOMPUTE_REPLACEMENT),
                encoding="utf-8",
            )
            render_mutant = load_outcomes(copy_path, "_outcomes_render_mutant")

            report = render_mutant.build_report(list(ZERO_WRONG_ROWS))
            self.assertEqual(
                report["summary"],
                dict(zip(FIELDS, ZERO_WRONG_EXPECTED)),
                "render mutant summary stays correct",
            )
            parser = OutcomeTableParser()
            parser.feed(report["html"])
            parser.close()
            self.assertEqual(
                dict(parser.rows)["Wrong"],
                "2",
                "render mutant changed displayed Wrong",
            )

            with self.assertRaises(AssertionError) as caught:
                self.assert_report(
                    render_mutant,
                    ZERO_WRONG_ROWS,
                    ZERO_WRONG_EXPECTED,
                    "render mutant",
                )
            self.assertIn("Wrong", str(caught.exception))

            # Restore the copy, load it fresh, and require the same oracle to pass.
            copy_path.write_bytes(original_bytes)
            restored = load_outcomes(copy_path, "_outcomes_restored")
            self.assert_report(
                restored, ZERO_WRONG_ROWS, ZERO_WRONG_EXPECTED, "restored"
            )
            self.assertEqual(
                hashlib.sha256(copy_path.read_bytes()).hexdigest(), original_digest
            )

        self.assertEqual(OUTCOMES_PATH.read_bytes(), original_bytes)
        self.assertEqual(
            hashlib.sha256(OUTCOMES_PATH.read_bytes()).hexdigest(), original_digest
        )


if __name__ == "__main__":
    unittest.main()
