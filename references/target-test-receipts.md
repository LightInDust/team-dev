# Target-test receipts and interrupted capture

Target tests execute before the helper collects the final repository manifest and identity. A later SSH or capture failure does not erase a test that already ran. Preserve the original run and inspect its local receipts before deciding what to do next:

```bash
python3 -B <skill>/scripts/team_dev.py test-receipts --run <run>
```

This command reads local evidence only. It does not contact the target, invoke a provider, rerun a test, or rebuild `tests.json`.

## What a new invocation preserves

Each invocation creates an exclusive directory under `target-test-attempts/`. Records are appended without overwriting prior records. Command intent is saved before execution; after a command returns, its captured streams, hashes, observed transport return and known declared test exit are saved before the next command or repository capture. Before/after identity and manifest stages, report binding, completion and failure records distinguish execution evidence from capture completion.

An intent without a result proves only that execution was intended. SSH return 255 or a local transport deadline leaves the declared test exit unknown; partial output and an observed transport code do not prove success. A complete capture can still contain failed tests. The completion marker is not a PASS verdict.

## Acceptance and recovery

New receipt evidence must be complete, consistent and bound to the frozen task, applied candidate, current report and captured logs before a PASS review can be recorded or finalized. Finalization checks it again, so a previously accepted review does not waive evidence that was subsequently lost or changed.

Do not repeat `test` after an incomplete capture, unknown exit or invalid receipt chain. Preserve the run, report the unresolved phase and reconcile the target's actual execution state. Any further execution needs a fresh authorized task that accounts for possible side effects; a new run is not permission to replay work whose outcome is unknown. Do not hand-edit receipts or construct a replacement report to unblock acceptance.

Older runs have no per-invocation receipts. A complete, internally consistent legacy report with known exits and matching commands, logs and bindings may still be explicitly retested under the existing workflow. Orphan logs, a missing report or unknown results remain unresolved. Do not infer historical process exit codes from worker metadata, test text, or the name of a log file.

Receipt durability is bounded by the local filesystem: each record uses exclusive creation and file synchronization, with directory synchronization where supported. A failure during persistence may leave partial evidence; inspection exposes that gap instead of inventing a result. These receipts do not establish scientific correctness, cancel remote work, change timeout policies or automatically retry commands.
