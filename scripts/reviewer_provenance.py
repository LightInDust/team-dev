"""Bind parent-observed dispatch evidence; never infer the serving model SKU."""
import argparse
import hashlib
import json
from pathlib import Path
import re

POLICY = {'version': 1, 'requested_model': 'gpt-5.6-sol', 'requested_effort': 'max',
          'dispatch': 'parent_tool_record_required', 'runtime_identity': 'unknown_no_adapter'}


def enabled(task, meta):
    key = 'reviewer_provenance_policy'
    if key not in task and key not in meta:
        return False
    if any(not isinstance(obj.get(key), dict) or obj[key] != POLICY or
           type(obj[key].get('version')) is not int for obj in (task, meta)):
        raise ValueError('Reviewer provenance policy changed; prepare a fresh run')
    return True


def validate(evidence_root, review):
    """Check local integrity/consistency, not authentication of editable records.

    The coordinator saves a narrow tool-call extract. Its source is manually
    accountable; neither a self-report nor generic identity prose is that source.
    No current adapter establishes the actual serving model or reasoning effort.
    """
    value = review.get('reviewer_provenance')
    if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1:
        raise ValueError('Require reviewer_provenance version 1')
    if (value.get('requested_model') != POLICY['requested_model'] or
            value.get('requested_effort') != POLICY['requested_effort'] or
            review.get('model') != value['requested_model'] or review.get('effort') != value['requested_effort']):
        raise ValueError('Requested reviewer model/effort or compatibility labels disagree')
    if (value.get('actual_verified_model') != 'unknown' or value.get('actual_verified_effort') != 'unknown' or
            value.get('verification_basis') != 'parent_dispatch_only'):
        raise ValueError('No runtime identity adapter: actual model/effort must remain unknown')
    if not isinstance(value.get('self_reported_model'), str) or not value['self_reported_model'].strip():
        raise ValueError('Self-reported model must be explicit; use unknown when absent')
    if type(value.get('reused_agent')) is not bool:
        raise ValueError('Declare whether the original dispatched agent was reused')
    reference = value.get('dispatch_evidence')
    if not isinstance(reference, dict):
        raise ValueError('Require hash-bound parent dispatch evidence')
    filename, expected = reference.get('file'), reference.get('sha256')
    if (not isinstance(filename, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,120}\.json', filename) or
            not isinstance(expected, str) or not re.fullmatch(r'[a-f0-9]{64}', expected)):
        raise ValueError('Dispatch evidence requires a local JSON basename and SHA-256')
    path = Path(evidence_root) / filename
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
        raise ValueError('Dispatch evidence is missing, linked or oversized')
    raw = path.read_bytes()
    if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError('Dispatch evidence hash changed')
    record = json.loads(raw)
    if (not isinstance(record, dict) or type(record.get('version')) is not int or record['version'] != 1 or
            record.get('tool') != 'collaboration.spawn_agent'):
        raise ValueError('Require an original collaboration.spawn_agent tool record')
    source, request, response = record.get('source'), record.get('request'), record.get('response')
    if (not isinstance(source, dict) or source.get('kind') != 'parent_tool_observation' or
            not isinstance(source.get('reference'), str) or not source['reference'].strip()):
        raise ValueError('Dispatch source must reference a parent-observed tool call')
    if (not isinstance(request, dict) or request.get('model') != value['requested_model'] or
            request.get('reasoning_effort') != value['requested_effort']):
        raise ValueError('Dispatch did not explicitly request the required model/effort')
    fork = request.get('fork_turns')
    if not isinstance(fork, str) or not (fork == 'none' or re.fullmatch(r'[1-9][0-9]*', fork)):
        raise ValueError('Explicit model dispatch requires fresh or bounded fork context')
    task_name, agent_id = request.get('task_name'), value.get('agent_id')
    if (not isinstance(task_name, str) or not re.fullmatch(r'[a-z0-9_]+', task_name) or
            not isinstance(agent_id, str) or not agent_id.startswith('/') or
            not agent_id.endswith('/' + task_name) or response != {'task_name': agent_id}):
        raise ValueError('Review agent identity does not match accepted spawn response')
    return {'requested_model': value['requested_model'], 'requested_effort': value['requested_effort'],
            'agent_id': agent_id, 'dispatch_evidence_sha256': expected,
            'self_reported_model': value['self_reported_model'],
            'actual_verified_model': 'unknown', 'actual_verified_effort': 'unknown',
            'verification_basis': 'parent_dispatch_only'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence-root', required=True)
    parser.add_argument('--review', required=True)
    args = parser.parse_args()
    print(json.dumps(validate(Path(args.evidence_root), json.loads(Path(args.review).read_text())), indent=2))
