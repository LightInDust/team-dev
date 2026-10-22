from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from snapshot_guard import SnapshotGuard, SnapshotViolation


class SnapshotGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)/'snapshot'
        self.root.mkdir()
        (self.root/'src').mkdir()
        (self.root/'src/code.py').write_text('before\n')
        (self.root/'src/context.py').write_text('read only\n')
        self.git('init', '-q')
        (self.root/'.git/info/exclude').write_text('__pycache__/\n*.pyc\n.pytest_cache/\n')
        self.git('add', '-A')
        self.git('-c', 'user.name=Tests', '-c', 'user.email=tests@localhost', 'commit', '-qm', 'baseline')
        self.commit = self.git('rev-parse', 'HEAD').decode().strip()
        self.guard = SnapshotGuard(self.root, self.commit, ['src/code.py', 'new.py'])

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.root, stderr=subprocess.PIPE)

    def test_allowed_edits_new_files_and_ignored_caches(self):
        (self.root/'src/code.py').write_text('after\n')
        (self.root/'new.py').write_text('new\n')
        (self.root/'src/__pycache__').mkdir()
        (self.root/'src/__pycache__/code.pyc').write_bytes(b'cache')
        self.assertEqual(self.guard.inspect(), ['new.py', 'src/code.py'])

    def test_ignored_cache_removal_cannot_be_used_as_cleanup_compliance_evidence(self):
        cache = self.root/'src/__pycache__'; cache.mkdir()
        artifact = cache/'code.pyc'; artifact.write_bytes(b'fixture')
        self.assertEqual(self.guard.inspect(), [])
        # Only owned fixture operations; do not execute a recursive shell cleanup.
        artifact.unlink(); cache.rmdir()
        self.assertEqual(self.guard.inspect(), [])

    def test_context_deletion_is_not_hidden_by_allowed_filter(self):
        (self.root/'src/context.py').unlink()
        with self.assertRaises(SnapshotViolation) as caught: self.guard.inspect()
        self.assertEqual(caught.exception.reason, 'snapshot_scope_violation')
        self.assertEqual(caught.exception.paths, ['src/context.py'])

    def test_extra_manual_verification_script_stops_run(self):
        (self.root/'.tmp_verify_manual_validator.py').write_text('print("test")\n')
        with self.assertRaisesRegex(SnapshotViolation, 'snapshot_scope_violation'):
            self.guard.inspect()

    def test_allowed_file_deletion_requires_explicit_permission(self):
        (self.root/'src/code.py').unlink()
        with self.assertRaisesRegex(SnapshotViolation, 'snapshot_undeclared_deletion'):
            self.guard.inspect()
        allowed = SnapshotGuard(self.root, self.commit, ['src/code.py'], ['src/code.py'])
        self.assertEqual(allowed.inspect(), ['src/code.py'])

    def test_deleted_source_subtree_is_detected(self):
        # Only the private fixture is removed; never execute the historical shell command.
        shutil.rmtree(self.root/'src')
        with self.assertRaises(SnapshotViolation) as caught: self.guard.inspect()
        self.assertEqual(caught.exception.paths, ['src/context.py'])

    def test_missing_root_or_git_stops_before_git_discovery(self):
        shutil.rmtree(self.root/'.git')
        with mock.patch.object(self.guard, '_git', side_effect=AssertionError('must not discover parent')):
            with self.assertRaisesRegex(SnapshotViolation, 'snapshot_unavailable'):
                self.guard.inspect()
            shutil.rmtree(self.root)
            with self.assertRaisesRegex(SnapshotViolation, 'snapshot_unavailable'):
                self.guard.inspect()

    def test_new_commit_is_rejected(self):
        (self.root/'src/code.py').write_text('after\n')
        self.git('add', '-A')
        self.git('-c', 'user.name=Tests', '-c', 'user.email=tests@localhost', 'commit', '-qm', 'unexpected')
        with self.assertRaisesRegex(SnapshotViolation, 'snapshot_head_changed'):
            self.guard.inspect()

    def test_scan_failure_is_explicit_without_subprocess_payload(self):
        with mock.patch('snapshot_guard.subprocess.run', side_effect=subprocess.TimeoutExpired('PRIVATE', 5)):
            with self.assertRaises(SnapshotViolation) as caught: self.guard.inspect()
        self.assertEqual(caught.exception.evidence(), {'reason':'snapshot_scan_failed', 'paths':[], 'path_count':0})
        self.assertNotIn('PRIVATE', str(caught.exception))


if __name__ == '__main__': unittest.main()
