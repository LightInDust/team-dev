# Model isolation and reachable fault injections

Use this when a task explicitly forbids real model construction, weight loading/download or inference during its tests. An offline entrypoint name, synthetic frames or missing local dependencies do not establish that restriction. Keep model-free tests separate from explicitly authorized real-model tests.

## Shared guard, installed before execution

Astra identifies the actual model construction/loading/inference boundaries and how the project looks them up, including imported aliases and setup-time calls. Select the existing shared fixture/base test class into the task snapshot and use it for every affected case, including error/export tests. Install a default-deny guard before any import, setup or public entrypoint that could load a model. Per-test deterministic model mocks are scoped inside that guard. Forgetting a mock must fail before the real boundary executes, rather than fall through to an installed backend.

Fixture construction is execution too. In the shared setup, install the guard and task-required device settings before calling a real constructor, backprojection helper, superclass setup or fixture factory that may select a device or initialize a backend. For model-free CPU fixtures, pass the actual supported CPU/device option explicitly; synthetic input alone does not prevent a CUDA default. Trace the real setup order instead of moving protection only around the eventual test-body call. CPU selection does not replace the default-deny guard, and no CPU-only restriction is implied for separately authorized GPU tests.

Patch the real lookup locations with scoped `patch.object`/monkeypatch. A string patch target can import the project module before installing the patch; do not use it to discover a potentially eager-loading module. Identify an import-safe backend seam first, or report missing isolation context instead of experimenting with real loads. Previously bound aliases, subprocesses and unlisted backends need their own mapped boundaries; an in-process guard does not cover them automatically. Preserve required constructor/setup state and real argument types, such as paths, in positive fixtures. Prefer the supported constructor/shared fixture factory with external boundaries patched over `__new__` plus a partial hand-built attribute set; if bypassing initialization is necessary, enumerate and assert the actual required state before exercising the public path. Do not fabricate packages or modify `sys.modules` to make imports pass.

Keep an attempted-call latch as well as the immediate exception. Production code can catch `Exception`/`AssertionError` and return an empty result; the shared fixture's teardown must still fail. Register this check as fixture cleanup outside the test's expected-exception context, and retain the guard through all relevant execution. Finish owned asynchronous work before fixture cleanup. Do not clear the latch or count an expected guard exception as the intended application fault.

The following reusable `unittest` pattern belongs in the project's shared test support; adapt the explicitly inspected boundary objects and labels. It imports no model library and makes no claim to enumerate every model backend. Its cleanup is registered before patch cleanup, so partial setup failure still restores installed patches and checks attempts.

```python
from unittest.mock import patch


class ForbiddenModelCalls:
    def __init__(self, case, boundaries):
        boundaries = list(boundaries)
        if not boundaries:
            raise ValueError("Supply inspected model boundaries")
        self.attempts = []
        case.addCleanup(self.assert_clear)
        for owner, attribute, label in boundaries:
            blocker = patch.object(owner, attribute, side_effect=self._reject(label))
            blocker.start()
            case.addCleanup(blocker.stop)

    def _reject(self, label):
        def denied(*args, **kwargs):
            self.attempts.append(label)
            raise AssertionError("Forbidden real model boundary: " + label)
        return denied

    def assert_clear(self):
        if self.attempts:
            raise AssertionError("Forbidden model attempts: " + ", ".join(self.attempts))
```

Create this guard in the shared `setUp`/fixture with `(already_inspected_owner, attribute_name, safe_label)` entries; then use the project's existing model-result patches for deterministic outputs. Do not include input payloads, credentials or weight contents in labels. In pytest, use the same latch and scoped patches in an appropriately scoped automatic fixture, with the final check outside test-body exception handling. This is a reusable fixture design, not an installed host-wide intercept or permission to run a separate Bash probe.

Verify the guard itself with a bounded synthetic case: omit one ordinary model mock, catch the immediate guard exception as production might, and verify teardown still fails and the underlying sentinel was never invoked. Keep that guard self-check distinct from business acceptance. Presence of fixture code alone does not prove every affected test uses it.

Include the constructor/setup path in that check: invoke a synthetic eager constructor after shared protection, swallow its immediate exception, and require cleanup to retain the forbidden-attempt failure while the underlying sentinel remains untouched. Also check the valid scoped-mock setup path and restoration after setup fails. A test-body-only guard check does not cover initialization before the test body.

