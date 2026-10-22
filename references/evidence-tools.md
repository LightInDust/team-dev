# Read-only coordinator evidence tools

`scripts/evidence_tools.py` is a stdlib-only, read-only viewer over saved run
evidence. It answers two coordinator questions without touching a live target:

- **Where does a multi-run effort stand, by recorded evidence?** — `index`
- **What is the indexed review package for one run?** — `bundle`

It never inspects processes, runs tools, uses the network, writes inside a run,
launches/retries work or edits worker state. It reads a fixed allowlist of
evidence filenames from each explicitly named run and reports *recorded* bytes.

## Commands

```text
python3 -B scripts/evidence_tools.py index --manifest /absolute/manifest.json
python3 -B scripts/evidence_tools.py index --manifest /absolute/manifest.json --output /absolute/dir/index.json
python3 -B scripts/evidence_tools.py bundle --run /absolute/run
python3 -B scripts/evidence_tools.py bundle --run /absolute/run --output /absolute/dir/package.json
python3 -B scripts/evidence_tools.py invocations --run /absolute/run
python3 -B scripts/evidence_tools.py invocations --run /absolute/run --output /absolute/dir/ledger.json
```

Without `--output` the JSON result is printed to stdout. With `--output` only a
small `{"output": ..., "sha256": ...}` receipt is printed; the file is created
exclusively (`os.link`, mode `0600`) and is never overwritten. Output is
rejected when it equals an input or would be written inside any input run
directory. Argument or parse errors exit nonzero and create no output file.

## Recovery manifest

```json
{
  "version": 1,
  "coordinator_agent_id": "/root/astra",
  "slices": [
    {"id": "core", "runs": ["/absolute/run-a", "/absolute/run-b"]}
  ]
}
```

`version` must be the integer `1` (not `true`); `coordinator_agent_id` a
nonblank string; each slice a nonempty `id` plus a nonempty list of absolute run
paths. Malformed types, duplicate slice ids, duplicate or canonical-equivalent
run references (including `..` and symlinked duplicates), and more than 16 runs
are structural errors: the command exits nonzero. Each index entry reports the
recorded process/delivery, task identity, selected fingerprints, every evidence
path with its observed SHA-256, open/unsafe/conflicting/missing evidence and a
suggested next inspection.

## Invocation ledger

`invocations --run /absolute/run` joins the saved **request**, **pre-execution
gate** and **terminal result** records of one run by tool-call ID, so the
coordinator reads actual invocations instead of a worker narrative. Rows keep
tool-journal **start order** first, then gate-journal-only decisions, then
tool-journal end-only rows; each row states its `order_basis`
(`tool_journal_start`, `gate_journal_decision`, `tool_journal_end`) and the
document states `ordering.cross_journal_chronology: unknown`, because the two
journals carry no shared clock. Repeated identical command hashes are kept as
separate rows; nothing is deduplicated, merged, guessed or re-ordered against
the gate journal.

Every row reports the call ID, the recomputed `command_sha256` (**exact**
recorded command bytes — no trimming) and the separately computed
`policy_command_sha256` (the existing native strip normalization) with
`command_match` against the frozen `task.worker_bash_policy.allowed_command_sha256`
(policy gate unchanged), plus `command_source`:

- `request` — the raw hash comes from the retained request start arguments.
- `gate` — no usable start command; the strip-normalized hash comes from the
  gate decision and the raw `command_sha256` stays `unknown`. Raw start
  arguments are never silently replaced by gate arguments.
- `absent` — neither record carries a usable command; both hashes stay `unknown`.

