"""Exact reviewer fields and fail-closed publication of coordinator receipts."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from reviewer_provenance import POLICY, validate


JSON_BASENAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,120}\.json')

# Constant, allowlisted diagnostics. Neither echoes a supplied path or content.
DISPATCH_BASENAME_ERROR = ('Review handoff failed: --dispatch needs a JSON basename under --evidence-root '
                           '(safe example: --dispatch reviewer-dispatch.json).\n')
GENERIC_FAILURE = ('Review handoff failed. Preserve existing files; inspect validator diagnostics '
                   'and output state before retrying.\n')


def is_local_json_basename(filename):
    return isinstance(filename, str) and JSON_BASENAME.fullmatch(filename) is not None


def local_json(root, filename):
    if not is_local_json_basename(filename):
        raise ValueError('Require a local JSON basename')
    return Path(root) / filename


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON keys are not supported')
        result[key] = value
    return result


def read_object(root, filename, limit=1024 * 1024):
    path = local_json(root, filename)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError('Input JSON is missing, linked or oversized')
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('Input JSON is oversized')
    value = json.loads(raw, object_pairs_hook=unique_keys)
    if not isinstance(value, dict):
        raise ValueError('Input JSON must be an object')
    return value, hashlib.sha256(raw).hexdigest()


def reviewer_fields(root, dispatch_file, *, reused_agent=False):
    """Construct metadata from an actual saved dispatch, never a PASS verdict."""
    if type(reused_agent) is not bool:
        raise ValueError('reused_agent must be a boolean')
    record, digest = read_object(root, dispatch_file, limit=65536)
    response = record.get('response')
    agent = response.get('task_name') if isinstance(response, dict) else None
    result = {'model': POLICY['requested_model'], 'effort': POLICY['requested_effort'],
              'reviewer_provenance': {
                  'version': 1, 'requested_model': POLICY['requested_model'],
                  'requested_effort': POLICY['requested_effort'], 'agent_id': agent,
                  'reused_agent': reused_agent,
                  'dispatch_evidence': {'file': dispatch_file, 'sha256': digest},
                  'self_reported_model': 'unknown', 'actual_verified_model': 'unknown',
                  'actual_verified_effort': 'unknown', 'verification_basis': 'parent_dispatch_only'}}
    validate(root, result)
    return result


def write_receipt(root, review_file, expected_review_sha256, draft_file, output_file):
    """Publish only after PASS and live provenance validation; never certify tests.

    The nested coordinator payload remains the coordinator's assertion. This
    wrapper checks review/dispatch consistency, not candidate/test semantics.
    Files remain cooperative evidence, not protection against same-user races.
    """
    root = Path(root)
    output = local_json(root, output_file)
    if output.exists() or output.is_symlink():
        raise ValueError('Receipt already exists; preserve it and use a new basename')
    if not isinstance(expected_review_sha256, str) or not re.fullmatch(r'[a-f0-9]{64}', expected_review_sha256):
        raise ValueError('Require the expected review SHA-256')
    review, review_hash = read_object(root, review_file)
    if review_hash != expected_review_sha256:
        raise ValueError('Review hash changed')
    if review.get('verdict') != 'pass':
        raise ValueError('A passing review is required to publish a receipt')
    payload, payload_hash = read_object(root, draft_file)
    if not payload:
        raise ValueError('Coordinator payload must not be empty')
    validation = validate(root, review)  # An exception must prevent all publication.
    dispatch_file = review['reviewer_provenance']['dispatch_evidence']['file']
    _, dispatch_hash = read_object(root, dispatch_file, limit=65536)
    if dispatch_hash != validation['dispatch_evidence_sha256']:
        raise ValueError('Dispatch evidence hash changed')
    if output_file in {review_file, dispatch_file, draft_file}:
        raise ValueError('Receipt output cannot replace an input')
    receipt = {
        'receipt_format': 'team_dev_review_validated_receipt_v1',
        'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'review_validation': {
            'scope': 'review_verdict_and_dispatch_provenance_only', 'verdict': 'pass',
            'review': {'file': review_file, 'sha256': review_hash},
            'dispatch': {'file': dispatch_file, 'sha256': dispatch_hash}, 'result': validation},
        'coordinator_payload_source': {'file': draft_file, 'sha256': payload_hash},
        'coordinator_payload': payload,
        'limits': [
            'Coordinator payload claims, candidate/source hashes, target tests, scientific acceptance and deployment are not validated by this gate.',
            'Provenance validation does not establish the actual serving model or effort.',
            'This receipt does not finalize a failed worker or replace any prior receipt.',
        ],
    }
    raw = (json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.review-receipt-', dir=root, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        # Recheck after staging, immediately before exclusive publication.
        validate(root, review)
        if read_object(root, review_file)[1] != review_hash or read_object(root, draft_file)[1] != payload_hash:
            raise ValueError('Receipt inputs changed during validation')
        if read_object(root, dispatch_file, limit=65536)[1] != dispatch_hash:
            raise ValueError('Dispatch changed during validation')
        os.link(temporary, output)  # Atomic create; EEXIST never overwrites an old receipt.
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {'receipt_file': output_file, 'receipt_sha256': hashlib.sha256(raw).hexdigest(),
            'validation_scope': 'review_verdict_and_dispatch_provenance_only'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    template = sub.add_parser('template', help='Print exact reviewer metadata; no verdict or file write')
    template.add_argument('--evidence-root', required=True)
    template.add_argument('--dispatch', required=True)
    template.add_argument('--reused-agent', action='store_true')
    receipt = sub.add_parser('receipt', help='Validate PASS/provenance and exclusively publish a new receipt')
    receipt.add_argument('--evidence-root', required=True)
    receipt.add_argument('--review', required=True)
    receipt.add_argument('--expected-review-sha256', required=True)
    receipt.add_argument('--draft', required=True)
    receipt.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        if args.action == 'template':
            if not is_local_json_basename(args.dispatch):
                parser.exit(1, DISPATCH_BASENAME_ERROR)
            result = reviewer_fields(args.evidence_root, args.dispatch, reused_agent=args.reused_agent)
        else:
            result = write_receipt(args.evidence_root, args.review, args.expected_review_sha256, args.draft, args.output)
    except (OSError, ValueError, TypeError):
        parser.exit(1, GENERIC_FAILURE)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
