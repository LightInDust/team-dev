# Native reviewer identity and dispatch evidence

Keep these questions separate: which model/effort was requested, which explicit request the parent dispatched and the tool accepted, what the reviewer says about itself, and whether the runtime exposed the actual serving model. Generic system/developer text such as “agent based on GPT-6”, a role named Sol, a preferred default, or a task title cannot establish an exact SKU or effort. Do not infer `gpt-6-sol` or `gpt-5.6-sol` from that text.

The current collaboration tool accepts an explicit model/effort request and returns an agent identity. That is parent-side dispatch evidence. Its returned agent name is not runtime model metadata. This schema version has no runtime identity adapter, so `actual_verified_model` and `actual_verified_effort` must be the literal `unknown`, even when the requested route is fully evidenced. Report “explicitly dispatched as gpt-5.6-sol / max; actual serving identity not exposed”, rather than claiming backend verification. If authoritative runtime metadata becomes available, preserve it in a separate addendum and update the supported protocol deliberately; never backfill old receipts from inference.

## Bounded review and receipt closeout

Keep one independent review stage. In its handoff, Astra names the frozen candidate/manifest, the finite remaining checklist, the exact review output path and the evidence already accepted for unchanged scope. Bind reused CORE, numeric and test conclusions to their original review/log hashes and relevant source/input identity; distinguish reused evidence from newly inspected work. A later dependency/source change or concrete contradictory finding reopens the affected conclusion. Do not repeat an unchanged accepted calculation merely because artifact closeout is delayed. A reported-complete calculation without inspectable evidence is not a reusable PASS.

Name one lifecycle coordinator by exact agent ID in every review/helper handoff, normally the parent Astra. Only that owner requests recovery steering, interrupts, restarts, reassigns or dispatches replacements for this review and its helpers. Reviewers and visual helpers may inspect status/evidence and report a blocker or recovery request to the owner; they must not independently control siblings or spawn a replacement. Independent judgment of code and evidence remains the reviewer's job. This is a coordination instruction, not a runtime lock or enforced tool permission.

Before recovery, the owner checks the latest status, receipt and in-flight tool/write state, then serializes the action and reports its observed result to affected helpers. Transfer ownership only through an explicit acknowledged handoff identifying the new owner and pending work; the old owner relinquishes control before the new owner acts. Silence, a role label or a delayed message does not transfer ownership. If ownership is unclear, report the blocker to the parent and retain a pending receipt rather than starting a second recovery loop. This grants no permission to message unrelated user chats.

Derive the artifact checklist from the actual contract: selected source/tests, manifests, report/result consistency and required saved visual inspections. Name which items are sampled and why the remaining evidence is sufficient; a sample does not silently replace required full coverage. A twelve-file case is not a universal cap. New concrete findings permit targeted expansion with its reason recorded; missing required provenance, scientific or visual evidence remains a blocker rather than being excluded to meet the checklist.

Progress can state `inspecting`, `finding`, `ready to write`, or `saved`, with inspected items, remaining checks and blockers rather than private reasoning. Once each assigned item has an evidence-backed result and the required review fields are available, write the review at the named path before optional commentary or further exploration. Preserve every finding, unresolved requirement and limit; use the existing supported non-PASS verdict when required acceptance is incomplete. Return the actual saved path/hash. A ready-to-write message, silence or absent findings is not a saved PASS.

The named owner chooses a scope-proportional checkpoint in the existing handoff, including closeout and any assigned visual helper; no shared numeric timeout or automatic renewal is introduced. Each assignee reports inspected items, remaining checks and blockers by that checkpoint, or saves its scoped receipt. If no receipt or usable update arrives, only the owner requests one compact status and narrows the remaining work. At most one bounded same-assignee closeout recovery should follow for this incident. If it still fails, the owner preserves the original dispatch, partial evidence and observed interruption outcome, stops that assignee and inspects in-flight tool/write state before conflicting work. One fresh minimal-context replacement may finish the remaining checklist from the same frozen evidence, using a distinct output filename and its own actual dispatch; it must independently judge those items, not rubber-stamp a requested PASS. If that replacement also cannot complete, retain review-pending/blockers instead of an open-ended respawn loop. This is recovery of the existing review or assigned helper work, not a mandatory extra review. Record available checkpoint/interruption/receipt times without diagnosing model or network cause from delay alone. An artifact PASS never relabels a failed DS attempt.

