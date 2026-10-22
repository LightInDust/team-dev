#!/usr/bin/env python3
"""Pure, deterministic execution-budget policy resolver.

Astra/Sol negotiate reasoning effort; this function maps the explicit effort
selection to concrete time budgets. It is a pure function: it performs no IO,
makes no model calls, runs no complexity classifier, and never mutates its
inputs or module state. Every call returns a fully independent result dict.
"""

import math
from copy import deepcopy
from typing import Any, Dict, Tuple

POLICY_VERSION = 2

_CLOSEOUT_PROFILES = {
    'low': {'warn_seconds': 180, 'deadline_seconds': 300},
    'high': {'warn_seconds': 360, 'deadline_seconds': 600},
    'max': {'warn_seconds': 540, 'deadline_seconds': 900},
}

# worker_thinking -> (warn_seconds, stall_seconds, first_write_seconds)
_EFFORT_PROFILES: Dict[str, Dict[str, int]] = {
    "low": {"warn_seconds": 120, "stall_seconds": 300, "first_write_seconds": 420},
    "high": {"warn_seconds": 240, "stall_seconds": 600, "first_write_seconds": 900},
    "max": {"warn_seconds": 360, "stall_seconds": 900, "first_write_seconds": 1200},
}

# worker_thinking -> default total execution timeout (seconds)
_EFFORT_TIMEOUTS: Dict[str, int] = {
    "low": 900,
    "high": 1800,
    "max": 2400,
}

_SUPPORTED_THINKING = frozenset(_EFFORT_PROFILES)
_SUPPORTED_BUDGET_MODES = frozenset(("auto", "custom"))
_SUPPORTED_PROGRESS_KEYS = frozenset(
    ("warn_seconds", "stall_seconds", "first_write_seconds")
)
_SUPPORTED_ASSESSMENT_BY = frozenset(("astra", "sol", "user", "default"))

_DEFAULT_ASSESSMENT: Dict[str, str] = {
    "by": "default",
    "reason": "No coordinator assessment supplied; compatibility default.",
}

_MIN_TIMEOUT = 10
_MAX_TIMEOUT = 3600
_MAX_REASON_LEN = 2000


def resolve_policy(task: dict, *, version=POLICY_VERSION) -> dict:
    """Resolve a single policy for ``task`` and return a fresh, independent dict.

    The input ``task`` is treated as read-only: it is never mutated, and no
    value from it or from module state is aliased into the result.
    """
    if not isinstance(task, dict):
        raise ValueError("task must be a dict")
    if type(version) is not int or version not in (1, 2):
        raise ValueError('Unsupported execution policy version')

    thinking = _resolve_thinking(task)
    mode = _resolve_budget_mode(task)
    progress, timeout = _resolve_budgets(task, thinking, mode)
    assessment = _resolve_assessment(task.get("effort_assessment"))

    result = {
        "policy_version": version,
        "worker_thinking": thinking,
        "budget_mode": mode,
        "progress": progress,
        "timeout_seconds": timeout,
        "effort_assessment": assessment,
    }
    if version == 2:
        raw = task.get('closeout', {})
        if not isinstance(raw, dict) or set(raw) - {'warn_seconds', 'deadline_seconds'}:
            raise ValueError('closeout must contain only warn_seconds/deadline_seconds')
        closeout = dict(_CLOSEOUT_PROFILES[thinking])
        if mode == 'custom':
            closeout.update(raw)
        if any(not _is_finite_positive_number(v) or v > 3600 for v in closeout.values()):
            raise ValueError('closeout intervals must be finite positive numbers <= 3600')
        if closeout['warn_seconds'] >= closeout['deadline_seconds']:
            raise ValueError('closeout.warn_seconds must be < closeout.deadline_seconds')
        result['closeout'] = closeout
    return result


def _resolve_thinking(task: Dict[str, Any]) -> str:
    thinking = task.get("worker_thinking", "low")
    if not isinstance(thinking, str) or thinking not in _SUPPORTED_THINKING:
        raise ValueError(
            "unsupported worker_thinking %r; expected one of %r"
            % (thinking, sorted(_SUPPORTED_THINKING))
        )
    return thinking