Gate classification and decision are separate. Native `undeclared_command`,
`invalid_arguments` and `unavailable` classifications require `block` and
`denials >= 1`; legacy `denied/block` is also retained. `allowed/allow` is
normal, while `allowed/block` is valid only when the count reaches the positive
integer `task.worker_bash_gate.max_denials` saved with that run. A missing limit
cannot establish exhaustion. This reader never changes the gate or its limit.
`gate_denials` must be a nonnegative integer. Request/terminal shapes are equally
required: a nonempty command in the required arguments, `is_error` boolean,
an object `result.details`, an integer `exitCode` when present, and integer `1`
(not `true`) as the tool-record envelope version. An empty details object is
valid: an absent `exitCode` remains unknown; an explicit null is malformed.
Missing or malformed required values stay `unknown`/invalid, keep the row out
of `binding_complete`, and are never reported as agreement.

`executed` is **tri-state** and never an OS-execution attestation:
`true` only for a supported, matched, fully bound allowed request; `false` only
for a coherent preexecution block, including OMP start/end wrappers with no
test log marker. Such wrappers record the rejected request, not an executed
test. Malformed request arguments can keep its binding incomplete while the
block remains explicit. `unknown` applies to anything else — a start with no usable synchronous
terminal evidence, an orphan, a duplicate, a conflict, a shared log directory or
an async shape. Aggregate `invocations` counts only explicit `true` rows;
`observed_starts` exposes the retained start records separately.

An allowed `binding_complete` requires all of: a start record, a terminal
result record, exact start/gate argument agreement, one **unique** canonical
`WORKER_TEST_LOG_DIR=` marker (no surrounding text, no whitespace), a single
unique log directory inside the run, the three regular bounded log files read
as `read`, and a valid retained numeric `exit-code`. A terminal result object
alone is not a binding: a missing marker, unknown path, missing/oversized/
nonregular file or non-numeric exit keeps `binding_complete` and `complete`
false with a visible issue. Rows also report `gate_status`/`gate_decision`/
`gate_denials`, `start_present`/`end_present`/`terminal_result_present`,
`args_agree` (`unknown` when a side is absent — never assumed to agree),
`log_dir`, `exit_code` (retained numeric file), `terminal_exit_code` (the
result's own `exitCode`, kept `unknown` when the shape omits it or is
non-integer), per-file log hashes, row `issues` and `binding_complete`.

Any `details.async` key — the native `{jobId, type: bash, state}` shape or a
malformed marker — and any legacy job-id key is retained as `unsupported`: no
job is joined, no affirmative `executed` is recorded and the row stays
incomplete.

Reading is bounded and narrow: only `task.json`, `tool-evidence.jsonl`,
`bash-gate-events.jsonl` and the three files inside one canonical
`worker-test-logs/check-[A-Za-z0-9]{1,64}` directory. JSONL files are capped at
4 MiB, 2048 records and 128 invocation IDs; log files at 4 MiB and `exit-code`
at 64 bytes. A log directory is bound only from a single exact
`WORKER_TEST_LOG_DIR=` line whose parent resolves to the run's own
`worker-test-logs`; multiple markers, traversal, other-run and outside paths are
rejected before anything is opened, and both directory levels are opened with
no-follow descriptors, so a symlinked parent, a symlinked leaf, a FIFO or an
oversized file is reported (`symlink`, `nonregular`, `oversized`) instead of
being followed, read or wholly allocated.

Use the ledger as the coordinator's invocation table: tri-state `executed`
(`invocations` counts explicit `true` only, `observed_starts` reports the
retained starts), the tool-journal ordering with `unknown` cross-journal
chronology, which attempts were denied and never executed, which started
without a terminal result, duplicates/orphans/conflicts, async shapes that
remain unsupported, and the retained exit status per attempt. Then, per the
existing report rules:

- **Preserve the original worker report** and append corrections, unknowns and
  reconciliations; never rewrite it to match the ledger.
- **RED stays RED.** A later GREEN does not retire an earlier failing attempt;
  both rows remain, in order, with their own retained status.
- **A complete binding is not acceptance.** `acceptance_certified`,
  `current_target_verified` and `current_liveness_verified` stay false; the
  ledger labels stored evidence only. A bound command hash is permission
  evidence, not proof that the test passed, was meaningful or had no side
  effects, and the process/acceptance distinction remains the coordinator's to
  draw with the ordinary declared tests and Sol review.
