"""Strict switch authorization from saved assistant errors, not generic diagnostics.

Only recognized billing, transport or gateway errors qualify. An unrelated error in the same attempt prevents automatic replay.
"""
import json
import re

from run_diagnostics import STREAM_ERROR_KINDS, error_kind, http_error_status

# Explicit gateway and request-timeout responses.
NETWORK_HTTP_STATUSES = frozenset({408, 502, 503, 504})

QUOTA_CODES = ('insufficient_quota', 'billing_hard_limit_reached')


def _quota_body(value):
    # Accept a JSON error object after the leading HTTP status, not examples in prose.
    # OMP's OpenAI adapter can flatten the structured code onto a final detail line.
    detail = value.strip().splitlines()
    if len(detail) > 1:
        match = re.search(r'\((?:type|code)=([a-z_]+)\)\s*$', detail[-1])
        if match and match[1] in QUOTA_CODES:
            return True
    prefix, sep, body = value.partition('{')
    if not sep or not re.fullmatch(r'\s*(?:HTTP(?:/[0-9](?:\.[0-9])?)?\s+|Error code:\s*)?429'
                                   r'(?:\s+Too Many Requests)?[\s:]*', prefix, re.I):
        return False
    try:
        payload = json.loads(sep + body)
    except (ValueError, RecursionError):
        return False
    error = payload.get('error', payload) if isinstance(payload, dict) else None
    return isinstance(error, dict) and any(error.get(k) in QUOTA_CODES for k in ('code', 'type'))


def _qualifies(value):
    if not isinstance(value, str) or not value.strip():
        return None
    status = http_error_status(value)
    if status == 402:
        return 'billing'
    if status == 429:
        return 'billing' if _quota_body(value) else None
    if status is not None:
        return 'network' if status in NETWORK_HTTP_STATUSES else None
    if value.strip().lower() == 'unable to connect. is the computer able to access the url?':
        return 'network'
    return 'network' if error_kind(value) in STREAM_ERROR_KINDS else None


def fallback_reason(errors):
    """Return 'billing', 'network' or None; all supplied errors must qualify."""
    if not isinstance(errors, list) or not errors:
        return None
    reasons = [_qualifies(value) for value in errors]
    if any(reason is None for reason in reasons):
        return None
    return 'billing' if 'billing' in reasons else 'network'