def _resolve_budget_mode(task: Dict[str, Any]) -> str:
    has_progress = "progress" in task
    has_timeout = "timeout_seconds" in task
    if "budget_mode" in task:
        mode = task["budget_mode"]
        if mode not in _SUPPORTED_BUDGET_MODES:
            raise ValueError(
                "unsupported budget_mode %r; expected one of %r"
                % (mode, sorted(_SUPPORTED_BUDGET_MODES))
            )
        return mode
    # Unspecified mode: legacy numeric fields signal a custom budget;
    # otherwise default to auto.
    return "custom" if (has_progress or has_timeout or "closeout" in task) else "auto"


def _resolve_budgets(
    task: Dict[str, Any], thinking: str, mode: str
) -> Tuple[Dict[str, Any], int]:
    profile = _EFFORT_PROFILES[thinking]
    timeout = _EFFORT_TIMEOUTS[thinking]

    if mode == "auto":
        # Select ONLY profile numbers; intentionally replace any stale numeric
        # progress/timeout values copied into the input (they are never
        # validated). Unknown progress keys are still rejected.
        _checked_progress_keys(task)
        progress = dict(profile)
    else:  # custom
        progress = dict(profile)
        if "progress" in task:
            raw = task["progress"]
            if not isinstance(raw, dict):
                raise ValueError("progress must be a dict when provided")
            for key, value in raw.items():
                if key not in _SUPPORTED_PROGRESS_KEYS:
                    raise ValueError("unknown progress key %r" % (key,))
                progress[key] = value
        if "timeout_seconds" in task:
            timeout = task["timeout_seconds"]

    _validate_progress(progress)
    timeout = _validate_timeout(timeout)
    return progress, timeout


def _checked_progress_keys(task: Dict[str, Any]) -> None:
    if "progress" not in task:
        return
    raw = task["progress"]
    if not isinstance(raw, dict):
        raise ValueError("progress must be a dict when provided")
    unknown = set(raw) - _SUPPORTED_PROGRESS_KEYS
    if unknown:
        raise ValueError("unknown progress key(s): %r" % (sorted(unknown),))


def _is_finite_positive_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and value > 0


def _validate_progress(progress: Dict[str, Any]) -> None:
    for key in _SUPPORTED_PROGRESS_KEYS:
        if not _is_finite_positive_number(progress[key]):
            raise ValueError("progress %r must be a finite positive number" % (key,))
    if not (progress["warn_seconds"] < progress["stall_seconds"]):
        raise ValueError("progress.warn_seconds must be < progress.stall_seconds")
    # first_write and stall are NOT required to fit within the total timeout:
    # an explicitly shorter total budget is allowed and simply takes precedence.


def _validate_timeout(timeout: Any) -> int:
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        raise ValueError("timeout_seconds must be an integer")
    if not (_MIN_TIMEOUT <= timeout <= _MAX_TIMEOUT):
        raise ValueError(
            "timeout_seconds must be an integer in [%d, %d]"
            % (_MIN_TIMEOUT, _MAX_TIMEOUT)
        )
    return timeout


def _resolve_assessment(assessment: Any) -> Dict[str, Any]:
    if assessment is None:
        return dict(_DEFAULT_ASSESSMENT)
    if not isinstance(assessment, dict):
        raise ValueError("effort_assessment must be a dict when provided")
    # Audit claim only, not authentication. Deep-copy so optional extra fields
    # are preserved and never share storage with the caller's input.
    result = deepcopy(assessment)
    by = result.get("by")
    if by not in _SUPPORTED_ASSESSMENT_BY:
        raise ValueError(
            "effort_assessment.by %r unsupported; expected one of %r"
            % (by, sorted(_SUPPORTED_ASSESSMENT_BY))
        )
    reason = result.get("reason")
    if (
        not isinstance(reason, str)
        or not reason.strip()
        or len(reason) > _MAX_REASON_LEN
    ):
        raise ValueError(
            "effort_assessment.reason must be a nonempty string <= %d chars"
            % (_MAX_REASON_LEN,)
        )
    return result