## Parent-owned dispatch record

After the actual `collaboration.spawn_agent` call succeeds, copy the complete [dispatch template](../assets/reviewer-dispatch.template.json) into a new private JSON file under the [consuming evidence root](#choose-the-consuming-evidence-root). Fill `source.reference` from the visible parent tool exchange, `request` from the actually supplied routing fields, and `response.task_name` from the accepted response. Retain the template's `version`, `tool` and `source.kind`; reconstructing only request/response previously omitted these required fields. The blank template is deliberately invalid and is not dispatch evidence. Do not fill a planned response before the tool returns, ask the reviewer to invent dispatch, or copy secrets, unrelated messages or model reasoning.

The request may retain other actually supplied parameters. Save the original accepted agent identity exactly; do not fill a planned name before the tool returns. Save as a private JSON file in the helper run/evidence root, then calculate its SHA-256. The supported response is the tool's `{ "task_name": ... }` result, not an invented response containing a model.

When reusing an agent via `followup_task`, retain that agent's **original accepted spawn record** and mark `reused_agent: true`. A new instruction telling it “you are Sol” or asking its model is not new routing evidence. If the original record is unavailable, preserve the code review but mark routing unknown; a new explicitly routed native reviewer can perform bounded acceptance of the existing artifacts without repeating implementation or experiments. Do not relabel an old agent or overwrite an old review to match the preferred SKU.

## Choose the consuming evidence root

Choose the root from the operation that will consume the review, before saving dispatch or generating fields:

| Consumer | Root used to resolve `reviewer_provenance.dispatch_evidence.file` |
| --- | --- |
| Normal `team_dev.py record-review --run ...` and `finalize --run ...` | That exact helper run directory. Use it as `review_handoff.py template --evidence-root` too. The location of the JSON supplied through `--review` does not change this root. |
| Independent coordinator/takeover receipt | The dedicated directory supplied to `review_handoff.py receipt --evidence-root`. Generate fields and validate provenance against that same directory. This receipt does not finalize a helper run. |

A template generated successfully in an experiment or reporting directory only proves its dispatch is valid there. It does not copy the dispatch into a helper run or verify that normal `record-review` can find it. A matching basename in another directory is insufficient; the actual regular file in the consuming root must match the pinned SHA-256 and original accepted dispatch.

If the only defect is that the authentic dispatch exists in a different evidence directory, preserve the rejected invocation and original files. Verify the source bytes against the review's pinned digest and accepted parent dispatch, then place an identical regular-file copy at the expected basename in the consuming root when that destination is absent. If it already exists, verify it; do not overwrite different bytes or replace a symlink to bypass rejection. Validate provenance against the consuming root before retrying `record-review`, then follow the existing successful-exit gate before `finalize`.

When candidate, tests, process evidence, review judgment and dispatch bytes are unchanged, this placement correction does not require another model review, worker run or business experiment; retain the same review bytes and original failure. A hash/identity conflict, invalid record or changed acceptance evidence is a different defect and remains blocking under the existing correction/review rules. Missing-file placement is a coordinator evidence-location error, not evidence of a model, API or SSH failure. Keep the basename, size, symlink, hash and provenance checks intact; never weaken them to accept an external path.

## Review schema

For `review_handoff.py template`, pass the evidence directory separately from
the dispatch filename: `--evidence-root /owned/evidence --dispatch reviewer-dispatch.json`.
`--dispatch` takes a JSON basename inside that directory, not an absolute path
or a relative path containing directories. Do not strip an arbitrary path and
assume it identifies the same record: verify the existing file under the chosen
evidence root. Invalid paths remain rejected; correcting this syntax does not
establish a model-routing or backend failure.

New native reviews carry this object alongside verdict, candidate fingerprint, findings and the existing process binding:

```json
{
  "model": "gpt-5.6-sol",
  "effort": "max",
  "reviewer_provenance": {
    "version": 1,
    "requested_model": "gpt-5.6-sol",
    "requested_effort": "max",
    "agent_id": "/root/review_candidate",
    "reused_agent": false,
    "dispatch_evidence": {
      "file": "reviewer-dispatch.json",
      "sha256": "actual SHA-256 of the saved parent dispatch record"
    },
    "self_reported_model": "unknown",
    "actual_verified_model": "unknown",
    "actual_verified_effort": "unknown",
    "verification_basis": "parent_dispatch_only"
  }
}
```

The top-level `model`/`effort` are compatibility aliases for the **requested** route in this schema, not actual-runtime claims. `self_reported_model` preserves an actual self-report, or `unknown` when absent; it cannot satisfy the dispatch requirement. If an earlier reviewer self-reported a different SKU, preserve that statement as a self-report and explain the correction in findings/limits or a separate provenance addendum. Do not silently rewrite it. Candidate correctness and route provenance are separate claims.

If dispatch is unavailable, keep a non-acceptance note with `dispatch_evidence: null`, `verification_basis: "unknown"` and actual identity unknown. This is useful historical evidence but cannot satisfy the new PASS gate. Preserve the original receipt and bind a correction addendum to its hash; a correction of provenance alone does not repeat code review or establish the missing route.

## Normal worker review versus takeover receipt

The provenance object above and `review_handoff.py template` output are **metadata
only**, not a complete review template. Before dispatching a normal worker review
for `team_dev.py record-review`, give the reviewer these exact fields and sources:

| Field | Authoritative source / responsibility |
| --- | --- |
| Top-level `fingerprint` | `applied.json.fingerprint`; it must agree with `metadata.json.applied_fingerprint` and the current target-test `tests.json.fingerprint`. Supply the actual value, never a placeholder. |
| `model`, `effort`, full `reviewer_provenance` | Validated `review_handoff.py template` output from the actual accepted dispatch. |
| `verdict`, findings and limits | The reviewer's independent judgment; metadata generation supplies no PASS. |
| Full `process_review.binding` and its assessment | The complete current `process-evidence` object and review of actual commands/results, including prevented requests and pending/resource outcomes. Follow the existing process-review schema; a scalar subset is invalid. |

For a run with enabled process review, fill the following slots from the current
binding before returning a PASS. `scripts/process_evidence.py:validate_review`
is the executable schema; the metadata-only generator does not fill these slots.

| Evidence predicate | Required `process_review` slot and reviewer responsibility |
| --- | --- |
| Process review is enabled | `verdict`, full `binding`, and nonempty `reason`; a PASS requires an independently justified `verdict: "pass"`. |
| `binding.preexec_gate.denied_requests` is nonzero | Nonempty `preexec_denial_resolution`: identify the prevented request, actual denial/result and later behavior. Distinguish a pre-execution block from an executed violation. Prose in findings or `reason` does not replace this named field. |
| `binding.audit.status_counts.unsupported_syntax` or `over_limit` is nonzero | Nonempty `coverage_resolution`: inspect the actual command arguments/results outside automatic audit coverage. |
| `binding.audit.text_observation_counts.absolute_tmp_path_mention` is nonzero | Nonempty `temporary_path_resolution`: disposition actual owned/retained paths and resource evidence, without treating a text mention as proof of execution. |
| The frozen process policy requires async review (v2 and later) | `async_jobs`, including `[]` when none exist. For observed jobs supply the exact per-invocation records and bound lifecycle receipts from [async review](async-bash-lifecycle.md#ordinary-sol-review-with-per-job-evidence). |

An observed predicate selects a required field, not its conclusion. Empty text,
generic "resolved" placeholders and coordinator-written judgments do not replace
Sol's inspection. Nonempty `binding.blocked_requests`, malformed/incomplete
bindings and unresolved lifecycle evidence still block acceptance; adding a
resolution field cannot waive them. Preserve the frozen policy version of older
runs rather than adding or removing markers to change these requirements.

`candidate_fingerprint` and `candidate_manifest_sha256` may be supplementary
context but are not aliases for top-level `fingerprint`. `record-review` rejects
its absence or mismatch even when provenance is correct. Matching saved hashes
does not prove current target contents; keep the existing target checks and
`finalize` gates. If applied/test identity is missing or inconsistent, resolve
that gap rather than guessing a review value.

For an independently completed takeover, bind the actual final source manifest
and tests in the separate review/coordinator receipt. Do not invent a normal-run
`fingerprint` from that manifest hash or finalize a failed/rejected worker as the
new candidate. The receipt writer validates PASS/provenance only; the coordinator
still accepts the final source and tests.

For a schema-only mistake, retain the rejected review and helper diagnostics. If
candidate, tests, process evidence, findings and dispatch are unchanged, ask the
same reviewer to save a separately named correction with the exact missing field
and a note binding the earlier review. That does not require a new reviewer,
recalculation or test rerun. Changed evidence or a concrete contradiction still
reopens affected acceptance; never repair a semantic defect by relabeling fields.

## Exit-gated dependent steps

`record-review` must succeed before `finalize`; `finalize` must succeed before a
downstream assessment or publication whose task requires helper acceptance.
`await` serializes calls but does not test their exit status. A returned tool
object can contain a nonzero `exit_code` without throwing. Inspect each completed
result and its expected run/review binding before dispatching its dependent step;
do not place dependent calls in an unconditional loop or parallel batch.

For the current `exec_command` result shape, the coordinator can use this guard
with its already prepared argument objects:

```javascript
async function completedStep(args) {
  const result = await tools.exec_command(args);
  if (result.session_id != null || result.exit_code !== 0) {
    throw new Error("Dependent step withheld: inspect the prior result and saved evidence.");
  }
  return result;
}
await completedStep(recordReviewArgs);
await completedStep(finalizeArgs);
```

A running session or missing/unknown exit is pending, not success. Wait for that
same invocation's terminal result and inspect its evidence before continuing;
do not rerun it merely because the batch stopped. This snippet guards execution
order only and does not validate review semantics, receipt identity or permission
for a later calculation. If a prior batch already continued after rejection,
record what actually ran and its then-incomplete helper state. Keep later schema
correction/finalization separate; it cannot retroactively make the earlier gate
successful. Do not repeat completed scientific work to repair that chronology.

## Bind coordinator tests to the selected candidate

After copying a candidate or changing takeover directories, prepare each coordinator test invocation from the current absolute repository root and declared argv. Before execution, compare the invocation's actual `cwd`/resolved repository root, relevant HEAD when applicable, and source/test manifest with the candidate being accepted; do not reuse a previous command's directory by habit. Keep identity/hash inspection in the authorized coordinator workflow, not as extra Bash probes for a restricted worker.

Retain that candidate binding with the exact argv, environment, terminal exit, complete logs and hashes. Check for relevant source changes across execution before reusing the result. A test launched in another snapshot may describe that snapshot, but cannot verify the selected candidate even if it is green or the directory names look similar. Preserve the mistaken invocation as a coordinator error, exclude it from the selected candidate's acceptance, and run only the still-required check from the correct bound directory. Do not attribute that command's failures to the worker or overwrite the failed worker/takeover chronology.

For remote verification after tests have already passed, see [transport failure during finalization](task-contract.md#transport-failure-during-finalization) before retrying tests or changing routes.

## Exact fields in the reviewer handoff

Give the reviewer the exact schema above in its context, including top-level `model` and `effort`. Immediately after saving the completed dispatch template, use the existing command to validate it and generate concrete fields, so schema errors do not first appear at final closeout:

```text
python3 -B scripts/review_handoff.py template --evidence-root /absolute/helper-run --dispatch reviewer-dispatch.json
```

This example is for normal helper review: `/absolute/helper-run` is the same directory used by `record-review --run`. For an independent receipt, substitute its dedicated consuming evidence root as described above.

Send the returned JSON to that same reviewer in the existing review stage and ask it to copy these field names alongside its independently determined verdict, findings and candidate/process bindings. The generator validates the saved original dispatch, derives the accepted agent ID and file hash, and prints only the metadata (not the applied `fingerprint` or complete process review); it writes no file and supplies no PASS verdict. There is no need for another reviewer stage. For a reused agent, add `--reused-agent` and retain its original spawn evidence. Keep an existing actual self-report explicitly attributed rather than replacing history with a default `unknown` label.

Check the command's exit status before using its output; an empty redirected file after rejection is not a template or review. Keep the invalid dispatch and diagnostics, then save a separately named correction populated only from the same actual tool observation and validate again. A schema correction does not constitute a new dispatch or prove runtime identity. Do not weaken the validator or overwrite a rejected record to make closeout pass.

Do not substitute `requested_reasoning_effort`, `actual_model`, `agent`, `basis` or dispatch `path` for the supported fields. `reviewer_provenance.version` must be integer `1`; `dispatch_evidence.file` is a JSON basename, not an absolute path. A content PASS with an invalid provenance object remains blocked for closeout. Preserve it and request a separately named correction from the same reviewer with the original dispatch binding.

## Guarded coordinator receipt publication

For independent takeover closeout, use one Python operation to validate the exact review and publish a new receipt. Do not run the validator and then an unconditional shell command that writes completion: a newline or a successful last command can hide the earlier failure. The normal helper `record-review` / `finalize` gates are unchanged.

After Astra checks candidate hashes, target-test receipts, required acceptance and residuals, save those coordinator declarations in a new `completion-draft.json`. Then use the independently recorded SHA-256 of the intended review (replace `ACTUAL_REVIEW_SHA256`):

```text
python3 -B scripts/review_handoff.py receipt --evidence-root /absolute/evidence --review sol-review-v2.json --expected-review-sha256 ACTUAL_REVIEW_SHA256 --draft completion-draft.json --output completion-v2.json
```

All file arguments are local JSON basenames under the evidence root. This command requires the pinned review bytes, exact `verdict: pass`, and a successful direct call to the official provenance validator. It rechecks review/draft/dispatch bytes and validates provenance again immediately before atomic, exclusive publication. Schema errors, validation exceptions, non-PASS verdicts, stale hashes and changed inputs prevent publication; an existing output is never overwritten. A shell continuing after a failed invocation cannot make this writer emit a receipt. Inspect the writer's own exit status and returned receipt hash; after an I/O interruption, inspect output state before using a fresh name.

The output is a `team_dev_review_validated_receipt_v1` envelope: `review_validation` binds the review/dispatch and explicitly limits its scope to `review_verdict_and_dispatch_provenance_only`; `coordinator_payload` preserves the draft as coordinator declarations. The writer does **not** interpret those declarations, validate candidate or test hashes, certify scientific/field acceptance, prove serving identity, or make a failed DS run finalizable. Do not infer full completion from this narrower gate alone. It is a guarded publication step after Astra's acceptance work, not an additional semantic validator. Original failures, invalid reviews, withdrawn receipts and later correction receipts remain separate. As with other local hashes, this is cooperative evidence rather than a security boundary against concurrent same-user edits.

## Validation and compatibility

New `prepare` calls freeze `reviewer_provenance_policy` v1 in task and metadata. `record-review` requires this schema and an intact accepted dispatch record before recording PASS; `finalize` checks them again. Changed/missing evidence cannot leave a previously verified run marked verified after a finalization attempt. Existing runs without the marker keep their previous helper schema; never edit markers to upgrade/downgrade an old run. A legacy PASS is not retroactive proof of model routing.

For independent takeover receipts, the same read-only validator can check local structure and integrity:

```text
python3 -B scripts/reviewer_provenance.py --evidence-root /absolute/evidence --review /absolute/sol-review.json
```

This validates matching requested model/effort, original spawn request/accepted agent identity, an unchanged local dispatch file hash, and explicit unknown runtime identity. A direct follow-up call, generic identity text, self-label alone, wrong model/effort or mismatched agent cannot substitute. No model call, backend query, new approval or extra review stage is introduced.

These are cooperative consistency checks. A same-user editable JSON extract and hash cannot authenticate the tool transcript or prove which backend served the call; Astra must preserve the real tool exchange and connect the returned review to that agent. Do not call synthetic unit-test records real provenance. Full original tool history is not automatically scraped or broadly searched by the validator.
