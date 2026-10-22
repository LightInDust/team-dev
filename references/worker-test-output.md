# Worker acceptance test output

Run every worker acceptance test through the prepared `worker_logged_test_commands` (the `T#.logged` reference — `T1.logged` for `T1`), regardless of expected output length, so the full stdout/stderr and the saved exit-code are retained for review. Each Bash command runs the corresponding exact `worker_test_commands` argv from the snapshot root; it changes output destinations, not the project framework, test selection or flags. Keep a finite Bash timeout within the frozen task budget. Target-only tests remain NOT VERIFIED locally and are left to the coordinator's existing target environment.

New prompts also provide numbered `worker_test_invocations` entries with ready-to-submit Bash `arguments`. Copy the `T#.logged` object and submit it unchanged. `T#.direct` keeps the unchanged direct command and remains permitted by the unchanged runtime catalog, including for frozen/legacy tasks, but it is not the new prompt default; only an explicit coordinator-authored exception may retain it, and that keeps the missing saved-log and exit-code evidence limitation. The IDs are labels, not shell commands. Do not reconstruct the logging envelope, environment-variable prefix or exit tail. If strict gate v2 rejects a request, use its parameter/mismatch hint to locate and recopy the frozen entry. A corrected call still has to match the original allowlist and remaining correction budget.

The generated shell command creates a unique mode-0700 `check-*` directory under the prepared private `worker_test_log_root`, prints its absolute path before starting, and writes:

- `stdout.log` and `stderr.log`: separate full byte streams received from that invocation, with private file permissions.
- `exit-code`: the shell-observed test status, written only after the declared test process returns. The Bash command exits with that same status. If allocating or recording evidence fails, it exits 125 rather than claiming a successful test.

These directories are authorized retained run evidence outside the snapshot, not disposable fixtures. Do not remove or overwrite them. Every invocation gets a fresh directory, including failed tests and reruns. The envelope uses the real executable directly; it supplies no fake runner, dependency or collection result. It has no retry, timeout extension, process-kill mechanism or cleanup command.

The saved `exit-code` is independent evidence from that test process. After the matching async delivery, read it directly instead of inferring success from notification wording, `tool_execution_end`/`agent_end` or lifecycle completion, and never synthesize an exit value or notification status. If an earlier direct invocation's outcome was never captured, it stays unknown; do not rerun it merely to rewrite that record.

After synchronous completion or the matching terminal background notification, use the native `read` tool for all three files. New prompts include `worker_test_log_read_templates`: replace `<WORKER_TEST_LOG_DIR>` with the exact absolute directory printed after `WORKER_TEST_LOG_DIR=` for that invocation, then pass each `arguments` object directly to `read`. Do not use the parent log root, a guessed directory, another attempt or a shell variable. The requests are:

```json
[
  {"tool":"read","arguments":{"path":"<WORKER_TEST_LOG_DIR>/exit-code"}},
  {"tool":"read","arguments":{"path":"<WORKER_TEST_LOG_DIR>/stdout.log:1-200"}},
  {"tool":"read","arguments":{"path":"<WORKER_TEST_LOG_DIR>/stderr.log:1-200"}}
]
```

These are templates, not literal paths or Bash commands. OMP 18.2.8's [read schema](https://github.com/can1357/oh-my-pi/blob/v18.2.8/packages/coding-agent/src/tools/read.ts) takes `path` with inline selectors; do not invent separate `offset`/`limit` arguments. Continue through bounded ranges using `read` as needed. If the directory marker is missing or a file cannot be read, report the blocker and available evidence; do not launch a Bash fallback or rerun the test just to recover the path. Wait for completion rather than polling running output files.

In `declared_tests_only` mode, Bash `cat`, `head`, `tail`, `grep` and shell wrappers remain undeclared commands even for saved logs or a one-line `exit-code`, and even after a successful test. Use the native read tool. The passive-read examples later in this reference describe the live-output classifier, not exceptions to strict Bash policy. A successful log read does not erase a process violation or finalize a failed worker.

