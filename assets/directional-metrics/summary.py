"""Tiny synthetic directional-count example.

Truth labels are ``alpha``, ``beta``, ``outside`` and ``unknown``; predictions
are ``alpha``, ``beta``, ``outside`` or ``None`` (undecided).  ``false_accept``
only counts outside-truth rows predicted as alpha/beta, while
``negative_total`` counts every outside-truth row, including undecided ones.
"""

__all__ = ["summarize"]


def summarize(rows):
    """Return the five integer observation counts for ``truth``/``prediction`` rows."""
    total = 0
    false_accept = 0
    negative_total = 0
    unknown_truth = 0
    undecided = 0
    for row in rows:
        truth = row["truth"]
        prediction = row["prediction"]
        total += 1
        if truth == "outside":
            negative_total += 1
        if truth == "outside" and prediction in ("alpha", "beta"):
            false_accept += 1
        if truth == "unknown":
            unknown_truth += 1
        if prediction is None:
            undecided += 1
    return {
        "total": total,
        "false_accept": false_accept,
        "negative_total": negative_total,
        "unknown_truth": unknown_truth,
        "undecided": undecided,
    }