- **A denied gate request is not a test invocation** and is never counted as
  one; missing, malformed, duplicate, orphaned, conflicting or async records
  stay visible and keep `binding_complete`/`complete` false rather than being
  silently repaired.

## An index is navigation, not acceptance

Every index and bundle states `current_liveness_verified: false`,
`current_target_verified: false` and `acceptance_certified: false`. Recorded
states are labelled as recorded and are never promoted to a fresh result:

- Recorded `running` → inspect the existing run; do not start a duplicate.
- Recorded `verified` → reconcile the current target and acceptance; never
  auto-skip re-verification.
- A recorded failed candidate → inspect the preserved candidate before retry.
- Unknown, missing or malformed evidence stays unknown/a visible issue.

Fingerprint fields are compared, not merged: every known applied-target
fingerprint (`metadata.applied_fingerprint`, `applied.fingerprint`,
`tests.fingerprint`, `review.fingerprint`) is compared pairwise — independently
of whether an intermediate workflow file is present — and a `changes.patch`
whose bytes do not match `proposal.patch_sha256` is reported as a conflict. Any
such conflict, and any absent optional workflow file, keeps the entry out of
`evidence_complete`/`complete`.

## Stored versus fresh evidence

The tools hash bytes that are already on disk. A stored SHA-256 is a declared
value, not fresh verification of the target or of the recorded verdict; a
same-user JSON file can be edited. Reuse is safe for navigation and focused
review, but the coordinator still owns every current acceptance blocker outside
the diff: re-run the declared tests on the current target, re-check target
identity, and re-confirm the requirement ledger before finalizing. Ledger rows
are the same stored evidence: `binding_complete` describes record completeness,
not a passing or meaningful test.

## Private evidence handling

For `index` and `bundle`, only fixed basenames are read: `metadata.json`,
`task.json`, `proposal.json`,
`applied.json`, `tests.json`, `review.json`, `failure.json`, `termination.json`,
`progress.json`, `command-audit.json`, `worker-result.json`, `changes.patch`, and
`test-0` … `test-15` `.stdout`/`.stderr`. Every selected file is opened once
through a single no-follow, non-blocking descriptor; that descriptor is
`fstat`ed as a regular file and read for at most `limit + 1` bytes, so a swapped
symlink, a FIFO or a grown file is classified (`symlink`, `nonregular`,
`oversized`) instead of being followed or wholly allocated. Nonregular,
symlinked or oversized files (JSON > 1 MiB, patch/log > 4 MiB) are reported as
issues and never parsed. JSON-supplied paths are never followed: a `tests.json`
log reference is accepted only as the canonical `test-N` basename for its own
stream kind, so `test-00.stdout`, an off-kind stream name and an unbounded digit
string are flagged and skipped without any integer conversion. Each selected log
is hashed from its bytes and compared against the recorded
`stdout_sha256`/`stderr_sha256`: a present but malformed or mismatching digest is
a conflict, while an absent legacy digest stays explicit `unknown` plus a
visible gap. Every parsed manifest/evidence JSON also rejects non-finite numbers
(`NaN`, `Infinity`, overflowing exponents) as invalid, so no public result ever
carries a value that `json.dumps(..., allow_nan=False)` cannot emit.
Credentials, `invocation.json`, session files, `events.jsonl`, live processes and
recursively found files are never opened by any subcommand, and `invocations`
reads the narrower allowlist above and never a worker narrative or
`worker-result.json`. `worker-result.json` is exposed as a pointer plus hash only
— the worker's final report is not authoritative output. Absent optional workflow
evidence (proposal/applied/tests/review) is a visible gap, and a gap keeps the
bundle out of `complete`/`evidence_complete`; missing or invalid
`metadata.json`/`task.json` marks the dependent bundle incomplete rather than
complete.
