"""Isolated demonstration, not a generic validator of arbitrary project semantics."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parent
SOURCE_FILES = ('producer.py', 'generate_fixture.py', 'consumer.mjs',
                'consumer.test.mjs', 'preview.fixture.json', 'check_contract.py')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def manifest():
    return {name: sha((ROOT / name).read_bytes()) for name in SOURCE_FILES}


def checked(argv, env, commands):
    result = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, timeout=30)
    commands.append({'argv': argv, 'exit_code': result.returncode,
                     'stdout': result.stdout.decode('utf-8', errors='replace'),
                     'stderr': result.stderr.decode('utf-8', errors='replace')})
    if result.returncode:
        raise RuntimeError('Acceptance command failed with exit ' + str(result.returncode))
    return result.stdout


def check(node, receipt):
    resolved_node = shutil.which(node)
    if not resolved_node:
        raise RuntimeError('NOT VERIFIED: Node.js runtime is unavailable')
    before = manifest()
    receipt['sources_sha256'] = before
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    commands = receipt['commands']
    receipt['runtime_versions'] = {
        'python': sys.version.split()[0],
        'node': checked([resolved_node, '--version'], env, commands).decode().strip(),
    }
    with tempfile.TemporaryDirectory(prefix='team-dev-contract-') as tmp:
        generated = checked([sys.executable, str(ROOT / 'generate_fixture.py')], env, commands)
        # Validate JSON but compare bytes: no normalization silently changes types.
        json.loads(generated)
        committed = (ROOT / 'preview.fixture.json').read_bytes()
        receipt['generated_fixture_sha256'] = sha(generated)
        receipt['committed_fixture_sha256'] = sha(committed)
        if generated != committed:
            raise RuntimeError('Committed fixture differs from current producer output; review regeneration')
        generated_path = Path(tmp) / 'preview.generated.json'
        generated_path.write_bytes(generated)
        output = checked([resolved_node, '--test-reporter=tap', str(ROOT / 'consumer.test.mjs'), str(generated_path)], env, commands)
        counts = {}
        for key in ('tests', 'pass', 'fail', 'cancelled', 'skipped', 'todo'):
            matches = re.findall(r'^# ' + key + r' (\d+)\r?$', output.decode(), re.MULTILINE)
            if len(matches) != 1:
                raise RuntimeError('NOT VERIFIED: consumer test collection summary is missing or ambiguous')
            counts[key] = int(matches[0])
        receipt['consumer_test_counts'] = counts
        if counts['tests'] <= 0 or counts['pass'] != counts['tests'] or any(
                counts[key] for key in ('fail', 'cancelled', 'skipped', 'todo')):
            raise RuntimeError('NOT VERIFIED: consumer tests must collect and pass without skips')
        if generated_path.read_bytes() != generated:
            raise RuntimeError('Consumer test changed its generated input')
    if manifest() != before:
        raise RuntimeError('Source changed during contract acceptance')
    receipt['source_stable'] = True
    receipt['passed'] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node', default='node', help='Existing Node.js command or absolute path')
    args = parser.parse_args()
    receipt = {'scope': 'isolated_example', 'passed': False, 'normalization': [], 'commands': []}
    try:
        check(args.node, receipt)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        receipt['error'] = str(exc)
    print(json.dumps(receipt, indent=2, ensure_ascii=False))
    return 0 if receipt['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
