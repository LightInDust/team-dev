"""Execute the shared-fixture reference with synthetic boundaries, never models."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


namespace = {}
reference = Path(__file__).resolve().parent.parent/'references/model-isolation-tests.md'
code = reference.read_text().split('```python\n', 1)[1].split('\n```', 1)[0]
exec(compile(code, str(reference), 'exec'), namespace)
ForbiddenModelCalls = namespace['ForbiddenModelCalls']
import_policy_namespace = {}
import_policy_code = reference.read_text().split('```python\n')[2].split('\n```', 1)[0]
exec(compile(import_policy_code, str(reference), 'exec'), import_policy_namespace)
optional_test_import = import_policy_namespace['optional_test_import']


class ImportPolicyReferenceTests(unittest.TestCase):
    def missing_dependency(self):
        raise ModuleNotFoundError('synthetic missing optional backend', name='test_backend')

    def test_optional_missing_dependency_is_explicitly_unverified_not_executed(self):
        body = Mock(name='functional_test_body')
        class Case(unittest.TestCase):
            def setUp(case):
                optional_test_import(self.missing_dependency, required=False,
                                     optional_names={'test_backend'})
            def runTest(case):
                body()
        result = unittest.TestResult()
        Case().run(result)
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(len(result.skipped), 1)
        self.assertIn('NOT VERIFIED', result.skipped[0][1])
        body.assert_not_called()

    def test_required_import_failure_is_error_not_skip(self):
        def missing():
            optional_test_import(self.missing_dependency, required=True,
                                 optional_names={'test_backend'})
        result = unittest.TestResult()
        unittest.FunctionTestCase(missing).run(result)
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.skipped, [])
        self.assertIn('ModuleNotFoundError', result.errors[0][1])

    def test_optional_mode_does_not_hide_project_import_errors_or_guard_failures(self):
        errors = [ModuleNotFoundError('project bug', name='project.internal'),
                  ImportError('broken installed backend'), RuntimeError('initialization bug'),
                  AssertionError('forbidden model attempt')]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                def broken():
                    raise error
                with self.assertRaises(type(error)) as caught:
                    optional_test_import(broken, required=False, optional_names={'test_backend'})
                self.assertIs(caught.exception, error)

    def test_available_dependency_value_is_preserved_in_optional_and_required_modes(self):
        value = object()
        for required in (False, True):
            loader = Mock(return_value=value)
            self.assertIs(optional_test_import(loader, required=required,
                                              optional_names={'test_backend'}), value)
            loader.assert_called_once_with()


class GuardReferenceTests(unittest.TestCase):
    def run_case(self, body, boundaries=None):
        real = Mock(name='real_model_boundary')
        backend = SimpleNamespace(load=real)
        class Case(unittest.TestCase):
            def setUp(case):
                ForbiddenModelCalls(case, boundaries(backend) if boundaries else [(backend, 'load', 'loader')])

            def runTest(case):
                body(case, backend)
        result = unittest.TestResult()
        Case().run(result)
        self.assertIs(backend.load, real)  # Patches restore after success/failure.
        real.assert_not_called()
        return result

    def test_omitted_mock_and_swallowed_assertion_still_fail_cleanup(self):
        def body(case, backend):
            with case.assertRaises(AssertionError):
                backend.load('private-input')
            case.assertEqual([], [])  # An unrelated empty-result assertion is green.
        result = self.run_case(body)
        self.assertEqual(len(result.failures), 1)
        self.assertIn('Forbidden model attempts: loader', result.failures[0][1])
        self.assertNotIn('private-input', result.failures[0][1])

    def test_scoped_deterministic_mock_is_allowed_inside_guard(self):
        def body(case, backend):
            with patch.object(backend, 'load', return_value='fixture-model') as stub:
                case.assertEqual(backend.load(), 'fixture-model')
                stub.assert_called_once_with()
        self.assertTrue(self.run_case(body).wasSuccessful())

    def test_constructor_attempt_is_latched_before_test_body_and_late_guard_is_detectable(self):
        def exercise(guard_first):
            real = Mock(name='real_constructor_boundary')
            backend = SimpleNamespace(load=real)
            body = Mock(name='test_body')
            class EagerFixture:
                def __init__(self):
                    try:
                        backend.load()
                    except AssertionError:
                        pass  # A constructor may swallow the immediate rejection.
            class Case(unittest.TestCase):
                def setUp(case):
                    if guard_first:
                        ForbiddenModelCalls(case, [(backend, 'load', 'constructor')])
                    case.fixture = EagerFixture()
                    if not guard_first:
                        ForbiddenModelCalls(case, [(backend, 'load', 'constructor')])
                def runTest(case):
                    body()
            result = unittest.TestResult()
            Case().run(result)
            self.assertIs(backend.load, real)
            return result, real, body
        protected, real, body = exercise(True)
        self.assertEqual(len(protected.failures), 1)
        self.assertIn('Forbidden model attempts: constructor', protected.failures[0][1])
        real.assert_not_called()
        body.assert_called_once_with()
        # Negative control: the same fixture with late protection misses setup.
        late, escaped, _ = exercise(False)
        self.assertTrue(late.wasSuccessful())
        escaped.assert_called_once_with()

    def test_valid_constructor_mock_and_setup_error_restore_shared_guards(self):
        real = Mock(name='real_setup_boundary')
        backend = SimpleNamespace(load=real)
        for fail_setup in (False, True):
            observed = []
            class Case(unittest.TestCase):
                def setUp(case):
                    ForbiddenModelCalls(case, [(backend, 'load', 'constructor')])
                    with patch.object(backend, 'load', return_value='fixture'):
                        observed.append(backend.load())
                    if fail_setup:
                        raise RuntimeError('synthetic setup failure')
                def runTest(case):
                    case.assertEqual(observed, ['fixture'])
            result = unittest.TestResult()
            Case().run(result)
            self.assertEqual(len(result.errors), int(fail_setup))
            self.assertEqual(result.failures, [])
            self.assertEqual(observed, ['fixture'])
            self.assertIs(backend.load, real)
            real.assert_not_called()

    def test_partial_setup_failure_restores_prior_patches(self):
        result = self.run_case(lambda case, backend: None,
                               lambda backend: [(backend, 'load', 'loader'), (backend, 'absent', 'missing')])
        self.assertEqual(len(result.errors), 1)
        self.assertIn('does not have the attribute', result.errors[0][1])

    def test_empty_boundary_inventory_is_not_a_working_guard(self):
        result = self.run_case(lambda case, backend: None, lambda backend: [])
        self.assertEqual(len(result.errors), 1)
        self.assertIn('Supply inspected model boundaries', result.errors[0][1])

    def test_fault_oracle_rejects_empty_bypass_even_if_public_error_matches(self):
        def run_fault(objects):
            writer = Mock(side_effect=RuntimeError('export boom'))
            def public_path():
                if not objects:
                    return {'error': 'export boom'}  # Misleading empty-path result.
                try:
                    writer(objects[0])
                except RuntimeError as error:
                    return {'error': str(error)}
            result = public_path()
            self.assertEqual(result, {'error': 'export boom'})
            writer.assert_called_once_with('object')
        run_fault(['object'])
        with self.assertRaisesRegex(AssertionError, 'Called 0 times'):
            run_fault([])


if __name__ == '__main__':
    unittest.main()
