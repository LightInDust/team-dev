# Async Bash results and resource evidence

Use this procedure when an OMP Bash tool returns `details.async`. Process-evidence v2 and later capture async results. New `prepare` calls additionally freeze `async_execution_policy` v2 in task and metadata; this separate policy identifies invocations by tool-call ID. Runs without that marker retain the original async tracker/summary v1 and job-ID-only review schema. Do not migrate a running job or rewrite old evidence.

## What each event establishes

The supported shape was checked against OMP 18.2.8 [Bash execution](https://github.com/can1357/oh-my-pi/blob/v18.2.8/packages/coding-agent/src/tools/bash.ts) and [async message assembly](https://github.com/can1357/oh-my-pi/blob/v18.2.8/packages/coding-agent/src/session/async-job-delivery.ts). A running return carries a job ID and means the call has moved to the background. A later custom message has `customType: async-result` and `details.jobs` identities; one packet can deliver several results. Completion wording can accompany a failed command. The formatted message is not guaranteed to expose a structured numeric exit code or a complete output stream. For a logged test invocation, the saved `exit-code` file is the independent test-process evidence: after the matching delivery, read it directly rather than synthesizing a notification exit code or treating lifecycle completion as the outcome. An original direct invocation whose outcome was never captured stays unknown; do not rerun it to rewrite that record.

The helper records these custom packets privately and binds their hashes and structured job IDs to the original Bash tool-call result. It does not derive success, signal sender or cleanup from the words "completed", `isError=false` on a running return, or model prose. Missing metadata, unrecognized IDs, duplicate results, changed private packets or pending jobs block acceptance. A new OMP format must be inspected and tested before widening this parser; text-only guessing is not a fallback.

OMP 18.2.8 can reuse a `bg_N` after its old entry is evicted: its allocator chooses the first ID absent from the current job map, not a run-wide counter. The local bundled implementation and an observed serial reuse after delivery confirm this. New async protocol v2 keeps each `(job_id, tool_call_id)` with a generation, original result hash and its own delivery evidence. Reuse requires the earlier invocation to be delivered (or explicitly inline-terminal) and the new Bash start to occur afterward. An overlapping start, repeated call ID, unknown result, repeated packet hash or malformed batch fails closed. A new running return starts pending; it cannot inherit old delivery or exit/cleanup claims.

The pinned notification carries job IDs, not tool-call IDs. Attribution therefore uses the ordered observed lifecycle and the unique pending invocation for that ID, rather than deriving identity from result prose. Private and compact Bash/notice ordering must agree. Exact old packets cannot close a later generation. This is cooperative protocol evidence, not authentication against a producer fabricating a differently stamped packet for an old job; unsupported or ambiguous upstream semantics require inspection. The summary version is 2 and `pending` lists tool-call IDs; `jobs` retains every generation, even when job IDs repeat. Legacy replay still reports its original duplicate-ID failure.

While jobs remain pending, an early `agent_end` is a yield, not worker completion. The helper waits for event delivery under the existing total deadline without shell polling or another model call. An async notification invalidates that early terminal event; a subsequent terminal event is required. A genuine delivered result updates tool progress; thinking tokens do not. `status`/`worker-result.json` expose delivery state separately from outcome. A pending job at the deadline remains unresolved; shutdown/abort is not proof that every child or temporary directory is gone.

An incoming steer can itself cause OMP to return a background-running job while the original command continues. This does not mean the worker deliberately spawned a detached job. Retain the same tool-call/job/log association; do not repeat the command or modify inputs/resources it still uses. A steer ACK is not cancellation. A queued steer may affect a later Bash call even if no tool was active at ACK time. Prefer status observation for progress-only checks; use steering for a concrete correction, and do not infer causation from ACK timing alone. Wait for structured delivery without polling, then inspect the actual outcome and resource disposition. For worker acceptance tests, use the [prepared logged commands](worker-test-output.md) (the `T#.logged` reference, regardless of output length) and preserve their printed directory; a delivered notification without a numeric exit remains unknown until supported by the saved `exit-code` or actual status evidence.

For a stopped unsuccessful v2 attempt with observed async activity, `resume` refuses before changing evidence. Inspect the original owned resources, then prepare a fresh bounded task if still needed. Preserve failed/cancelled attempts. Never kill a process merely because its PID or name resembles the job; verify authorized host, owner, parent/start identity and actual command first. This helper adds no automatic PID killing or host-wide cleanup scan.

## Avoid unbounded exploratory probes

Use finite timeouts for `wait`, `join`, subprocess communication and IPC operations. Check the dependency ordering: waiting for a writer-ready event before opening the reader can deadlock when FIFO write-open itself waits for a reader. Prefer a small declared regression over an extra exploratory process when it already proves the behavior.

All scratch producers and consumers must finish within their owned temporary context. Capture the actual scratch path before an operation that can block; keep that receipt private. A `TemporaryDirectory` context and exit 130 do not independently prove that unwinding or removal happened. After a relevant failure, distinguish: final tool result, tracked child state, path disposition and code/test acceptance. A later clean snapshot says nothing about a directory outside it.

Use event timestamps for ordering, and separate the time an observation was made from the time a steer message was delivered. A queued message saying a process was running can arrive after an interrupt-result notification. Preserve corrections as addenda without deleting the earlier assertion or attributing a coordinator signal to the CLI automatically.

## Ledger join (read-only, evidence_tools invocations v2)

`evidence_tools.py invocations` reports `ledger_version: 2`. When the private
`tool-evidence.jsonl` carries any async marker it additionally reads, through the
same bounded no-follow reader, `metadata.json` (1 MiB) and `events.jsonl`
(4 MiB, 32768 records). Nothing else is opened, and the ledger starts no process.

The join reuses `async_evidence.protocol_version(task, metadata)` (must be v2)
and `async_evidence.validate_binding(events, tools, version=2)`; it has no
separate job-identity algorithm. A row is bound only for a native
`queued`/`running` acknowledgement (`is_error` false, no marker, no legacy job
key) followed by a delivered custom notice with exactly one bash job of the same
identity. The single canonical `WORKER_TEST_LOG_DIR=` marker is read from the
notice content (string or text blocks), not from the acknowledgement, and the
retained `exit-code` file supplies the numeric status. Rows expose
`result_source: async-notice` and `async_binding` with `job_id`, `generation`,
the acknowledgement `ack_state`, the original result `result_sha256` and the
notice `notice_sha256`; `terminal_exit_code` remains `unknown` because an
acknowledgement is not a synchronous terminal result.

Serial reuse of one job ID after delivery binds distinct generations and logs.
Multi-job packets, legacy/markerless runs, malformed shapes, inline terminal
states, premature reuse, stale replay, duplicate/missing notices, private or
compact order/hash mismatch and contradictory ACK markers stay
`unsupported`/`unknown` and keep the ledger incomplete. Any missing, truncated,
symlinked, oversized, over-limit or duplicate-key metadata/event evidence makes
the whole async mapping unavailable; partial evidence is never bound.
Required-journal faults found before the join — blank/truncated/over-limit
tool or gate rows, an unsupported tool envelope version, a gate lockout or a
duplicate decision — likewise yield `async_inputs_incomplete`, so a supported
join never rests on partially parsed input.

The ledger binds stored provenance only. It does not establish that the test
passed (a retained nonzero exit is still evidence-complete), that the child
exited, that scratch was removed or that the worker attempt is accepted.
`acceptance_certified`, `current_target_verified` and `current_liveness_verified`
stay false; independent reinspection is new evidence, and historical ledger
bytes or failed runs are never rewritten.

## Ordinary Sol review, with per-job evidence

Obtain the final `process-evidence --run` binding after target tests. For every item of `binding.async_execution.jobs`, Astra writes a scoped JSON observation receipt inside the run's authorized evidence area. No extra model stage is added. Sol reads the actual command, complete available async packet, target test evidence, receipt and any contradictory worker claims.

`process_review.async_jobs` is an exact list of observed invocations; use `[]` when there are none. Under async protocol v2, both `job_id` and `tool_call_id` are required, including when job IDs are unique. A reused job ID requires separate entries and receipts. Legacy v1 reviews retain their job-ID-only shape. A v2 entry has:

```json
{
  "job_id": "actual-job-id",
  "tool_call_id": "actual-bash-tool-call-id",
  "outcome": "failed",
  "exit_code": 130,
  "reason": "Describe the observed result and any uncertainty; do not infer the signal sender.",
  "lifecycle_receipt": {"path": "lifecycle/job.json", "sha256": "actual-file-sha256"}
}
```

Outcomes are `succeeded`, `failed` or `cancelled`. Success requires a known zero exit. For a failed/cancelled result without a numeric status, use `null` and state that the exit code is unknown; this cannot support an all-commands-succeeded claim. An expected/explicitly dispositioned exploratory failure can remain failed while the code is accepted, provided the task constraints and lifecycle are actually satisfied. Do not waive a confirmed task violation.

The receipt has `version: 1`, `observer: "coordinator"`, the absolute `run` path, `observed_at`, and `execution` copied exactly from the matching bound job object. It also contains:

```json
{
  "children": {
    "status": "stopped",
    "evidence": "Actual scoped observations with host, owned PID/start identity and postcondition, or an owned child reaped by wait/communicate."
  },
  "scratch": {
    "status": "removed",
    "paths": ["/actual/previously-recorded/owned/path"],
    "evidence": "Actual path observation after all consumers ended; cite the observation record."
  }
}
```

Children may be `not_created` or `stopped`; `not_created` needs a reason grounded in the inspected command and observations. Scratch may be `not_used` (empty paths), `removed`, or `authorized_retained` (nonempty absolute paths plus the authorization/evidence). Unknown paths, unverified children, residual scratch or worker prose alone do not close the gate. `authorized_retained` must be an actual task-authorized deliverable, not a retroactive exemption for leaked scratch.

The helper checks receipt identity, schema, file location and SHA256 at review and finalization; missing, changed or symlinked receipts fail. Sol is responsible for whether the observations support the stated scope. Editable JSON is not author authentication, proof of absence across the host, or a substitute for inspecting the relevant actual process/path. Preserve unresolved facts and use a separately reviewed takeover receipt when full original-process acceptance is unavailable.

## Unsupported command coverage

Inspect manual dependency probes too. For example, a quoted `ls && python -c "import PIL"` can be outside the literal command auditor's coverage while producing zero guard observations. Inspect the exact argv, declared `-B` requirement and actual environment evidence. Zero observations do not establish compliance; absence of a flag does not by itself prove that bytecode files were written. Retain the manual disposition with the ordinary process review.
