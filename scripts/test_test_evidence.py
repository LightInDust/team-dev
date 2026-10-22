import unittest
from test_evidence import module_cache_writes, unittest_evidence


class EvidenceTests(unittest.TestCase):
    def test_real_unittest_summaries(self):
        for executable in ('python', 'python3', '/usr/bin/python3.12'):
            argv = [executable, '-m', 'unittest', 'discover']
            for count, word in ((1, 'test'), (12, 'tests')):
                result = unittest_evidence(argv, '', '\nRan %d %s in 0.002s\n\nOK\n' % (count, word))
                self.assertTrue(result['passed'])
                self.assertEqual(result['count'], count)

    def test_zero_and_missing_summary_fail(self):
        argv = ['env', 'PYTHONPATH=src', 'python3', '-m', 'unittest', 'discover']
        self.assertEqual(unittest_evidence(argv, '', 'Ran 0 tests in 0.1s\nOK')['reason'], 'no_tests_collected')
        for output in ('21 tests passed', 'prefix Ran 21 tests in 0.1s', ''):
            self.assertFalse(unittest_evidence(argv, output, '')['passed'])

    def test_interpreter_options_do_not_bypass_collection_check(self):
        for options in (['-I'], ['-B', '-u'], ['-X', 'dev'], ['-W', 'ignore'],
                        ['-Xdev', '-Wignore'], ['-OO', '-s'], ['-IBu'],
                        ['--check-hash-based-pycs', 'always'], ['--check-hash-based-pycs=always']):
            with self.subTest(options=options):
                argv = ['python3'] + options + ['-m', 'unittest', 'discover']
                result = unittest_evidence(argv, '', 'Ran 0 tests in 0.1s\nOK')
                self.assertEqual(result['framework'], 'unittest')
                self.assertEqual(result['reason'], 'no_tests_collected')
                self.assertFalse(result['passed'])
        self.assertFalse(unittest_evidence(['python3','-B','-munittest'], '', '')['passed'])
        for argv in (['python3','-c','print("-m unittest")'], ['python3','script.py','-m','unittest'],
                     ['python3','--','-m','unittest']):
            self.assertIsNone(unittest_evidence(argv, '', '')['framework'])

    def test_other_commands_and_last_summary(self):
        self.assertTrue(unittest_evidence(['node', '--check', 'a.js'], '', '')['passed'])
        self.assertTrue(unittest_evidence(['/usr/bin/true'], '', '')['passed'])
        self.assertEqual(unittest_evidence(['python3','-m','unittest'], '',
                         'Ran 4 tests in 1s\nRan 0 tests in 0s\n')['count'], 0)

    def test_naked_module_injection_including_stub_builder(self):
        snippets = [
            'import sys\nsys.modules["real"] = fake',
            'import sys as s\ns.modules["real"] = fake',
            'from sys import modules as m\nm["real"] = fake',
            'import sys\ncache = sys.modules\ncache["real"] = fake',
            'import sys\ndef install(name):\n    sys.modules[name] = fake\ninstall("real")',
            'import sys\nif True:\n    sys.modules.update({})',
            'import sys\nclass A:\n    sys.modules.setdefault("real", fake)',
            'import sys\ndel sys.modules["real"]',
            'import sys\nsys.modules["real"] += fake',
            'import sys\nsys.modules["real"]: object = fake',
            'from sys import modules\nmodules |= {}',
        ]
        for source in snippets:
            with self.subTest(source=source): self.assertTrue(module_cache_writes(source))

    def test_module_level_patching_rejected(self):
        for source in (
            'import sys\nfrom unittest.mock import patch\nwith patch.dict(sys.modules, {}):\n    pass',
            'from unittest import mock\nmock.patch("sys.modules", {})',
            'import sys\nimport unittest.mock as m\nclass A:\n    x = m.patch.dict(sys.modules, {})',
            'import unittest\nunittest.mock.patch.dict("sys.modules", {})',
            'import sys\nfrom unittest.mock import patch\ndef test():\n    patch.dict(sys.modules, {}).start()',
        ):
            with self.subTest(source=source): self.assertTrue(module_cache_writes(source))

    def test_whole_cache_replacement_deletion_and_nested_targets(self):
        for source in (
            'import sys\nsys.modules = {}',
            'import sys as s\ndef install():\n    s.modules = {}',
            'import sys\nsys.modules: dict = {}',
            'import sys as s\ndel s.modules',
            'import sys\nsys.modules, other = {}, None',
            'import sys\n[other, sys.modules["x"]] = values',
            'import sys\ndel (sys.modules, other)',
        ):
            with self.subTest(source=source): self.assertTrue(module_cache_writes(source))
        self.assertEqual(module_cache_writes('from sys import modules\nmodules = {}\ndel modules'), [])

    def test_reads_and_scoped_patches_allowed(self):
        source = '''import sys
from unittest.mock import patch
x = sys.modules.get("real")
y = sys.modules["real"]
example = 'sys.modules["real"] = fake'
@patch.dict(sys.modules, {})
def test_one():
    with patch.dict(sys.modules, {}):
        pass
'''
        self.assertEqual(module_cache_writes(source), [])

    def test_syntax_error_propagates(self):
        with self.assertRaises(SyntaxError): module_cache_writes('def invalid(')


if __name__ == '__main__': unittest.main()