## Prove the injected boundary was reached

For an export/write failure, derive the smallest valid nonempty fixture from the real producer's preconditions. Supply required initialization/configuration, path types and upstream model results so at least one object reaches the intended writer. Keep a separate legitimate empty-input case; do not silently use it for a writer-failure test.

Inject a distinct application exception at the actual writer and assert both its public error outcome and the expected invocation/count/arguments. For a fixture that should write exactly once, `write_point_cloud.assert_called_once()` (or `assert_called_once_with(...)`) is necessary evidence that the boundary was reached; use the actual contract's count for other cases. A broad expected exception is insufficient: an earlier missing attribute, import error or model guard failure must not satisfy the export-failure oracle. For APIs that catch the writer error, assert the specific failed result/diagnostic and contract-required absence of success output instead of requiring a propagated exception.

Add one bounded sensitivity check where needed: force the upstream output empty or bypass the writer, and verify the same writer-fault regression fails for missing invocation rather than passing with no work. Test the real public path and retain independent expected values. The call-count assertion proves reachability, not correct error handling or complete output semantics; both still need their own assertions. Use declared tests and owned fixtures, without rerunning historical experiments.

For corruption/swap faults, first prove a valid baseline and that the proposed mutation changes the exact property the validator reads. Swapping filenames or two byte-identical synthetic images does not create a content-hash mismatch. Create distinguishable valid input bytes, bind expected hashes to that baseline before mutation, assert the relevant bytes/hashes differ, and only then corrupt/swap without refreshing the expected values. Preserve unrelated valid preconditions. Through the public path, assert the intended rejection reason plus evidence that the target validator ran (an existing diagnostic or scoped spy on its real implementation), and any contract-required absence of downstream mapping/publication. A precondition assertion alone is not reachability; a signature/setup/import error is not the intended rejection. This example does not mandate hashing for contracts that use other identities.

## Target evidence and isolation limits

Report collected, executed, passed, failed/errors and skipped counts with reasons and environment. A local `OK (skipped=10)` for ten collected dependency-heavy cases means zero executed cases, not ten passes or target acceptance. Required skipped cases remain NOT VERIFIED until the declared target tests actually execute. A target run with missing constructor state or test-fixture errors still fails; fix the real setup path and rerun that candidate's target checks.

When the task defines a REQUIRED flag for target tests, every new test module, module-level import handler and fallback/base fixture must obey that same flag before dependency imports can fail. Read it using import-safe standard-library code or shared import-safe test support, not from the heavy module that may fail to import. A sibling module's correct policy does not cover a new module. In required mode, missing dependencies/import failures must produce a failing check, never `SkipTest`, `skipIf`, `importorskip` or a blanket skip decorator. In optional local mode, skip only known absent optional dependencies authorized by the task; internal project import bugs, assertion failures, broken installed packages and other runtime errors must not be converted to skips. Local skipping is NOT VERIFIED, never source or target functional acceptance. Keep the actual flag/value and target command from the task; do not invent a replacement flag or loosen model-isolation rules.

The following small shared policy is reusable with import-safe loader callbacks. Each affected module passes its already-resolved required flag and the exact optional dependency names approved for that task. It only permits `ModuleNotFoundError` for those names; other import failures propagate. Test the optional and required branches with a synthetic failing callback within a declared test, without uninstalling packages, triggering models or writing fake modules to `sys.modules`.

```python
import unittest


def optional_test_import(importer, *, required, optional_names):
    try:
        return importer()
    except ModuleNotFoundError as error:
        if required or error.name not in optional_names:
            raise
        raise unittest.SkipTest(
            "NOT VERIFIED: optional test dependency unavailable: " + error.name
        ) from error
```

Sol checks each new module's import/fixture path and required-mode behavior in the ordinary review; presence of this helper does not prove every module uses it. Retain the complete target log and inspect skips across the entire declared discovery pattern. REQUIRED enforces dependency availability for required cases, not permission for real model calls.

Distinguish no real model calls from complete isolation: static prototype/class metadata, environment configuration and installed packages may still be read. Document those dependencies or replace them with authorized deterministic fixtures when required. Do not claim GPU/model behavior from model-free tests. Preserve original failures and bind any takeover's guard coverage, reached fault boundary, target results and independent review to its separate candidate.
