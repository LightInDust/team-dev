"""Exact-test reference labels and payload-free correction hints, never execution."""
from worker_bash import commands, inspect, command_sha


def catalog(task, run):
    _, direct, logged = commands(task, run)
    return [{'id': f'T{index}.{kind}', 'tool': 'bash', 'arguments': {'command': command}}
            for index, pair in enumerate(zip(direct, logged), 1)
            for kind, command in zip(('direct', 'logged'), pair)]


def validate_catalog(value, rules):
    if not isinstance(value, list) or not value or len(value) % 2:
        raise ValueError('Invalid exact-test catalog')
    hashes = set()
    for index, entry in enumerate(value):
        label = f'T{index // 2 + 1}.' + ('direct' if index % 2 == 0 else 'logged')
        if (not isinstance(entry, dict) or set(entry) != {'id', 'tool', 'arguments'} or
                entry['id'] != label or entry['tool'] != 'bash' or
                not isinstance(entry['arguments'], dict) or set(entry['arguments']) != {'command'} or
                inspect(entry['arguments'], rules)['status'] != 'allowed'):
            raise ValueError('Exact-test catalog is outside frozen commands')
        hashes.add(command_sha(entry['arguments']['command'].strip()))
    if hashes != set(rules['allowed_command_sha256']):
        raise ValueError('Exact-test catalog is incomplete')
    return value


def correction_hint(args, rules, entries):
    checked = inspect(args, rules)
    match = checked.get('command_match')
    fields = [f"command_match={'yes' if match is True else 'no' if match is False else 'unknown'}",
              'argument_issues=' + (','.join(checked.get('argument_issues', [])) or 'none'),
              'cwd=' + checked.get('cwd_status', 'unavailable')]
    command = args.get('command') if isinstance(args, dict) else None
    # Keep work linear in a bounded catalog prefix. The hint never grants permission.
    budget, best, ambiguous = 131072, None, False
    if isinstance(command, str) and len(command) <= 32768:
        actual = command.strip()
        for entry in entries:
            expected = entry['arguments']['command'].strip()
            budget -= len(expected)
            if budget < 0:
                break
            prefix = 0
            while prefix < min(len(actual), len(expected)) and actual[prefix] == expected[prefix]:
                prefix += 1
            suffix = 0
            while (suffix < min(len(actual), len(expected)) - prefix and
                   actual[len(actual) - 1 - suffix] == expected[len(expected) - 1 - suffix]):
                suffix += 1
            score = (prefix + suffix) / max(len(actual), len(expected), 1)
            item = (score, entry['id'], prefix, len(expected), expected)
            if best is None or score > best[0]:
                best, ambiguous = item, False
            elif score == best[0] and expected != best[4]:
                ambiguous = True
        if best is not None and best[0] >= .5 and not ambiguous:
            fields += ['reference=' + best[1],
                       'first_difference_codepoint=' + ('none' if actual == best[4] else str(best[2])),
                       f'expected_length={best[3]}', f'received_length={len(actual)}']
    return ('Exact-test hint: ' + '; '.join(fields) +
            '. References identify worker_test_invocations; copy its complete arguments object unchanged. '
            'Do not execute a reference label or rebuild a logging envelope. Hint is not permission.')
