"""Tiny synthetic known-truth outcome counter and HTML report example.

Truth labels are ``alpha`` and ``beta``; predictions are ``alpha``, ``beta`` or
``None`` (undecided).  Unknown truth is outside this example's domain.  Every
row is one observation, partitioned into correct, wrong and undecided, so
``correct + wrong + undecided == observations`` and ``wrong`` never counts an
undecided prediction.
"""

__all__ = ["summarize", "render_summary", "build_report"]


def summarize(rows):
    """Return the four integer outcome counts for ``truth``/``prediction`` rows."""
    observations = 0
    correct = 0
    wrong = 0
    undecided = 0
    for row in rows:
        observations += 1
        prediction = row["prediction"]
        if prediction is None:
            undecided += 1
        elif prediction == row["truth"]:
            correct += 1
        else:
            wrong += 1
    return {
        "observations": observations,
        "correct": correct,
        "wrong": wrong,
        "undecided": undecided,
    }


def _row(label, value):
    return f"      <tr><th>{label}</th><td>{value}</td></tr>"


def render_summary(summary):
    """Render the four accepted summary counts as a visible HTML table."""
    rows = "\n".join(
        (
            _row("Observations", summary["observations"]),
            _row("Correct", summary["correct"]),
            _row("Wrong", summary["wrong"]),
            _row("Undecided", summary["undecided"]),
        )
    )
    return (
        "<!DOCTYPE html>\n"
        "<html><body>\n"
        '  <table class="outcome-summary">\n'
        f"{rows}\n"
        "  </table>\n"
        "</body></html>\n"
    )


def build_report(rows):
    """Return one summary plus the HTML rendered from that same summary."""
    summary = summarize(rows)
    return {"summary": summary, "html": render_summary(summary)}
