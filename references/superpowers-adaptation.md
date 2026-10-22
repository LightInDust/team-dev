# Superpowers practices adapted to team-dev

This adaptation draws on [obra/superpowers](https://github.com/obra/superpowers/tree/bb92a77741419a4ab5f06e711a283343f1ada0c3), pinned at commit `bb92a77741419a4ab5f06e711a283343f1ada0c3`. The upstream skills are optional reference material; installing all of them or their plugin hooks is not required by team-dev.

The guidance below is scoped to team-dev's existing roles and gates. Documentation review and isolated synthetic checks do not establish improved stability across future tasks. Attribute runtime claims to the actual candidate, task and verification evidence.

## Apply within the existing workflow

### 1. Give each worker a small, self-contained handoff

Use the existing prepared task and selected context as the handoff. State the slice's observable outcome, relevant real interfaces, allowed files, exact prepared test invocation and known environment limits. Include the source needed to reason about those interfaces; a short prompt is not a reason to omit dependencies. Avoid replaying the coordinator's entire history or copying every unrelated reference into the worker prompt.

OMP workers keep `--no-skills`: installing a Codex skill does not inject it into DeepSeek. Use [worker guidance selection](worker-guidance.md) to select explicit domain sections before new preparation; keep common constraints and the original acceptance contract. Omitted selection retains all sections. Saved selection/content identity documents what was prepared; it is not an automatic proof that the coordinator chose the right context, nor a reason to rewrite a running task.

Before preparation, inspect shared files and producer/consumer dependencies between planned slices. Sequence overlapping changes from the latest baseline. A slice that needs an unresolved architectural decision should return the missing decision to Astra, not expand its own scope. DS remains the implementation worker; it does not dispatch nested reviewers.

### 2. Make worker reports actionable without changing machine states

New prepared prompts require a short human-readable summary beginning with `Status DONE`, `Status DONE_WITH_CONCERNS`, `Status NEEDS_CONTEXT`, or `Status BLOCKED`, followed immediately by the complete compact acceptance ledger, before optional narrative. Share command/result/log references instead of repeating passing-case lists. Preserve the concrete missing fact or concern, changed paths, actual test commands/results, scratch/deviations and pending tool/job. The existing `worker-result.json` captures the final text; do not require a separate worker report file outside its allowed paths. These words are communication cues only: do not add them to helper JSON schemas or translate `DONE` into verified acceptance. Old prepared prompts keep their original bytes and requirements.

Read missing or truncated evidence at its recorded path before rerunning work. For `NEEDS_CONTEXT`, supply the specific missing context through the already-permitted recovery route. For `BLOCKED`, distinguish provider/transport failure, absent runtime, and task ambiguity; increasing model effort does not resolve a rejected API request.

### 3. Recover from evidence instead of repeating completed slices

For work spanning multiple slices or context compactions, reuse the existing acceptance ledger. If it lacks a recovery index, add a compact coordinator-owned table alongside it: slice, run path, current candidate fingerprint, target-test receipt, review receipt, open finding, next action. Keep this outside frozen task metadata and worker snapshots unless explicitly included in their file scope.

The [evidence tools](evidence-tools.md) can generate this view from explicit run paths and prepare indexed review material. Their recorded process/delivery values are not fresh liveness or target checks. A recorded PASS, suggested next inspection or matching stored fingerprints never authorizes automatic skipping, restarting or acceptance. Check the underlying evidence and current target before acting.

On recovery, reconcile the table with current target identity, helper status and recorded artifacts. The table is an index, not an authority that can turn a failed or unverified run into success. Do not redispatch an accepted unchanged slice merely because conversation context was lost. Preserve failed runs and point to separately accepted takeover evidence where applicable.

### 4. Use the existing Sol review for two clear questions

Provide a bounded review handoff: original requirements, relevant interface contracts, current diff/fingerprint, target test evidence, process evidence and known gaps. Ask the existing independent Sol reviewer to distinguish requirement compliance from code quality in its narrative, while retaining the established review JSON schema and provenance binding.

The reviewer verifies worker claims against code and evidence. It may inspect unchanged producers, consumers and call sites when needed; a diff-only reading rule must not hide integration defects. Additional review agents or a separate mandatory final branch review are not introduced by this adaptation. Astra still checks end-to-end acceptance across the task's slices.

### 5. Keep repair and re-review focused on the finding

Check each review finding against the actual requirement and code before changing implementation. Give a repair run the confirmed finding, relevant source, acceptance obligation and exact declared tests. Prepare it from the current target using team-dev's existing recovery and fix-round limits.

Re-review the fix and its affected interfaces, retaining unresolved findings from earlier reviews. Reuse readable evidence only while its candidate binding remains valid. Candidate changes still require the target tests and review bindings required by team-dev; scoped review is not permission to skip those gates. Do not rerun broad checks solely because a log was not yet read, but do not substitute stale results for current acceptance.

Do not copy the upstream assumption that every finding outside the fix diff is non-blocking because a final whole-branch review will follow. team-dev adds no such mandatory stage. A concrete defect in unchanged code that breaks the current acceptance contract still blocks that contract; inspect its relevant producer, consumer or caller. Unrelated improvements can be recorded separately without extending this repair.

## Runtime and workflow adaptation

team-dev is the active orchestrator when requested. Use upstream planning, debugging and test-design practices within its existing roles and gates; do not start the SDD task-start/task-done/branch-finishing pipeline in parallel. A reviewer inspects the assigned evidence itself and reports blockers to the named lifecycle owner; it does not dispatch replacements or control sibling agents.

Use the actual dispatch schema. In the current host, explicit model/effort overrides require `fork_turns: "none"`; the upstream statement about overrides on a full-history fork does not apply here. The user's Sol max route stays explicit, and runtime identity remains unknown when not exposed. Upstream wait durations likewise do not override host wait bounds or required progress updates; wait for events within those bounds rather than polling unchanged state.

Test-first development uses the task's real framework and authorized exact commands. Upstream instructions to run a broader suite, commit, remove a workspace or ask for new global settings do not expand this task's permissions. Missing required test coverage must be reported or addressed through an authorized fresh task, never hidden by declaring success. Keep the user's chosen source/target workflow and existing recovery allowance.

## Boundaries retained from team-dev

- Preserve Astra coordination, OMP/DeepSeek implementation and independent Sol max review. Do not infer new provider/model routes from upstream examples.
- Keep existing recovery allowances, deadlines, target-environment testing, snapshot protection and fail-closed completion. Do not import Superpowers' five-fix-round budget or use a coordinator ruling to waive unmet requirements.
- Do not import automatic commits, branch finishing, workspace deletion, blanket full-suite reruns or extra reviewer stages. Use the current task authorization and team-dev lifecycle.
- Use the actual tool schema for dispatch and lifecycle behavior. Upstream Codex examples are guidance, not authority over the host's supported arguments, model allowlist or user instructions.
- For a request explicitly limited to installation or study without tests, deliver the files and findings and state that execution is unverified. Do not start a worker or run tests to satisfy an upstream workflow by accident.

## Source map

- [Subagent-driven development](https://github.com/obra/superpowers/blob/bb92a77741419a4ab5f06e711a283343f1ada0c3/skills/subagent-driven-development/SKILL.md): task isolation, task-owned progress and recovery.
- [Implementer handoff](https://github.com/obra/superpowers/blob/bb92a77741419a4ab5f06e711a283343f1ada0c3/skills/subagent-driven-development/implementer-prompt.md): focused context, escalation and short reports.
- [Task reviewer handoff](https://github.com/obra/superpowers/blob/bb92a77741419a4ab5f06e711a283343f1ada0c3/skills/subagent-driven-development/task-reviewer-prompt.md): separate compliance/quality questions, evidence-based review and scoped checks.
- [Receiving review](https://github.com/obra/superpowers/blob/bb92a77741419a4ab5f06e711a283343f1ada0c3/skills/receiving-code-review/SKILL.md): verify a finding before applying it.
- [Verification before completion](https://github.com/obra/superpowers/blob/bb92a77741419a4ab5f06e711a283343f1ada0c3/skills/verification-before-completion/SKILL.md): distinguish implementation claims from observed evidence.

The older OMP runtime findings in [reuse research](reuse-research.md) remain separate evidence. Attribute new runtime checks to their exact candidate, task and receipts; do not infer performance improvements or retroactively change historical outcomes.
