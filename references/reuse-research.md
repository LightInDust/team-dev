# OMP and subagent reuse findings

Historical assessment against OMP 18.2.8 and the linked pinned primary sources; newer upstream versions may differ. Superpowers is reference material at commit `bb92a77741419a4ab5f06e711a283343f1ada0c3`, not a required installed dependency. See [the scoped adaptation](superpowers-adaptation.md).

| Component | What it offers | Decision for this workflow |
| --- | --- | --- |
| [OMP Python RPC client](https://github.com/can1357/oh-my-pi/tree/df624f56b0508c51067a70422606cac898ac2bcb/python/omp-rpc) | Typed commands/events, correlation, protocol v2 framing, steering, abort and session state | Reused in `scripts/vendor/omp_rpc`; pinned commit and MIT license retained. One documented environment-isolation adaptation. |
| [OMP native task tool](https://github.com/can1357/oh-my-pi/blob/df624f56b0508c51067a70422606cac898ac2bcb/docs/tools/task.md) and [agent discovery](https://github.com/can1357/oh-my-pi/blob/df624f56b0508c51067a70422606cac898ac2bcb/docs/task-agent-discovery.md) | Batch/async subagents, per-agent model/effort, isolated patches, bounded concurrency and agent lifecycle | Useful if OMP later becomes the coordinator. Current Codex Astra coordination needs no nested OMP task fan-out. Bundled agents are prompt contracts, not a Codex-native model bridge. |
| [pi-subagents skill](https://github.com/nicobailon/pi-subagents/blob/main/skills/pi-subagents/SKILL.md) | Single-agent delegation and scripted sequences/fan-out, compact task contracts and role ownership | Reuse scope and handoff ideas. The package describes itself as a Pi extension and its skill depends on its `subagent` tool; copying the Markdown alone does not supply that runtime in Codex or prove OMP compatibility. Not installed. |
| [Superpowers subagent-driven-development](https://github.com/obra/superpowers/blob/bb92a77741419a4ab5f06e711a283343f1ada0c3/skills/subagent-driven-development/SKILL.md) | Fresh implementation context per task, specification/quality review, feedback loops | Adopt the scoped coordination guidance while keeping the existing Sol max review and helper gates. Installing the upstream suite is optional; improved runtime stability is not established by adopting its documentation. |

The [official RPC reference](https://github.com/can1357/oh-my-pi/blob/df624f56b0508c51067a70422606cac898ac2bcb/docs/rpc.md) distinguishes command acknowledgement from agent completion and provides `steer`, `abort`, `abort_and_prompt`, thinking-level controls and session APIs. This helper uses queued steering and abort, then an explicit bounded restart with `--resume` for recovery. It does not automatically call `abort_and_prompt` in a loop.

The OMP source also contains prompt compression/optimization skills. They are not a ready-made Astra–DeepSeek–Sol cross-runtime scheduler. No compatible turnkey skill was established in this search; this is a scoped finding, not a claim that none exists anywhere.

Recommended composition remains: Codex `team-dev` skill owns task contracts and acceptance, official OMP RPC owns worker transport, native Codex Sol owns independent review. API keys stay on the Mac; remote repositories need only their existing SSH, Git and test environment.

## Execution-hook feasibility

Source-checked 2026-09-29 against tagged OMP 18.2.8. Its [extension API](https://github.com/can1357/oh-my-pi/blob/v18.2.8/docs/extensions.md) exposes `tool_call` before execution and permits a blocking result. The [wrapper](https://github.com/can1357/oh-my-pi/blob/v18.2.8/packages/coding-agent/src/extensibility/extensions/wrapper.ts) handles interception before invoking the underlying tool; loop-issued calls are intercepted at argument preparation. This is a different boundary from the helper's RPC `tool_execution_start` observer. New strict team-dev preparations now load a pinned gate; historical and standard-mode process stops remain observational.

The [loading rules](https://github.com/can1357/oh-my-pi/blob/v18.2.8/docs/extension-loading.md) allow explicit `-e/--extension` paths with `--no-extensions`, which disables ambient extension discovery. The integration retains that flag and loads one pinned local file; removing the flag alone neither implements the gate nor proves isolation. The current implementation explicitly loads its frozen local gate and tests it against the actual 18.2.8 binary with a scripted loopback provider; no paid model request is part of that test.

The implemented gate is checked against these conditions:

- Freeze an explicit new policy plus hook identity/hash for new preparations, retain legacy behavior, and confirm registration before dispatch. Missing/failed registration must prevent launch rather than silently reverting to observation-only behavior.
- Apply the same exact prepared Bash command and argument rules before invoking Bash, retaining source edits on edit/write and completed-log reads on read. Reject with a tool-selection explanation; do not translate a shell replacement into an inferred edit or expand the allowlist.
- Record denied request identity, rule and non-execution evidence. A pre-execution denial may lack the usual tool-start event, so RPC audit/review must consume an explicit bound denial record and distinguish denial from executed failure, successful execution and unknown outcome. Keep old failed runs unchanged; any correction/retry behavior needs an explicit new bounded policy, not erased evidence.
- Test allowed tests/status propagation and denied sd/cat/rewriting requests with a sentinel proving the underlying tool never ran. Cover registration failure, policy mismatch, cwd/control arguments and applicable loop/direct dispatch paths in the actual OMP runtime with a scripted loopback provider and without a real model request or business experiment. A mocked hook alone cannot establish the installed execution boundary.

These conditions govern the implemented [pre-execution gate](task-contract.md#pre-execution-bash-gate). The tested 18.2.8 runtime emits tool-start/end even for a denied call; the bound decision and blocking result distinguish prevention. Prompt text alone and this same-user extension do not provide a host security sandbox.
