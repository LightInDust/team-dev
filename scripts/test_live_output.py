import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

from live_output import inspect, OBSERVATION, MAX_CHARS


class LiveOutputTests(unittest.TestCase):
    def test_dependency_probes_and_quoted_absolute_interpreters_are_recognized(self):
        for command in (
            'python3 -B -c "import missing_dependency" 2>&1 | tail -1',
            'env PYTHONDONTWRITEBYTECODE=1 "/path with spaces/python3" -B -c "import PIL; print(PIL.__version__)" | head -1',
            'python3 -B first.py; /usr/bin/python3 -B -c "import plyfile" 2>&1 | tail -1',
            'false && python3 -B diagnostic.py | grep error',
            'python3 -B -m unittest | tee log',
            'command python3 -B -c \'print("quoted | example")\' | cat | /usr/bin/tail -1',
        ):
            with self.subTest(command=command):
                self.assertEqual(inspect({'command':command})['observations'],[OBSERVATION])

    def test_passive_log_reads_and_inline_strings_are_not_live_pipelines(self):
        for command in ('tail -20 saved.stderr', 'cat saved.stderr | tail -1', 'grep error saved.log | head -2',
                        'head -30 saved.log; python3 -B -c "import PIL"',
                        'python3 -B -c "import PIL"; tail -1 saved.stderr',
                        'echo \'python3 -c "import PIL" | tail -1\'',
                        'python3 -B -c \'print("| tail -1")\'',
                        'python3 -B -c \'print(1)\' "|" tail -1',
                        'python3 -B -c \'print(1)\' || tail -1 saved.log'):
            with self.subTest(command=command):
                self.assertEqual(inspect({'command':command})['observations'],[])

    def test_unknown_expansion_heredoc_control_and_redirection_are_uninspected(self):
        for command in ('"$PY" -B -c "import PIL" | tail -1',
                        'cd "$(pwd)" && python3 -B -c "import PIL" | tail -1',
                        'python3 - <<\'PY\' | tail -1\nprint(1)\nPY',
                        'if true; then python3 -B probe.py | tail -1; fi',
                        'python3 -B probe.py >log | tail -1', 'python3 -B probe.py | tail -1 # comment',
                        'python3 -B probe.py |', 'echo "unterminated', 'py"thon"3 -B probe.py | tail -1'):
            with self.subTest(command=command):
                result=inspect({'command':command})
                self.assertEqual(result['status'],'unsupported_syntax')
                self.assertEqual(result['observations'],[])

    def test_limits_payload_privacy_and_no_execution(self):
        for args in ({},{'command':None},{'command':'x'*(MAX_CHARS+1)}):
            self.assertEqual(inspect(args)['status'],'unavailable')
        result=inspect({'command':'/PRIVATE/python3 -B -c "import PRIVATE_MODULE" | tail -1'})
        self.assertNotIn('PRIVATE',json.dumps(result))
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/'never-executed'
            inspect({'command':'python3 -c "import pathlib; pathlib.Path('+repr(str(target))+').touch()" | tail -1'})
            self.assertFalse(target.exists())

    def test_real_failure_masked_by_tail_but_later_saved_log_read_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            argv=[sys.executable,'-B','-c','import missing_team_dev_dependency_fixture_7491']
            direct=subprocess.run(argv,cwd=root,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'),capture_output=True,timeout=10)
            self.assertNotEqual(direct.returncode,0)
            self.assertIn(b'ModuleNotFoundError',direct.stderr)
            (root/'stderr.log').write_bytes(direct.stderr)
            command=shlex.join(argv)+' 2>&1 | tail -1'
            masked=subprocess.run(['/bin/sh','-c',command],cwd=root,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'),capture_output=True,timeout=10)
            self.assertEqual(masked.returncode,0)
            self.assertIn(b'ModuleNotFoundError',masked.stdout)
            self.assertEqual(inspect({'command':command})['observations'],[OBSERVATION])
            saved='cat stderr.log | tail -1'
            read=subprocess.run(['/bin/sh','-c',saved],cwd=root,capture_output=True,timeout=10)
            self.assertEqual(read.returncode,0)
            self.assertEqual(read.stdout,masked.stdout)
            self.assertEqual(inspect({'command':saved})['observations'],[])
