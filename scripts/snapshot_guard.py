"""Cooperative snapshot checks; detection after filesystem changes, not a sandbox."""
import os
from pathlib import Path
import subprocess


class SnapshotViolation(ValueError):
    def __init__(self, reason, paths=()):
        self.reason = reason
        self.paths = sorted(set(paths))
        super().__init__(reason)

    def evidence(self):
        return {'reason': self.reason, 'paths': self.paths[:50], 'path_count': len(self.paths)}


class SnapshotGuard:
    def __init__(self, snapshot, commit, allowed_paths, allowed_deletions=()):
        self.snapshot = Path(snapshot).absolute()
        self.commit = commit
        self.allowed = set(allowed_paths)
        self.deletions = set(allowed_deletions)

    def _git(self, *args):
        # Missing .git must never fall through to discovery of a parent repository.
        try:
            result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null',
                '--git-dir', str(self.snapshot / '.git'), '--work-tree', str(self.snapshot),
                *args], cwd=self.snapshot, capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            raise SnapshotViolation('snapshot_scan_failed') from None
        if result.returncode:
            raise SnapshotViolation('snapshot_scan_failed')
        return result.stdout

    def inspect(self):
        root = self.snapshot
        if root.is_symlink() or not root.is_dir() or (root/'.git').is_symlink() or not (root/'.git').is_dir():
            raise SnapshotViolation('snapshot_unavailable')
        try:
            if self._git('rev-parse', 'HEAD').decode().strip() != self.commit:
                raise SnapshotViolation('snapshot_head_changed')
            changes = self._git('diff', '--no-ext-diff', '--no-renames', '--name-only', '-z', self.commit, '--')
            changes += self._git('ls-files', '--others', '--exclude-standard', '-z')
            names = {p.decode() for p in changes.split(b'\0') if p}
        except UnicodeDecodeError:
            raise SnapshotViolation('snapshot_scan_failed') from None
        outside = names - self.allowed
        if outside:
            raise SnapshotViolation('snapshot_scope_violation', outside)
        undeclared = {name for name in names if not os.path.lexists(root/name)} - self.deletions
        if undeclared:
            raise SnapshotViolation('snapshot_undeclared_deletion', undeclared)
        return sorted(names)
