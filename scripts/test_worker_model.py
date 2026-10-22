import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import team_dev as bridge
from worker_model import DEFAULT_MODEL, LEGACY_MODEL, frozen_model, model_id
from provider_evidence import provider_access
from run_diagnostics import diagnose_run
from startup_monitor import read_startup


class WorkerModelTests(unittest.TestCase):
    def test_frozen_and_markerless_history_never_uses_new_default(self):
        self.assertEqual(frozen_model({}, {}), LEGACY_MODEL)
        for selected in (LEGACY_MODEL, DEFAULT_MODEL):
            self.assertEqual(frozen_model({'worker_model': selected}, {'worker_model': selected}), selected)
        for task, meta in (
            ({'worker_model': DEFAULT_MODEL}, {}),
            ({}, {'worker_model': DEFAULT_MODEL}),
            ({'worker_model': DEFAULT_MODEL}, {'worker_model': LEGACY_MODEL}),
            ({'worker_model': None}, {'worker_model': None}),
            ({'worker_model': 'other/model'}, {'worker_model': 'other/model'}),
        ):
            with self.subTest(task=task, meta=meta), self.assertRaises(ValueError):
                frozen_model(task, meta)

    def test_new_prepare_default_and_old_run_preservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); repo = root/'repo'; repo.mkdir()
            (repo/'code.py').write_text('value = 1\n')
            bridge.git(repo, 'init', '-q'); bridge.git(repo, 'add', '.')
            bridge.git(repo, '-c', 'user.name=Tests', '-c', 'user.email=test@localhost', 'commit', '-qm', 'baseline')
            task = {'target': {'transport': 'local', 'repo': str(repo)}, 'goal': 'fixture',
                    'allowed_paths': ['code.py'], 'test_commands': [['/usr/bin/true']]}
            task_file = root/'task.json'; bridge.dump(task_file, task)
            with mock.patch.object(bridge, 'RUNS', root/'runs'):
                run = Path(bridge.prepare(task_file)['run'])
                prepared, meta = bridge.load(run/'task.json'), bridge.load(run/'metadata.json')
                self.assertEqual(frozen_model(prepared, meta), DEFAULT_MODEL)
                # A preserved old receipt remains old even after another new preparation.
                prepared['worker_model'] = meta['worker_model'] = LEGACY_MODEL
                bridge.dump(run/'task.json', prepared); bridge.dump(run/'metadata.json', meta)
                old = {p.name: p.read_bytes() for p in (run/'task.json', run/'metadata.json')}
                bridge.prepare(task_file)
                self.assertEqual(old, {p.name: p.read_bytes() for p in (run/'task.json', run/'metadata.json')})
                task['worker_model'] = LEGACY_MODEL; bridge.dump(task_file, task)
                with self.assertRaises(ValueError): bridge.prepare(task_file)

    def test_delivery_requires_the_frozen_model_not_any_supported_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for expected in (LEGACY_MODEL, DEFAULT_MODEL):
                task = {'worker_model': expected}
                meta = {'worker_model': expected, 'process': 'exited', 'exit_code': 0}
                bridge.dump(root/'task.json', task)
                for actual in (LEGACY_MODEL, DEFAULT_MODEL):
                    bridge.dump(root/'worker-result.json', {'models': [model_id(actual)],
                        'errors': [], 'tools': ['read'], 'final_text': 'synthetic result'})
                    if actual == expected:
                        bridge.require_worker(root, meta)
                    else:
                        with self.assertRaises(RuntimeError): bridge.require_worker(root, meta)

    def test_start_worker_resume_reject_changed_model_before_credentials_launch_or_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = {'worker_model': DEFAULT_MODEL}
            meta = {'worker_model': LEGACY_MODEL, 'process': 'not_started'}
            bridge.dump(root/'task.json', task); bridge.dump(root/'metadata.json', meta)
            with mock.patch.object(bridge, 'validate_worker_options'), mock.patch.object(bridge, 'validate_transport_policy'), mock.patch.object(bridge, 'validate_bash_policy'), mock.patch.object(bridge, 'worker_env') as credentials:
                for operation in (lambda: bridge.start(root), lambda: bridge.worker(root), lambda: bridge.resume(root, root/'unused')):
                    bridge.dump(root/'metadata.json', meta)
                    with self.assertRaises(ValueError): operation()
                credentials.assert_not_called()
            self.assertFalse((root/'invocation.json').exists())
            self.assertFalse((root/'attempt-1').exists())

    def test_supervisor_disables_bytecode_without_parent_environment_guard(self):
        import os
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); bridge.dump(root/'task.json', {})
            bridge.dump(root/'metadata.json', {'process': 'not_started'})
            (root/'import_target.py').write_text('value = 42\n')
            child = root/'child.py'
            child.write_text('import sys, json, import_target\nfrom pathlib import Path\nPath(__file__).with_name("observed.json").write_text(json.dumps({"disabled": sys.dont_write_bytecode, "value": import_target.value}))\n')
            real_popen = subprocess.Popen
            launched = []
            def launch(argv, **kwargs):
                # Exercise the real child argv, with no inherited environment guard.
                env = dict(os.environ); env.pop('PYTHONDONTWRITEBYTECODE', None)
                process = real_popen(argv, env=env, **kwargs)
                process.wait(timeout=10); launched.append(process.returncode)
                return process
            with mock.patch.object(bridge, '__file__', str(child)), mock.patch.object(bridge, 'validate_worker_options'), mock.patch.object(bridge, 'validate_transport_policy'), mock.patch.object(bridge, 'validate_bash_policy'), mock.patch.object(bridge, 'worker_env'), mock.patch.object(bridge.shutil, 'which', return_value='/unused/omp'), mock.patch.object(bridge.subprocess, 'Popen', side_effect=launch), mock.patch.object(bridge, 'process_stamp', return_value='test-reaped'):
                bridge.start(root)
            self.assertEqual(launched, [0])
            self.assertEqual(bridge.load(root/'observed.json'), {'disabled': True, 'value': 42})
            self.assertFalse((root/'__pycache__').exists())

    def test_foreground_runtime_switch_disables_bytecode_without_environment_guard(self):
        import os
        import subprocess
        import sys
        class ExecReplaced(BaseException):
            pass
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); child = root/'child.py'
            (root/'import_target.py').write_text('value = 42\n')
            child.write_text('import sys, json, import_target\nfrom pathlib import Path\nPath(__file__).with_name("observed.json").write_text(json.dumps({"disabled": sys.dont_write_bytecode, "value": import_target.value}))\n')
            def execute(path, argv):
                self.assertEqual(path, sys.executable)
                env = dict(os.environ); env.pop('PYTHONDONTWRITEBYTECODE', None)
                subprocess.run(argv, env=env, check=True, capture_output=True, timeout=10)
                raise ExecReplaced()
            with mock.patch.object(bridge, '__file__', str(child)), mock.patch.object(bridge, 'rpc_python', return_value=sys.executable), mock.patch.object(bridge.sys, 'version_info', (3, 9)), mock.patch.object(bridge.sys, 'argv', ['team_dev.py', 'run', '--run', str(root)]), mock.patch.object(bridge.os, 'execv', side_effect=execute):
                with self.assertRaises(ExecReplaced): bridge.main()
            self.assertEqual(bridge.load(root/'observed.json'), {'disabled': True, 'value': 42})
            self.assertFalse((root/'__pycache__').exists())

    def test_invalid_saved_identity_cannot_match_null_observations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); meta = {'worker_model': None}
            bridge.dump(root/'metadata.json', meta)
            bridge.dump(root/'rpc-state.json', {'provider': 'opencode-ds', 'model': None})
            bridge.dump(root/'worker-result.json', {'models': [None], 'errors': [], 'tools': ['read'], 'final_text': 'synthetic', 'terminal_event': True})
            self.assertFalse(read_startup(root, meta)['rpc_ready_evidence'])
            self.assertIn('expected_model_evidence_missing', diagnose_run(root)['worker_evidence_blockers'])

    def test_diagnosis_uses_saved_identity_and_historical_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for expected in (None, LEGACY_MODEL, DEFAULT_MODEL):
                meta = {} if expected is None else {'worker_model': expected}
                selected_id = model_id(expected or LEGACY_MODEL)
                bridge.dump(root/'metadata.json', meta)
                bridge.dump(root/'rpc-state.json', {'provider': 'opencode-ds', 'model': selected_id})
                bridge.dump(root/'worker-result.json', {'models': [selected_id], 'errors': [],
                    'final_text': 'synthetic result', 'tools': ['read'], 'terminal_event': True})
                access = provider_access(root, meta, {}, {}, http_errors=0, final_text_present=True)
                self.assertEqual(access['local_selector'], 'expected_selector_observed')
                self.assertTrue(read_startup(root, meta)['rpc_ready_evidence'])
                self.assertNotIn('expected_model_evidence_missing', diagnose_run(root)['worker_evidence_blockers'])
                other = model_id(DEFAULT_MODEL if selected_id == model_id(LEGACY_MODEL) else LEGACY_MODEL)
                bridge.dump(root/'worker-result.json', {'models': [other], 'errors': [], 'tools': ['read'], 'final_text': 'synthetic', 'terminal_event': True})
                self.assertIn('expected_model_evidence_missing', diagnose_run(root)['worker_evidence_blockers'])


if __name__ == '__main__':
    unittest.main()
