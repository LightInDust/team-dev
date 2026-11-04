# Native Sol team

This is an explicit coordinator workflow for a new Git coding task. It does not require local OMP or OMP provider/API credentials. It is not an OMP runner mode or a migration of an existing prepared run. Preserve earlier OMP attempts, accepted evidence, frozen policies, provider/proxy/timeout settings and failed history. An unchanged accepted artifact need not be repeated to change a route label. A changed candidate reopens its affected checks and review.

## Select and assign

An explicit “native Sol team” request selects this composition and retains it across slices and retries. For an unspecified new chat task, offer the configured OMP default first, its one-time network/billing fallback when configured, the other configured OMP provider, and this native route; wait for the user's submitted choice. “Use the default” keeps the OMP default. An OpenCode-only request disables its provider fallback. A prepared OMP run continues with its frozen selector and old reviewer gate.

Name exactly one lifecycle owner by accepted agent ID, usually `/root`. Only that owner dispatches, steers, interrupts, replaces agents and serializes recovery. The owner keeps original requirements, scope, target and acceptance ledger. Before dispatch, persist selected composition, role/effort assignments, allowed paths, exact target test plan and task checkpoints. Checkpoints guide coordinator monitoring; they do not provide OMP timeouts or automatic renewal. A planner or reviewer reports findings to the owner; it does not control siblings. For every overridden model/effort, use the host's accepted `collaboration.spawn_agent` request with `fork_turns: "none"`. Record the original accepted request, response, source reference and agent ID. Unless the current main chat is explicitly routed as `gpt-6-astra` and that requested route is evidenced, dispatch `gpt-6-astra` as a planner and final acceptance agent; a role title does not change the main chat model. Reuse retains original spawn evidence and is marked reuse. Requested routes do not prove serving identities; report actual model/effort as unknown unless authoritative runtime metadata exists.

| Role | Required requested route | Responsibility |
| --- | --- | --- |
| Planner and final acceptance | `gpt-6-astra` | Fix original requirements, slice boundaries, evidence and final decision. |
| Coding worker | `gpt-6-sol`; record effort chosen for task | Implement one bounded scope, report changed files, exact checks and pending tools. No nested agents. |
| Independent reviewer | `gpt-6.1-sol` / `max` | Inspect current diff, interfaces, target evidence and unresolved limits; save judgment. |

If any requested route is unavailable, stop that stage with a scoped blocker. Do not silently substitute a model or provider. A plan-only request starts no worker.

## Isolate, implement and verify

Inspect project instructions, target Git root, HEAD, selected content and dirty work before edits. Use an isolated Git candidate and exact allowed paths. Save a baseline identity and current source manifest; these hashes bind cooperative evidence, not host permissions. Set actual host tool/file/command scope for the worker, choose effort from task complexity, and hand off the original requirements, real interfaces, exact target test commands and known limits. The owner observes every worker dispatch and pending tool. An ACK or worker DONE text is not a command result. Wait for terminal tool exits, inspect complete logs and resource disposition, and reconcile uncertain side effects before retrying.

Inspect the candidate diff and compare it to target HEAD/content before applying. Preserve dirty target files and stop on a stale baseline. Run the required checks in the actual target environment; save exact argv, exit status, full logs, failures and skips. Local worker tests do not replace target checks. For remote code, use the existing authorized SSH path and target test environment. Remote editing/testing remains a manual coordinator operation; this guide does not provide a native SSH adapter. A lost connection leaves the target outcome unknown until inspected. Missing dependencies or unreachable target checks are unverified blockers, not PASS.

OMP strict Bash enforcement, async instrumentation, prepared process schemas and frozen budgets do not cover native tools. The owner records the observed invocations/results and states gaps honestly. Native scope instructions and local hashes are not a security boundary. Never call `team_dev.py prepare`, `start`, `record-review` or `finalize` as though they represent a native worker.

## Review, receipt and acceptance

Freeze the candidate being reviewed. Supply the reviewer the original requirements, source identity and manifest, diff, actual target tests/logs, pending resource state and known gaps. Save the original accepted `gpt-6.1-sol` / `max` spawn as a parent-observed JSON extract in the consuming evidence root before generating fields. [Reviewer provenance](reviewer-provenance.md) defines its shape and limits. Generate exact metadata with:

```text
python3 -B scripts/review_handoff.py template --evidence-root /absolute/evidence --dispatch reviewer-dispatch.json --reviewer-model gpt-6.1-sol
```

The template is metadata only; the reviewer independently writes findings and verdict. A former `gpt-5.6-sol` review cannot satisfy the selected native route. Preserve failed/rejected reviews. Confirm the saved review's bytes/hash and unresolved findings. Fix verified findings, rerun affected target checks and re-review changed scope; do not silently retain stale acceptance.

Astra then independently checks original requirements, planner/worker dispatch records, current candidate/target bindings, actual tests, process gaps and review. Save its acceptance and the hashes of planner/worker dispatch and acceptance artifacts in coordinator evidence. The narrow receipt writer does not validate those declarations. After Astra acceptance and a real passing reviewer verdict, write a separate native receipt, with a pinned review SHA-256 and a nonempty coordinator draft:

```text
python3 -B scripts/review_handoff.py receipt --evidence-root /absolute/evidence --review sol-review.json --expected-review-sha256 ACTUAL_SHA256 --draft completion-draft.json --output completion.json --reviewer-model gpt-6.1-sol
```

Check the writer's own exit status and returned receipt hash before reporting publication. The writer validates reviewer verdict and dispatch provenance only. It does not certify planner/worker routing, source hashes, target tests, scientific outcomes, deployment or full acceptance. Actual serving identity remains unknown without runtime evidence. No receipt is inferred from a filename or shell continuation. The old OMP `POLICY` and normal `record-review`/`finalize` gates remain `gpt-5.6-sol` / `max`.

## Bounded recovery

Only the lifecycle owner may spend one coordinator recovery attempt for an unchanged deliverable. First inspect candidate, target, completed/pending tool invocations, full logs and resource state. Avoid replaying commands whose effect is uncertain. Hand only outstanding work to a fresh, explicitly routed worker when safe; keep failed attempts and new dispatch evidence separate. If recovery fails, report a blocker or an explicitly described coordinator takeover with fresh checks and independent review. There is no automatic native-to-DeepSeek or DeepSeek-to-native fallback. A later candidate change invalidates affected test/review/acceptance bindings.