State the status and evidence path alongside any excerpt; the excerpt is for navigation, not the full test receipt. Do not rerun a test merely to obtain shorter output. Do not pipe the test through `tail`, `head`, `grep`, `tee` or another filter, or append a successful print/read that hides its exit status. Changing verbosity/test selection still requires the task's authorization. A previously truncated console record remains truncated; this recipe cannot reconstruct its missing bytes. This prompt guidance affects new preparations only; existing frozen prompts, envelopes and command hashes remain unchanged.

If a timeout, abort or transport loss interrupts the command, preserve its partial logs. Missing/unreadable `exit-code` means no recorded final test status; file existence or an async acknowledgement is not success. A status only describes the declared test process, not all possible descendants. Let the coordinator inspect authorized owned resources before any restart or cleanup; do not infer cleanup from the log directory or a test's own assertion. Retained log evidence must remain distinct from the test's disposable fixtures.

The private Bash journal records the complete generated command. Its shell control/redirection syntax remains outside the literal auditor's subset, so `unsupported_syntax` still needs Sol's ordinary manual coverage disposition. The recipe is not an audit exemption: inspect the actual command, exact test argv, output files and any deviations. Worker logs are not automatically substituted for, hashed into or accepted as `tests.json`; the coordinator still runs the existing direct target-test commands and preserves their bound full logs. When a process-review claim relies on worker log contents, record and check those specific file hashes as review evidence without claiming an added automatic helper gate.

## Manual probes and audit cues

Keep the generated envelope unchanged, including `exit "$team_dev_test_exit"`. Replacing this with `printf`/`echo`, appending another successful command, or resetting the saved status can hide failure from the outer Bash result. A retained `exit-code` is valuable evidence but does not excuse that change. Compare the actual request to the prepared command, check all invocation logs/statuses, and preserve the deviation separately from later accepted code. An expected exit tail alone cannot validate the rest of an envelope.

Where such probes are authorized, the same full-output and explicit bytecode-guard rules apply to dependency imports, inline diagnostics, CLI smoke checks and manual layout/render probes. In `declared_tests_only`, this recipe does not authorize extra probes, including standalone `ast.parse`; use only exact prepared tests or report missing validation. For authorized probes in ordinary mode, run short probes directly; for large output, the coordinator can use `logged_worker_test_command(probe_argv, worker_test_log_root)` to generate the same logging envelope. Keep the declared interpreter, label probes exploratory, and do not replace exact acceptance argv. Never filter the live command with `head`, `tail`, `grep` or `tee`; read retained logs by bounded read-tool ranges after completion. This restriction concerns the producer's output stream, not a later authorized read of an existing log.

Disposable render outputs belong inside `with tempfile.TemporaryDirectory()` together with every consumer and assertion. Importing `tempfile`, using a fixed `/tmp` filename, or catching an exception before saving is not equivalent. If an attempted fixed-path render fails before `savefig` and the path is now absent, record both the command deviation and that limited filesystem observation; absence does not erase the request or prove host-wide cleanup. Do not rerun the renderer or delete an unowned path merely to resolve audit wording.

Newly launched workers emit command-audit v2. It retains the original literal subset and adds bounded `review_cues` (up to 32,768 command characters):

| Cue | Review action |
| --- | --- |
| `worker_log_envelope_mention` | Compare the entire actual envelope with the generated command and inspect test argv/logs/status. |
| `worker_log_exit_tail_not_observed` | Check for a replaced/missing final status return; prints are not exit propagation. |
| `output_filter_pipeline_mention` | Inspect a `head`/`tail`/`grep`/`tee` pipeline, including manual and dynamic-interpreter probes. |
| `python_header_without_explicit_guard_mention` | Inspect the recognized Python header and its local env prefix; application/body `-B` text is not an interpreter option. |
| `dynamic_interpreter_probe_mention` | Resolve the actual variable-selected interpreter/options from private evidence; a variable call is not proven guarded. |

