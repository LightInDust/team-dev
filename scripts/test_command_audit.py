import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from command_audit import CommandAudit, MAX_CHARS, inspect_bash


class CommandAuditTests(unittest.TestCase):
    def test_logged_envelope_failure_and_echo_tail_have_different_real_exit(self):
        from team_dev import logged_worker_test_command
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = logged_worker_test_command([sys.executable, '-B', '-c',
                'import sys; print("complete output"); sys.exit(37)'], root)
            changed = original.rsplit('exit "$team_dev_test_exit"', 1)[0] + 'echo "LOG=$team_dev_log_dir"'
            for command, expected_exit, missing_tail in ((original, 37, False), (changed, 0, True)):
                with self.subTest(expected_exit=expected_exit):
                    result = subprocess.run(['/bin/sh', '-c', command], capture_output=True, timeout=10)
                    self.assertEqual(result.returncode, expected_exit, result.stderr)
                    log = Path(result.stdout.decode().splitlines()[0].split('=', 1)[1])
                    self.assertEqual((log/'exit-code').read_text(), '37\n')
                    self.assertEqual((log/'stdout.log').read_text(), 'complete output\n')
                    audit = inspect_bash({'command': command})
                    self.assertEqual(audit['status'], 'unsupported_syntax')
                    self.assertIn('worker_log_envelope_mention', audit['review_cues'])
                    self.assertEqual('worker_log_exit_tail_not_observed' in audit['review_cues'], missing_tail)
                    self.assertEqual(audit['observations'], [])

    def test_exit_tail_is_only_a_cue_not_body_or_status_validation(self):
        prefix = 'team_dev_log_dir=owned; team_dev_test_exit=37; '
        for tail in ('echo "$team_dev_test_exit"', 'exit 0',
                     'echo \'exit "$team_dev_test_exit"\'', 'exit "$team_dev_test_exit"; echo done'):
            self.assertIn('worker_log_exit_tail_not_observed', inspect_bash({'command':prefix+tail})['review_cues'])
        # A reset status followed by the expected tail still needs envelope review.
        audit = CommandAudit()
        audit.event({'type':'tool_execution_start','toolName':'bash', 'commandAudit':inspect_bash(
            {'command':prefix+'team_dev_test_exit=0; exit "$team_dev_test_exit"'})})
        self.assertEqual(audit.summary()['review_cue_counts']['worker_log_exit_tail_not_observed'],0)
        self.assertTrue(audit.summary()['review_required'])
        self.assertEqual(audit.summary()['compliance'],'not_assessed')

    def test_manual_render_and_dynamic_dependency_filters_are_visible(self):
        commands = (
            '"/path with spaces/python3" -B -c "render(Path(\'/tmp/private.png\'))" 2>&1 | head -20',
            'python3 -B - <<\'PY\' 2>&1 | tail -10\nprint("probe")\nPY',
            'TGT=/private/interpreter; "$TGT" -c "import PIL" 2>&1 | head -1',
        )
        for command in commands:
            result = inspect_bash({'command':command})
            self.assertIn('output_filter_pipeline_mention',result['review_cues'])
            self.assertEqual(result['status'],'unsupported_syntax')
            self.assertEqual(result['observations'],[])
            self.assertNotIn('private',json.dumps(result))
        self.assertIn('dynamic_interpreter_probe_mention', inspect_bash({'command':commands[-1]})['review_cues'])
        for command in ('python3 -B probe.py || head saved.log', 'tail -20 saved.log', 'cat saved.log'):
            self.assertNotIn('output_filter_pipeline_mention',inspect_bash({'command':command})['review_cues'])

    def test_quoted_probe_guard_checks_header_not_python_payload_or_app_args(self):
        unguarded = ('python3 -c "print(\'-B\')"', '"/usr/bin/python3" -c "import PIL"',
                     'PYTHONDONTWRITEBYTECODE=1 python3 -I -c "import PIL"',
                     'python3 probe.py -B', 'python3 - <<\'PY\'\nprint(1)\nPY',
                     'env PYTHONDONTWRITEBYTECODE=1 PYTHONDONTWRITEBYTECODE=0 python3 -c "print(1)"')
        guarded = ('python3 -B -c "import PIL"', 'python3 -IB -c "import PIL"',
                   'env PYTHONDONTWRITEBYTECODE=1 python3 -c "import PIL"',
                   'python3 -W ignore -B -c "import PIL"')
        for command in unguarded:
            with self.subTest(command=command):
                self.assertIn('python_header_without_explicit_guard_mention',inspect_bash({'command':command})['review_cues'])
        for command in guarded:
            with self.subTest(command=command):
                self.assertNotIn('python_header_without_explicit_guard_mention',inspect_bash({'command':command})['review_cues'])

    def test_new_scan_is_bounded_unparsed_and_never_executes(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker=Path(tmp)/'never-created'
            audit=inspect_bash({'command':'TGT=$(touch '+str(marker)+'); "$TGT" -c "import PIL" | head -1'})
            self.assertFalse(marker.exists())
            self.assertIn('dynamic_interpreter_probe_mention',audit['review_cues'])
        self.assertEqual(inspect_bash({'command':'echo "unterminated'})['probe_scan_status'],'unparsed')
        for args in ({}, {'command':'python3 -c '+('x'*MAX_CHARS)}):
            result=inspect_bash(args)
            self.assertEqual(result['probe_scan_status'],'not_scanned')
            self.assertEqual(result['review_cues'],[])
        # Cues can come from examples; they are not automatic violation verdicts.
        example=inspect_bash({'command':'echo "python3 -c example | head -1"'})
        self.assertIn('output_filter_pipeline_mention',example['review_cues'])
        self.assertEqual(example['observations'],[])

    def test_v1_and_mixed_event_coverage_remain_explicit(self):
        command='python3 -c "print(1)" | head -1'
        legacy=inspect_bash({'command':command},version=1)
        self.assertEqual(set(legacy), {'version','status','observations','text_observations',
                         'text_observation_status','command_sha256','command_chars'})
        self.assertEqual(legacy['observations'],[])
        audit=CommandAudit()
        audit.event({'type':'tool_execution_start','toolName':'bash','commandAudit':legacy})
        self.assertEqual(audit.summary()['version'],1)
        self.assertNotIn('review_cue_counts',audit.summary())
        audit.event({'type':'tool_execution_start','toolName':'bash','commandAudit':inspect_bash({'command':command})})
        summary=audit.summary()
        self.assertEqual(summary['version'],2)
        self.assertEqual(summary['probe_scanned_calls'],1)
        self.assertEqual(summary['probe_unavailable_calls'],1)
        self.assertEqual(summary['review_cue_counts']['output_filter_pipeline_mention'],1)
        self.assertTrue(summary['review_required'])
        for version in (None, True, 0, 3):
            with self.assertRaises(ValueError):inspect_bash({'command':command},version=version)

    def test_reported_literal_command_is_flagged_without_running_or_copying_it(self):
        command = ('cd /private/example/snapshot && python3 -m unittest -v test_build_ledger '
                   '2>&1 | tail -3 && rm -rf ./__pycache__ && git status --short')
        result = inspect_bash({'command': command})
        self.assertEqual(result['status'], 'inspected_literal_subset')
        self.assertEqual(set(result['observations']), {'requested_recursive_rm', 'test_output_pipeline',
                                                       'python_without_explicit_bytecode_guard'})
        self.assertEqual(result['command_sha256'], hashlib.sha256(command.encode()).hexdigest())
        self.assertNotIn('/private/example', json.dumps(result))
        self.assertNotIn('__pycache__', json.dumps(result))

    def test_plain_recursive_rm_spelling_and_command_position(self):
        for command in ('/bin/rm -fr cache', 'command rm -R cache', 'exec rm --recursive cache',
                        'env FLAG=private rm -rf cache', 'false && rm -rf cache'):
            with self.subTest(command=command):
                self.assertIn('requested_recursive_rm', inspect_bash({'command': command})['observations'])
        for command in ('echo rm -rf cache', 'rm -- -rf', 'rm cache', 'rmdir cache'):
            with self.subTest(command=command):
                self.assertNotIn('requested_recursive_rm', inspect_bash({'command': command})['observations'])

    def test_complex_or_quoted_text_is_uninspected_not_a_compliance_pass(self):
        for command in ('echo "rm -rf cache"', "echo ';' rm -rf cache", 'bash -c "rm -rf cache"',
                        'python3 - <<PY\nprint(1)\nPY', 'eval $CLEANUP', 'rm -rf cache # comment',
                        'echo x > file', 'rm -rf cache &'):
            with self.subTest(command=command):
                result = inspect_bash({'command': command})
                self.assertEqual(result['status'], 'unsupported_syntax')
                self.assertEqual(result['observations'], [])

    def test_missing_and_oversized_args_are_explicit(self):
        for args in (None, [], {'cmd': 'PRIVATE'}, {'command': ''}, {'command': False}):
            self.assertEqual(inspect_bash(args)['status'], 'missing_command')
        self.assertEqual(inspect_bash({'command': 'x' * (MAX_CHARS + 1)})['status'], 'over_limit')

    def test_unknown_wrapper_options_require_review_instead_of_zero_flag_coverage(self):
        for command in ('env -i rm -rf cache', 'env -u PYTHONDONTWRITEBYTECODE python3 -m unittest',
                        'command -p rm -rf cache', 'exec -a alias rm -rf cache',
                        'env FLAG=value -i python3 -m unittest', 'command env -i rm -rf cache',
                        'env -- rm -rf cache', 'env'):
            with self.subTest(command=command):
                result = inspect_bash({'command': command})
                self.assertEqual(result['status'], 'unsupported_syntax')
                audit = CommandAudit()
                audit.event({'type': 'tool_execution_start', 'toolName': 'bash', 'commandAudit': result})
                self.assertEqual(audit.summary()['status_counts']['unsupported_syntax'], 1)
                self.assertTrue(audit.summary()['review_required'])

    def test_mixed_unsupported_wrapper_retains_known_observations_before_and_after_it(self):
        command = 'rm -rf cache && env -i true; python3 -m unittest | tail -3'
        result = inspect_bash({'command': command})
        self.assertEqual(result['status'], 'unsupported_syntax')
        self.assertEqual(set(result['observations']), {'requested_recursive_rm', 'test_output_pipeline',
                                                       'python_without_explicit_bytecode_guard'})

    def test_explicit_bytecode_guard_is_not_confused_with_application_flags(self):
        guarded = ('env PYTHONDONTWRITEBYTECODE=1 python3 -m unittest',
                   'PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m unittest',
                   'python3 -IB -m unittest', 'python3 -W ignore::BytesWarning -B -m unittest')
        unguarded = ('python3 -m unittest -B', 'python3 -Wignore::BytesWarning -m unittest',
                     'env PYTHONDONTWRITEBYTECODE=1 python3 -I -m unittest',
                     'env PYTHONDONTWRITEBYTECODE=1 python3 -E -m unittest',
                     'env PYTHONDONTWRITEBYTECODE=1 PYTHONDONTWRITEBYTECODE=0 python3 -m unittest')
        for command in guarded:
            with self.subTest(command=command):
                self.assertNotIn('python_without_explicit_bytecode_guard', inspect_bash({'command': command})['observations'])
        for command in unguarded:
            with self.subTest(command=command):
                self.assertIn('python_without_explicit_bytecode_guard', inspect_bash({'command': command})['observations'])

    def test_pipeline_marker_does_not_assert_test_exit_status(self):
        for command in ('python3 -B -m unittest | tail -3', 'pytest | tail -3'):
            self.assertIn('test_output_pipeline', inspect_bash({'command': command})['observations'])
        for command in ('python3 -B -m unittest && git status', 'python3 helper.py -m unittest | tail -3'):
            self.assertNotIn('test_output_pipeline', inspect_bash({'command': command})['observations'])

    def test_summary_keeps_observations_separate_from_unknown_and_compliance(self):
        audit = CommandAudit()
        for command in ('false && rm -rf cache', 'echo "PRIVATE"', 'python3 -B -m unittest'):
            audit.event({'type': 'tool_execution_start', 'toolName': 'bash',
                         'commandAudit': inspect_bash({'command': command})})
        audit.event({'type': 'tool_execution_start', 'toolName': 'bash'})  # legacy compact log
        audit.event({'type': 'tool_execution_start', 'toolName': 'read'})
        audit.event({'type': 'message_end', 'message': {'content': 'no shell cleanup'}})
        summary = audit.summary()
        self.assertEqual(summary['bash_calls'], 4)
        self.assertEqual(summary['status_counts']['missing_command'], 1)
        self.assertEqual(summary['status_counts']['unsupported_syntax'], 1)
        self.assertEqual(summary['observation_counts']['requested_recursive_rm'], 1)
        self.assertTrue(summary['review_required'])
        self.assertEqual(summary['compliance'], 'not_assessed')
        self.assertNotIn('PRIVATE', json.dumps(summary))

    def test_zero_patterns_never_claims_compliance(self):
        audit = CommandAudit()
        audit.event({'type': 'tool_execution_start', 'toolName': 'bash',
                     'commandAudit': inspect_bash({'command': 'python3 -B -m unittest'})})
        self.assertFalse(audit.summary()['review_required'])
        self.assertEqual(audit.summary()['compliance'], 'not_assessed')

    def test_explicit_compile_text_remains_visible_with_unsupported_heredoc(self):
        command = ('env PYTHONDONTWRITEBYTECODE=1 python3 -B - <<\x27PY\x27\n'
                   'import py_compile\npy_compile.compile("source.py", cfile="/tmp/private.pyc")\nPY')
        result = inspect_bash({'command': command})
        self.assertEqual(result['status'], 'unsupported_syntax')
        self.assertEqual(result['observations'], [])
        self.assertEqual(set(result['text_observations']),
                         {'explicit_bytecode_writer_mention', 'absolute_tmp_path_mention'})
        self.assertNotIn('private.pyc', json.dumps(result))
        audit = CommandAudit()
        audit.event({'type': 'tool_execution_start', 'toolName': 'bash', 'commandAudit': result})
        self.assertEqual(audit.summary()['status_counts']['unsupported_syntax'], 1)
        self.assertEqual(audit.summary()['text_observation_counts']['explicit_bytecode_writer_mention'], 1)
        self.assertTrue(audit.summary()['review_required'])
        self.assertEqual(audit.summary()['compliance'], 'not_assessed')

    def test_nonrecursive_tmp_cleanup_is_a_text_cue_without_recursive_claim(self):
        result = inspect_bash({'command': 'rm -f /tmp/owned-or-unowned.pyc && echo removed'})
        self.assertEqual(result['status'], 'inspected_literal_subset')
        self.assertEqual(result['observations'], [])
        self.assertEqual(result['text_observations'], ['absolute_tmp_path_mention'])
        audit = CommandAudit()
        audit.event({'type': 'tool_execution_start', 'toolName': 'bash', 'commandAudit': result})
        self.assertTrue(audit.summary()['review_required'])
        self.assertEqual(audit.summary()['observation_counts']['requested_recursive_rm'], 0)

    def test_text_mentions_do_not_prove_execution_or_lifecycle(self):
        for command in ('echo "py_compile /tmp/example"', '# compileall /var/tmp/example',
                        'cat /private/tmp/authorized-input', 'echo /private/var/tmp/example'):
            with self.subTest(command=command):
                result = inspect_bash({'command': command})
                self.assertIn('absolute_tmp_path_mention', result['text_observations'])
        for command in ('cat relative/tmp/file', 'echo /tmp-prefix/file', 'echo https://host/tmp/file'):
            with self.subTest(command=command):
                self.assertEqual(inspect_bash({'command': command})['text_observations'], [])
        self.assertEqual(inspect_bash({'command': 'py_compile ' + 'x' * MAX_CHARS})['text_observations'], [])
        audit = CommandAudit()
        audit.event({'type': 'tool_execution_start', 'toolName': 'bash',
                     'commandAudit': {'version': 1, 'status': 'unsupported_syntax', 'observations': []}})
        self.assertTrue(audit.summary()['review_required'])
        self.assertEqual(sum(audit.summary()['text_observation_counts'].values()), 0)

    def test_explicit_compile_really_writes_despite_B_and_environment_guard(self):
        # All producers, consumers and checks stay inside the owned temp context.
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source.py'
            output = Path(tmp) / 'compiled.pyc'
            source.write_text('VALUE = 7\n')
            command = [sys.executable, '-B', '-c',
                       'import py_compile,sys; py_compile.compile(sys.argv[1],cfile=sys.argv[2],doraise=True)',
                       str(source), str(output)]
            result = subprocess.run(command, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'),
                                    capture_output=True, timeout=10, check=True)
            self.assertEqual(result.returncode, 0)
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 0)
        self.assertFalse(Path(tmp).exists())

    def test_legacy_zero_flags_have_unavailable_text_coverage(self):
        audit = CommandAudit()
        audit.event({'type': 'tool_execution_start', 'toolName': 'bash',
                     'commandAudit': {'version': 1, 'status': 'inspected_literal_subset', 'observations': []}})
        audit.event({'type': 'tool_execution_start', 'toolName': 'bash',
                     'commandAudit': inspect_bash({'command': 'echo hello'})})
        self.assertEqual(audit.summary()['text_scanned_calls'], 1)
        self.assertEqual(audit.summary()['text_unavailable_calls'], 1)
        self.assertEqual(sum(audit.summary()['text_observation_counts'].values()), 0)
        self.assertTrue(audit.summary()['review_required'])
        self.assertEqual(audit.summary()['compliance'], 'not_assessed')


if __name__ == '__main__':
    unittest.main()
