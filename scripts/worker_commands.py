"""Prepared worker test envelopes shared by dispatch and policy verification."""
from pathlib import Path
import shlex


def logged_worker_test_command(argv, log_root):
    """Preserve each invocation's bytes/status without changing its test argv."""
    return '\n'.join([
        'umask 077',
        'team_dev_log_dir=$(mktemp -d ' + shlex.quote(str(Path(log_root) / 'check-XXXXXXXX')) + ') || exit 125',
        'printf \'WORKER_TEST_LOG_DIR=%s\\n\' "$team_dev_log_dir" || exit 125',
        'if ' + shlex.join(argv) + ' >"$team_dev_log_dir/stdout.log" 2>"$team_dev_log_dir/stderr.log"; then',
        '  team_dev_test_exit=0',
        'else',
        '  team_dev_test_exit=$?',
        'fi',
        'printf \'%s\\n\' "$team_dev_test_exit" >"$team_dev_log_dir/exit-code" || exit 125',
        'exit "$team_dev_test_exit"',
    ])