These are lexical hints, not shell execution, a shell AST or dataflow validation. Examples/comments can match. Altered status assignments with an apparently correct tail, constructed names/paths, aliases, other filters and wrapper behavior may escape a cue. The full-envelope cue still requires review even when the exit-tail cue is zero. Quoting, heredocs and control flow remain `unsupported_syntax`; cues never promote them to inspected coverage. Parse failures, unavailable arguments and over-limit requests stay explicitly uninspected. Compact evidence stores only fixed cue names/counts, not raw paths, inline code or environment values.

`review_cue_counts`, `probe_scanned_calls` and `probe_unavailable_calls` are added only when v2 records occur; mixed v1/v2 logs count v1 calls as unavailable to the new scan. `compliance` remains `not_assessed`. These cues do not change the existing automatic stop categories or add pre-execution interception. Sol uses the existing bound review and coverage/temporary-path disposition; confirmed violations still cannot be reported as process success. Old v1 records and review bindings are replayed with v1 rules, without rewriting them. A separate reinspection report may add observations without replacing original evidence.

## Live producers versus saved-log reads

A dependency import is a live diagnostic, even if its intended output is one line. `python3 -B -c "import plyfile" 2>&1 | tail -1` can display an import error while returning the filter's success. `-B`, `pipefail` or an apparently successful Bash tool result does not authorize discarding live output. Run the authorized producer directly or with its status-preserving full-log envelope, then read the completed logs separately.

