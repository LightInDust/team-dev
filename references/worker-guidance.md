# Selectable worker guidance

`scripts/worker_guidance.py` holds the conditional domain instructions that
used to be inlined in `team_dev.prepare`. A task selects the sections it needs;
universal scope, testing, isolation, interface, cleanup and acceptance rules
stay common and are always rendered.

## Task fields

| Field | Type | Meaning |
| --- | --- | --- |
| `worker_guidance` | list[str] | Omitted: select every section (safe default). `[]`: select none. Nonempty: select the named sections. |
| `guidance_assessment` | object | Required whenever `worker_guidance` is present, including `[]`. Needs `by` in `astra`, `sol`, `user` and a nonblank `reason`. |

There is no semantic keyword inference: only these two fields decide the
result. `resolve` rejects a non-list `worker_guidance`, a non-string or unknown
or duplicate entry, a missing/malformed assessment, an unknown `by`, and a
blank `reason`. Distinct known names are rendered in canonical order
regardless of input order.

## Sections

Canonical order: `rendered_artifacts`, `archived_evidence`, `model_isolation`,
`live_serialization`. `rendered_artifacts` carries the frozen-input export,
overlay/interpolation, image-frame and renderer-resource checks;
`archived_evidence` the hash-bound input capture, frozen historical-evidence
and cross-file/state rules; `model_isolation` the default-deny model boundary
and reachable-fault rules; `live_serialization` the live producer-serialization
and serializer field rules.

## Resolved policy

`resolve(task)` returns a JSON-safe version 1 policy:

```json
{
  "version": 1,
  "sections": ["model_isolation"],
  "content_sha256": {"model_isolation": "<sha256 of the section text>"},
  "assessment": {"by": "astra", "reason": "bounded slice"}
}
```

`text(policy, section)` returns the selected section text, or `""` when the
section was not selected (or is unknown). The SHA-256 values freeze the content
identity of what was selected.

## prepare integration

New preparation resolves the policy before source capture and records the same
`worker_guidance_policy` in `task.json`, in `metadata.json` and in the prompt
payload, rendering the selected sections into the worker prompt. Only new
preparations use this catalog. Existing runs are never rewritten and their
saved prompt is never compared against future catalog text at start or resume.
There is no new runtime or finalize gate.

## Communication report

Every new worker prompt supplies `worker_handoff_template`, a concrete unfilled
scaffold: one status line (`Status DONE`, `Status DONE_WITH_CONCERNS`,
`Status NEEDS_CONTEXT` or `Status BLOCKED`), a shared evidence block, the complete
acceptance ledger, then pending jobs, scratch disposition and deviations/concerns.
Fill it as evidence arrives and return it once after inspecting complete results.
Add narrative only for material information absent from these blocks.

The scaffold indexes existing acceptance values as A1/A2 and declared checks as
T1/T2 without copying their prose or command strings. E1/E2 entries carry actual
environment, exit, counts, log locations and evidence kind/actor; rows reference
them with the relevant case/boundary and implementation location. Add distinct
entries for different invocations, retaining earlier failed attempts. Split
compound rows into suffixed rows wherever clauses, evidence or blockers differ;
the index does not interpret their semantics. Missing or empty acceptance stays
an explicit NOT VERIFIED scope gap. Placeholders never constitute evidence.
Conciseness never permits dropping a clause, fault branch, failed/unverified
check, pending job, scratch disposition or constraint deviation. Status words
are human communication only, never machine acceptance states.

A blocked report names the missing fact, attempted checks, available evidence
and help needed. The existing worker-result captures the final text; no extra
report files are required. Inspect complete current test results, then hand off;
avoid optional warning polishing or rerunning an unchanged passing candidate.
Required correctness/constraint repairs still need affected tests. Partial final
text and passing tests cannot substitute for a terminal event or review. The
scaffold adds no parser, automatic success gate, report file, length cap or timer.
It reduces report assembly and duplication; it cannot guarantee a model's latency.

T# identifies a declared check, not a tool invocation. A check can run zero, one,
or several times. For each actual invocation give the tool-call ID (or explicitly
unknown), its own log directory, result and actor; retain RED, retries and
prevented requests separately. For a known target-only check give `not_run`,
`NOT VERIFIED locally` and its named coordinator, without inventing an exit code
or log. E# scaffold slots are placeholders, not evidence of that many Bash calls.

The coordinator reconciles any invocation totals against
`worker-result.json.command_audit.bash_calls`, actual tool-start/result identities
and the full `process-evidence` binding; pre-execution denials and async job
generations have their own records. Do not add these counts blindly: a denied
request is not an executed test, a read is not Bash, and a repeated T1 is another
invocation. When a count or result cannot be established, leave it unknown.
Preserve the original worker report and append a discrepancy/correction for Sol;
never rewrite the report to match the audit. This is evidence-backed review,
not an automatic natural-language report validator or a new acceptance gate.

`scripts/evidence_tools.py invocations --run /absolute/run`
([ledger](evidence-tools.md#invocation-ledger)) is the generated bound evidence
for that reconciliation: it joins the saved request, gate and terminal records
by tool-call ID in tool-journal start order (then gate-only decisions, then
end-only rows; cross-journal chronology stays `unknown`) and reports the
retained numeric exit, log binding, duplicates, orphans, conflicts, denials,
unsupported async shapes and per-row `issues`/`binding_complete`. Execution is
tri-state — `executed` is true only for a fully bound allowed request whose
frozen command matched, false only for a coherent gate-only denial, otherwise
`unknown`; `invocations` counts explicit true rows only and `observed_starts`
reports the retained starts separately. `command_sha256` hashes the exact
recorded command bytes while `policy_command_sha256` hashes the existing native
strip normalization compared against the frozen allowlist (`command_source`
names which record supplied the command). Route the coordinator's final report
through that table — preserve the original worker report, append corrections and
unknowns, keep invocation counts and RED/GREEN order bound to it, and state the
process/binding result separately from acceptance. The table never reads the
worker narrative and never certifies acceptance, target identity, liveness or
test success; a complete binding does not mean the tests passed or were
meaningful, and a later GREEN never retires an earlier RED.

## Limits

New tasks that omit `worker_guidance` keep every section; already prepared
runs retain their saved prompt bytes. Agent behavior is not guaranteed by
this compatibility rule. The helper does not parse the goal text, does not enforce section
relevance, and does not add machine-state aliases. It transports the resolved
policy as data; the coordinator/reviewer remains responsible for judging
whether a selection was appropriate.
