import json
from pathlib import Path
import tempfile
import unittest

from worker_bash import resolve, inspect
from worker_test_catalog import catalog, correction_hint, validate_catalog


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.task = {'worker_bash_mode': 'declared_tests_only',
                     'test_commands': [['python3', '-B', '-m', 'unittest', 'test_a'],
                                       ['python3', '-B', '-m', 'unittest', 'test_b']]}
        self.rules = resolve(self.task, self.root)
        self.entries = catalog(self.task, self.root)

    def test_ambiguous_reference_is_omitted_and_near_matches_remain_blocked(self):
        command = self.entries[0]['arguments']['command'].replace('test_a', 'test_c')
        hint = correction_hint({'command': command}, self.rules, self.entries)
        self.assertNotIn('reference=', hint)
        self.assertIn('command_match=no', hint)
        self.assertEqual(inspect({'command': command}, self.rules)['status'], 'undeclared_command')
        self.assertEqual(validate_catalog(self.entries, self.rules), self.entries)

    def test_argument_hint_does_not_disclose_raw_private_names_values_or_commands(self):
        args = dict(self.entries[0]['arguments'], timeout=True, cwd='PRIVATE_PATH', PRIVATE_KEY='PRIVATE_VALUE')
        hint = correction_hint(args, self.rules, self.entries)
        self.assertIn('command_match=yes', hint)
        self.assertIn('unsupported_arguments,invalid_timeout,cwd_not_snapshot', hint)
        self.assertIn('reference=T1.direct', hint)
        self.assertIn('first_difference_codepoint=none', hint)
        self.assertNotIn('PRIVATE', hint)
        self.assertNotIn('unittest', hint)
        self.assertEqual(inspect(args, self.rules)['status'], 'invalid_arguments')

    def test_unavailable_and_overlong_requests_do_not_invent_matching_evidence(self):
        for args in (None, {}, {'command': True}, {'command': 'PRIVATE'*6000}):
            with self.subTest(args_type=type(args).__name__):
                hint = correction_hint(args, self.rules, self.entries)
                self.assertIn('command_match=unknown', hint)
                self.assertNotIn('reference=', hint)
                self.assertNotIn('PRIVATE', hint)

    def test_hint_catalog_work_is_bounded_and_cannot_change_rules(self):
        before = json.dumps(self.rules, sort_keys=True)
        # Search exhausts its budget before the matching reference; match truth is
        # still determined independently by the unchanged command hash policy.
        entries = [{'id': f'T{i}.direct', 'arguments': {'command': 'x'*32768}} for i in range(5)]
        entries += self.entries
        hint = correction_hint(self.entries[0]['arguments'], self.rules, entries)
        self.assertIn('command_match=yes', hint)
        self.assertNotIn('reference=', hint)
        self.assertEqual(json.dumps(self.rules, sort_keys=True), before)


if __name__ == '__main__':
    unittest.main()
