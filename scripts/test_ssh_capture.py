import base64
import contextlib
import copy
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import ssh_capture as capture
import team_dev as bridge


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "repo with space and ' quote"
        self.repo.mkdir()
        (self.repo / 'src').mkdir()
        (self.repo / 'src/code.py').write_bytes(b'original\x00binary\n')
        (self.repo / '-option').write_text('executable\n')
        (self.repo / '-option').chmod(0o755)
        bridge.git(self.repo, 'init', '-q')
        bridge.git(self.repo, 'add', '-A')
        bridge.git(self.repo, '-c', 'user.name=Capture Tests', '-c', 'user.email=test@localhost',
                   'commit', '-qm', 'baseline')
        (self.repo / 'src/code.py').write_bytes(b'dirty\x00binary\n')
        self.target = {'transport': 'ssh', 'host': 'offline-fixture', 'repo': str(self.repo),
                       'ssh_keepalive': {'server_alive_interval': 5, 'server_alive_count_max': 2}}
        self.request = {'version': 1, 'nonce': 'a' * 32, 'repo': str(self.repo),
                        'paths': ['src/code.py', '-option', 'new/sub/file.py'], 'contents': True}

    @contextlib.contextmanager
    def in_repo(self):
        fd = os.open('.', os.O_RDONLY)
        try:
            os.chdir(self.repo)
            yield
        finally:
            os.fchdir(fd)
            os.close(fd)

    def response(self, request=None):
        with self.in_repo():
            return capture.collect(request or self.request)

    def transport(self, repo, argv, **kwargs):
        self.assertEqual(argv[:4], ['python3', '-I', '-B', '-c'])
        # Only the SSH boundary is replaced; run actual shipped remote code.
        return bridge.cmd([sys.executable, *argv[1:]], cwd=repo.repo, **kwargs)

    def test_real_protocol_preserves_dirty_binary_modes_and_absence(self):
        with mock.patch.object(bridge.Repo, 'run', autospec=True, side_effect=self.transport) as run:
            identity, manifest, files = bridge.Repo(self.target).capture(self.request['paths'])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(identity['repo'], str(self.repo))
        self.assertIn('src/code.py', identity['status'])
        self.assertEqual(files['src/code.py'], b'dirty\x00binary\n')
        self.assertEqual(manifest['-option']['mode'], '100755')
        self.assertEqual(manifest['new/sub/file.py'], {'exists': False})
        self.assertNotIn('new/sub/file.py', files)

    def test_prepare_twelve_context_files_uses_one_capture_connection(self):
        paths = []
        for i in range(12):
            name = 'src/context%d.txt' % i
            (self.repo / name).write_text('context%d\n' % i)
            paths.append(name)
        task = {'target': self.target, 'goal': 'offline fixture', 'allowed_paths': ['new.py'],
                'context_paths': paths, 'test_commands': [['/usr/bin/true']]}
        path = self.root / 'task.json'
        bridge.dump(path, task)
        with mock.patch.object(bridge, 'RUNS', self.root / 'runs'), mock.patch.object(
                bridge.Repo, 'run', autospec=True, side_effect=self.transport) as run:
            metadata = bridge.prepare(path)
        self.assertEqual(run.call_count, 1)
        snapshot = Path(metadata['run']) / 'snapshot'
        for name in paths:
            self.assertEqual((snapshot / name).read_bytes(), (self.repo / name).read_bytes())
        self.assertEqual(metadata['process'], 'not_started')
        self.assertEqual(metadata['transport_policy']['ssh_keepalive'], self.target['ssh_keepalive'])

    def test_identity_file_manifest_each_use_one_request(self):
        repo = bridge.Repo(self.target)
        with mock.patch.object(bridge.Repo, 'run', autospec=True, side_effect=self.transport) as run:
            self.assertEqual(repo.identity()['repo'], str(self.repo))
            self.assertEqual(repo.file('new/sub/file.py'), ({'exists': False}, None))
            self.assertEqual(len(repo.manifest(self.request['paths'])), 3)
        self.assertEqual(run.call_count, 3)
        self.assertEqual([json.loads(c.kwargs['data'])['contents'] for c in run.call_args_list],
                         [False, True, False])

    def test_executable_modes_match_existing_local_effective_user_policy(self):
        local = bridge.Repo({'transport': 'local', 'repo': str(self.repo)})
        for mode in (0o644, 0o755, 0o645, 0o654):
            with self.subTest(mode=mode):
                (self.repo / '-option').chmod(mode)
                self.assertEqual(self.response()['manifest']['-option'], local.file('-option')[0])

    def test_capture_preserves_fixed_ssh_options_and_is_bounded(self):
        def execute(argv, **kwargs):
            self.assertEqual(argv[:-1], ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                                        '-o', 'ClearAllForwardings=yes', '-o', 'ServerAliveInterval=5',
                                        '-o', 'ServerAliveCountMax=2', 'offline-fixture'])
            self.assertEqual(kwargs['timeout'], 30)
            self.assertEqual(kwargs['capture_limits'], (capture.MAX_RESPONSE, capture.MAX_STDERR))
            request = json.loads(kwargs['data'])
            return subprocess.CompletedProcess(argv, 0, capture.canonical(self.response(request)), b'')
        with mock.patch.object(bridge, 'cmd', side_effect=execute) as calls:
            bridge.Repo(self.target).capture(self.request['paths'])
        self.assertEqual(calls.call_count, 1)

    def test_nonzero_even_with_complete_body_is_never_absence(self):
        def failed(repo, argv, **kwargs):
            body = capture.canonical(self.response(json.loads(kwargs['data'])))
            return subprocess.CompletedProcess(argv, 255, body, b'timeout')
        with mock.patch.object(bridge.Repo, 'run', autospec=True, side_effect=failed):
            for operation in (lambda: bridge.Repo(self.target).file('missing'),
                              lambda: bridge.Repo(self.target).capture(['missing'])):
                with self.assertRaisesRegex(RuntimeError, 'transport failed'):
                    operation()

    def test_incomplete_or_failed_capture_does_not_create_prepared_run(self):
        path = self.root / 'task.json'
        bridge.dump(path, {'target': self.target, 'goal': 'offline fixture',
                          'allowed_paths': ['new.py'], 'test_commands': [['/usr/bin/true']]})
        for code, body in ((0, b'{"version":1,'), (255, b'')):
            with self.subTest(code=code), mock.patch.object(bridge, 'RUNS', self.root / 'runs'), mock.patch.object(
                    bridge.Repo, 'run', return_value=subprocess.CompletedProcess([], code, body, b'timeout')):
                with self.assertRaises((RuntimeError, ValueError)):
                    bridge.prepare(path)
                self.assertFalse((self.root / 'runs').exists())

    def test_response_rejects_partial_corrupt_extra_and_mismatched_data(self):
        good = self.response()
        raw = capture.canonical(good)
        malformed = [raw[:-1], raw + b'junk', b'{"version":1,"version":1}', b'null']
        for mutate in (
                lambda r: r.update(complete=False), lambda r: r.update(request_sha256='0'*64),
                lambda r: r['manifest'].pop('new/sub/file.py'),
                lambda r: r['manifest']['new/sub/file.py'].update(exists=0),
                lambda r: r['contents'].update({'extra': ''}),
                lambda r: r['contents'].update({'src/code.py': base64.b64encode(b'wrong').decode()}),
                lambda r: r['contents'].update({'src/code.py': '!!!'}),
                lambda r: r['sizes'].update({'src/code.py': True}),
                lambda r: r['identity'].update(head='bad')):
            value = copy.deepcopy(good)
            mutate(value)
            malformed.append(capture.canonical(value))
        for raw in malformed:
            with self.subTest(raw=raw[:60]), self.assertRaises(ValueError):
                capture.decode_response(raw, self.request)
        self.assertEqual(capture.decode_response(capture.canonical(good), self.request)['files']['src/code.py'],
                         b'dirty\x00binary\n')

    def test_symlink_leaf_parent_dangling_and_nested_repository_rejected(self):
        (self.repo / 'leaf').symlink_to('src/code.py')
        (self.repo / 'parent').symlink_to('src', target_is_directory=True)
        (self.repo / 'dangling').symlink_to('absent')
        (self.repo / 'nested').mkdir()
        (self.repo / 'nested/.git').write_text('gitdir: anywhere')
        for name in ('leaf', 'parent/code.py', 'dangling', 'dangling/child', 'nested/new.py'):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'Symlink|nested Git'):
                self.response(dict(self.request, paths=[name]))

    def test_non_directory_component_and_permission_failure_are_not_absence(self):
        with self.assertRaisesRegex(ValueError, 'regular file/directory'):
            self.response(dict(self.request, paths=['src/code.py/child']))
        original = capture.os.stat
        def denied(path, *args, **kwargs):
            if path == 'src':
                raise PermissionError(errno.EACCES, 'fixture denied')
            return original(path, *args, **kwargs)
        with mock.patch.object(capture.os, 'stat', side_effect=denied), self.assertRaises(PermissionError):
            self.response()

    def test_open_race_to_symlink_rejected_before_reading(self):
        original = capture.os.open
        swapped = False
        def race(path, flags, *args, **kwargs):
            nonlocal swapped
            if path == 'src' and not swapped:
                swapped = True
                (self.repo / 'src').rename(self.repo / 'old-src')
                (self.repo / 'src').symlink_to('old-src', target_is_directory=True)
            return original(path, flags, *args, **kwargs)
        with mock.patch.object(capture.os, 'open', side_effect=race), self.assertRaises(OSError):
            self.response()

    def test_identical_content_file_replacement_between_sweeps_is_rejected(self):
        original = capture.read_file
        calls = 0
        def replace(*args):
            nonlocal calls
            result = original(*args)
            calls += 1
            if calls == 1:
                source = self.repo / 'src/code.py'
                replacement = self.repo / 'replacement'
                replacement.write_bytes(source.read_bytes())
                os.replace(replacement, source)
            return result
        with mock.patch.object(capture, 'read_file', side_effect=replace), self.assertRaisesRegex(RuntimeError, 'Source changed'):
            self.response()

    def test_file_growth_during_descriptor_read_is_rejected(self):
        original = capture.os.read
        done = False
        # Call read_file directly so this hook does not affect Git transport pipes.
        with self.in_repo():
            fd = os.open('.', os.O_RDONLY | os.O_DIRECTORY)
            try:
                def grow(*args):
                    nonlocal done
                    data = original(*args)
                    if not done:
                        done = True
                        with (self.repo / 'src/code.py').open('ab') as f:
                            f.write(b'grew')
                    return data
                with mock.patch.object(capture.os, 'read', side_effect=grow), self.assertRaisesRegex(RuntimeError, 'Source changed'):
                    capture.read_file(fd, 'src/code.py')
            finally:
                os.close(fd)

    def test_root_path_replacement_is_rejected(self):
        real = capture.identity
        calls = 0
        def replace(deadline):
            nonlocal calls
            result = real(deadline)
            calls += 1
            if calls == 2:
                self.repo.rename(self.root / 'old-repo')
                self.repo.mkdir()
            return result
        with mock.patch.object(capture, 'identity', side_effect=replace), self.assertRaisesRegex(RuntimeError, 'root path changed'):
            self.response()

    def test_parent_replacement_and_newly_present_path_are_rejected(self):
        for kind in ('parent', 'absent'):
            with self.subTest(kind=kind):
                name = 'src/code.py' if kind == 'parent' else 'new.py'
                original = capture.read_file
                done = False
                def mutate(*args):
                    nonlocal done
                    result = original(*args)
                    if not done:
                        done = True
                        if kind == 'parent':
                            (self.repo / 'src').rename(self.repo / 'old-src')
                            (self.repo / 'src').mkdir()
                            (self.repo / 'src/code.py').write_bytes((self.repo / 'old-src/code.py').read_bytes())
                        else:
                            (self.repo / 'new.py').write_text('appeared')
                    return result
                with mock.patch.object(capture, 'read_file', side_effect=mutate), self.assertRaisesRegex(RuntimeError, 'Source changed'):
                    self.response(dict(self.request, paths=[name]))

    def test_identity_change_without_selected_byte_change_is_rejected(self):
        real = capture.identity
        count = 0
        def changed(deadline):
            nonlocal count
            result = real(deadline)
            count += 1
            if count == 2:
                result['head'] = 'b' * 40
            return result
        with mock.patch.object(capture, 'identity', side_effect=changed), self.assertRaisesRegex(RuntimeError, 'Source changed'):
            self.response()

    def test_file_total_and_request_limits_fail_closed(self):
        with mock.patch.object(capture, 'MAX_FILE', 4), self.assertRaisesRegex(ValueError, '4 MiB'):
            self.response()
        with mock.patch.object(capture, 'MAX_TOTAL', 5), self.assertRaisesRegex(ValueError, '32 MiB'):
            self.response()
        for paths in (['src/code.py'] * 2, ['../escape'], ['.git/config'], ['.'], ['a//b'],
                      ['/absolute'], ['a\nb'], ['x'*4097], ['file%d' % i for i in range(101)]):
            with self.subTest(paths=paths[:2]), self.assertRaises(ValueError):
                capture.validate_request(dict(self.request, paths=paths))

    def test_local_predicates_reject_all_error_exit_codes(self):
        repo = bridge.Repo({'transport': 'local', 'repo': str(self.repo)})
        actual = repo.run
        for marker in (['test', '-L', './src'], ['test', '-e', './src/.git'],
                       ['test', '-e', './src/code.py'], ['test', '-x', './src/code.py']):
            for code in (2, 255):
                def failed(argv, **kwargs):
                    if argv == marker:
                        return subprocess.CompletedProcess(argv, code, b'', b'fixture transport failure')
                    return actual(argv, **kwargs)
                with self.subTest(marker=marker, code=code), mock.patch.object(repo, 'run', side_effect=failed):
                    with self.assertRaisesRegex(RuntimeError, 'Cannot inspect path'):
                        repo.file('src/code.py')


class BoundedTransportTests(unittest.TestCase):
    def test_output_limits_for_both_pipes(self):
        for stream in ('stdout', 'stderr'):
            with self.subTest(stream=stream), self.assertRaisesRegex(RuntimeError, 'output limit'):
                capture.bounded_run([sys.executable, '-B', '-c',
                                     'import sys; sys.%s.write("x"*4096)' % stream], capture_limits=(128, 128))

    def test_nonzero_and_deadline_are_errors(self):
        with self.assertRaisesRegex(RuntimeError, r'failed \(255\)'):
            capture.bounded_run([sys.executable, '-B', '-c', 'print("complete body"); raise SystemExit(255)'])
        with self.assertRaises(subprocess.TimeoutExpired):
            capture.bounded_run([sys.executable, '-B', '-c', 'import time; time.sleep(10)'], timeout=.2)


if __name__ == '__main__':
    unittest.main()