Process-evidence policy **v3** (standard/legacy tasks) adds a separate `liveOutputAudit` without changing command-audit v1/v2 replay. New strict tasks use v4: its execution gate first blocks undeclared Bash requests, allows two corrections and stops on the third denial. The v3 observation rule below applies only to requests not already prevented by a validated gate; see [pre-execution Bash gate](task-contract.md#pre-execution-bash-gate). It recognizes a bounded subset of complete simple shell lists and pipelines: literal Python executables (including quoted absolute paths), plain assignment/env/command/exec prefixes, opaque whole quoted arguments, `;`/newline/`&&`/`||`, and `1>&2`/`2>&1`. A Python stage feeding a later `head`, `tail`, `grep` or `tee` stage requests `python_live_output_filter`. This includes dependency imports, diagnostics, modules and script invocations; the scanner does not infer a Python program's purpose. Use the read tool or ordinary log-reading utilities for already saved logs rather than a new filtered Python invocation.

| Request | New rule |
| --- | --- |
| `/absolute/env/python3 -B -c "import pkg" 2>&1 \| tail -1` | Recognized live Python output filtering; stop after observation. |
| `cat completed.stderr.log \| tail -20` | No live Python producer; this rule does not stop the read. |
| `tail -20 completed.stderr.log` or `grep error completed.stderr.log` | Passive log read; retain the full file and original producer exit status. |
| `python3 -B probe.py; tail -20 completed.stderr.log` | Separate commands; the latter read is not a pipeline from Python. Full producer status still requires review. |
| `echo 'python3 -c "import pkg" \| tail -1'` | Quoted example data, not a Python pipeline. |

Expansions/variable interpreters, substitutions, compound shell control, heredocs, quote concatenation, comments, other redirections and over-limit input remain unsupported/unavailable to this new recognizer. Other executables, aliases/functions and filters outside the listed set are not certified. Existing v2 text cues may still flag these requests for manual review. Unknown coverage and zero recognized patterns never establish compliance; “simple_lists” describes the parser subset only. Branch execution is not proven by a request.

For v3, live progress and worker results expose `live_output`; the complete process binding includes that nested summary and rechecks each `liveOutputAudit` against the private original request. A recognized request stops the worker with the existing `process_constraint_request` reason and cannot pass process review or resume to erase the attempt. If coordinator cancellation wins the reason race, retain that reason; resume still checks available raw requests. The command may have finished before observation: there is no pre-execution block or guarantee of stopping arbitrary descendants.

Existing process-policy v1/v2 runs retain their original stop/binding behavior. Do not edit or reclassify them retroactively; add separate read-only observations if useful. V3 continues all v2 async/lifecycle requirements. This is the existing review stage, not a new approval flow.

Use the exact interpreter/environment declared for tests when an exploratory probe is authorized. A PATH-resolved `python3` and an absolute virtualenv Python can load different packages despite both having `-B`/environment guards. Do not switch runtimes or install missing packages to turn a probe green; report NOT VERIFIED and the dependency/environment. If a task allows only the listed tests or forbids additional Bash/Python probes, obey that narrower scope. The helper does not automatically enforce arbitrary semantic probe bans or interpreter equivalence.

## Steering while the command runs

OMP can background an active Bash command to process an incoming steer, even when the worker did not request background execution. Keep its original tool-call ID, returned job ID and printed log directory associated with one invocation. The command continues; neither the returned running status nor the steer ACK means it stopped.

Do not duplicate the test or mutate inputs/resources it still uses. Do independent work or yield, following OMP's no-poll notice. Wait for the structured notification, then inspect the outcome and recorded files. Batched notifications still need a separate disposition for each job. `delivery=delivered` only records receipt of the notice; success, numeric exit status and resource cleanup remain separate questions.

When sending a steer during an active tool, Astra should state whether it applies after the tool finishes or requires an explicit cancellation decision. Avoid treating a scope-change message as implicit termination. A failed run stays failed; use the existing [async lifecycle procedure](async-bash-lifecycle.md) for owned children and scratch, without restarting or signalling unknown resources.

## Bounded context inspection

For large JSON/source context, use a known file and a narrow line selector with
native `read`/`grep`, plus a specific field/symbol pattern. OMP 18.2.8 grep accepts
`pattern`, `path`, `case`, `gitignore` and `skip`; it has no `limit` or
`max_results` argument. A `path` such as `input.json:50-100` bounds one known
region; `skip` paginates files, not matches within a file. See the pinned
[grep schema](https://github.com/can1357/oh-my-pi/blob/v18.2.8/packages/coding-agent/src/tools/grep.ts).
Expand ranges deliberately when required context is still missing. An inspected
slice is not proof of whole-file coverage. Avoid repeatedly listing a large
JSON document's keys or rereading unchanged source just to prepare a report.
These context-size hints preserve standard-mode permissions; strict Bash still
allows only its exact prepared tests. They never replace complete test-log
inspection or authorize a Bash fallback when native tools are insufficient.

For a target-only/no-Bash task, obtain context-size information from already
available coordinator metadata or native-tool result metadata when provided.
Do not request `wc`, a Python probe or another shell command solely to measure
the context. Size is not evidence that the content was read. Line selectors
cannot split an oversized single JSON line, and repeated ranges or grep hits
do not prove access to fields hidden by a tool's output limit. If required
fields remain inaccessible, name the fields and return `NEEDS_CONTEXT`; do not
guess them or substitute a synthetic fixture for a required archived input.

Before a fresh preparation, Astra can provide an authorized, bounded readable
view alongside the unchanged canonical input: record source path/hash, byte and
line counts, selected field paths or record IDs, and the view's own identity.
Select that view into the captured context. Preserve relevant values, types,
order and null/missing semantics; a normal JSON parse/reserialize view does not
preserve duplicate keys or exact source bytes, so it cannot stand in for checks
of those properties. A selected view is partial context, not whole-file review
or an alternative runtime input. If obtaining it requires unavailable target
access, keep the blocker explicit. Do not rewrite an active frozen task or
regenerate an archived experiment to make context readable; use the existing
bounded recovery route and retain all original acceptance requirements.
