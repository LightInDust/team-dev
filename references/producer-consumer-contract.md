# Producer-generated consumer fixtures

Read this for API/UI or other serialized component boundaries, including frozen historical evidence. A consumer and its hand-written fixture may agree on an invented field or invariant while the real producer has different semantics. Local branch fixtures remain useful, but cannot by themselves prove compatibility.

Choose the evidence mode first. For a live API/UI integration with authorized reproducible fixture generation, use the acceptance chain below. For frozen historical/scientific evidence, read the producing source and feed immutable archived bytes to the real consumer; do not run historical algorithms or import scripts with execution side effects just to regenerate fixtures. Follow [archived evidence semantics](#archived-evidence-semantics) instead. Explicit task constraints govern which generation, if any, is permitted.

## First executable interface check

Classify the boundary by what it calls or consumes, not whether its files are new. A new runner configuring an existing backend and a new renderer reading an existing producer are integration work. Select the actual selector/binding, producer and meaning-defining helpers in `context_paths`/`interface_contracts.source_paths`, and name the executable integration command. The helper checks declared fields, not whether Astra classified the work correctly. Written schema plus a self-consistent synthetic fixture cannot establish compatibility with an omitted producer.

### Coordinator context check before prepare

For a consumer of a changed export, reconcile these three items before freezing the handoff:

| Item | Evidence to select and check |
| --- | --- |
| Applicable producer | The producing source/serializer and meaning-defining helpers for the selected artifact version, with their source identity. For a new export, an older similarly named example is insufficient; for a historical archive, do not silently substitute a newer incompatible producer. |
| Representative serialized input | A small producer-generated or hash-bound archived sample with nonempty instances of every new or changed representation the consumer must handle. Inspect actual nesting, primitive/object types, required keys, cardinalities and identifier/index semantics. Record the sample's origin and any authorized record-selection rule; preserve retained record structure instead of rewriting it to fit a guessed schema. |
| First executable boundary | An existing declared command that feeds that sample through the real consumer entrypoint and checks independently specified values/identity links. A sample that omits the changed field cannot establish compatibility with its nonempty form. Keep branch fixtures, but do not count their agreement with the consumer as this check. |

Put the producer and representative sample in the existing `context_paths` and relevant `interface_contracts.source_paths`; include fixture/test destinations in `allowed_paths` when the task requires retained integration coverage. The helper checks selected-file presence, not whether a source is authoritative or a sample represents the needed fields. Astra checks these facts before dispatch, and Sol checks the same bindings during ordinary review. If a required representation is absent, report the precise missing source/sample and keep integration pending; use the existing bounded context-recovery route rather than inventing it or starting another unchecked implementation.

For example, a list of records containing a retained global index and a coordinate pair is a different interface from a list of coordinate pairs alone. Do not replace an explicit identifier with its list position or make an omitted field optional without source/contract support. For archived evidence, this context check permits source reading and authorized immutable slicing only; it does not authorize importing a historical producer, rerunning inference or regenerating an experiment. If a minimal authentic sample cannot be supplied within the task's constraints, retain that blocker.

### Exact-export adapters over permissive helpers

When a new adapter requires a complete export from a particular producer/version, inspect the reused parser's accepted domain as well as its successful output. A legacy helper may intentionally accept missing diagnostics or several formats. Reusing it does not enforce the adapter's narrower contract: require the adapter's fields before delegation, while preserving the helper's legitimate callers and the accepted scoring/processing semantics.

Resolve these distinctions from the applicable producer and task contract, not from one successful sample:

| Contract aspect | Adapter and regression check |
| --- | --- |
| Presence, nullability and contents | Distinguish an absent key, explicit `null`, an empty list and a populated list. Require present non-null lists only where specified; retain legitimate optional/null states and zero-count empty lists. Keep cardinality, identifiers and cross-record links checked after presence/type validation. |
| Producer envelope | Check the required version/variant and configuration fields that give the payload its meaning, including any required record-to-plan consistency. Distinguish frozen constants, values selected by an independently supplied plan, and legitimately variable fields. Validate required mapping types/keys and source or weight identities to the extent specified; matching only a schema label or mutually agreeing record/plan values cannot establish a separately frozen configuration. Do not hard-code every sample value or impose another project's parameter set. |

Start rejection cases from a copied, provenance-bound real sample whose unchanged form passes the actual public consumer. Within the existing declared tests, remove each required field or set it to `null` separately, and change the relevant required envelope value while preserving unrelated valid structure. Assert the specified rejection at the public analysis/consumer entrypoint and, when persistence is in scope, that its public write path cannot publish a successful result from that input. A preceding unrelated fixture error is not evidence for the intended gate. Preserve the archived bytes and accepted algorithms; do not regenerate evidence or import an executing producer to obtain these cases.

Keep asymmetric synthetic math/branch fixtures, but make their envelopes conform to the selected producer contract; document any explicitly scoped substitution. Do not strip required metadata to simplify a fixture or infer optionality from a permissive helper. A valid real-sample happy path alone does not demonstrate missing/null or configuration-drift rejection. Apply only the distinctions this adapter requires, in the existing integration test and ordinary Sol review, rather than adding a universal schema gate or exhaustive mutation suite.

### Exercise the selected interface

Before broader validation or report scaffolding, identify one small check that can disprove the core interface. Use the existing declared test command and task-authorized execution only:

| Boundary | First useful check | Independent expectation |
| --- | --- | --- |
| Backend construction/binding | Execute the real role-to-selector-to-resource binding with distinct fixture resources; mock only authorized external loading/inference boundaries | Explicit expected binding for each role; a swapped/missing resource or wrong selector fails. Mocking the selector itself misses this defect. |
| Coordinates and aggregation | Feed actual producer output, or hash-bound archived bytes when generation/import is forbidden, through the real consumer | Literal asymmetric coordinates/counts derived from declared units and source semantics. Original pixels stay original pixels unless an explicit crop/display transform is required. Do not derive expected values by calling the consumer conversion under test. |
| Export identity and rejection | Exercise the public export with a required metadata value and a controlled input change at an actual read/render boundary | Required viewer text is present and input drift cannot publish a success receipt; a helper-only check or receipt field is insufficient. |

For coordinate-changing boundaries, put the actual frame, units, origin/scale and
conversion stage in the existing interface contract: producer output, archived
baseline, restored result and summary/metric input. A name such as
`normalized_baseline` does not prove the stored pose is still normalized. Trace
whether the producer has already restored it before applying another transform
or comparing it with another record.

Use one small task-authorized fixture that distinguishes those stages: a nonzero
origin, nonunit scale and asymmetric values (plus a nontrivial rotation where
relevant). Derive the injected external-solver output independently from the
stated mathematical/source contract, and compare the actual consumer output
with independently fixed expectations in the destination frame. Do not feed a
WORLD pose as a normalized solver result, or call the restoration/conversion
under test to manufacture its own expected answer. For a summary requiring WORLD
poses, include a targeted sensitivity check that substituting pre-restoration
data for restored data fails. Preserve required raw outputs separately. Reuse the
existing declared test command and Sol review; this is a specific cross-frame
check, not a new universal test matrix or permission to rerun archived solvers.

Role names such as `extractor`/`matcher` need not equal selector keys such as a named backend. Trace the actual call chain, including constructor defaults and configuration, rather than assuming same-looking names are interchangeable. An offline binding check does not certify GPU construction or inference. Required target-only behavior remains NOT VERIFIED until the named authorized target check runs; never enable real model calls, heavy imports or downloads merely to obtain early feedback. Use the existing model-isolation guard where required.

When the contract names a helper, pin its module path and symbol in the existing interface/context fields and inspect the consumer's actual import/call. A report copy of a producer's `_decide` can have a different tie tolerance or reason vocabulary despite the same name. Reuse the specified authorized helper with a small shape adapter; do not replace it with the most convenient near-equivalent. Test a distinguishing boundary such as exact tie versus a tiny valid nonzero margin using independently derived expected behavior, alongside the required ordinary cases. If direct import is forbidden, preserve that restriction and use only the task-authorized source/fragment route; missing permission or context is a blocker, not permission to substitute semantics.

If the producer or permitted evidence is not yet available, either prepare after the dependency is ready or explicitly scope a provisional consumer unit layer while retaining integration as pending acceptance. The worker cannot silently narrow an already-prepared contract. For an immutable archive, source reading and archived-byte consumption take precedence over fixture regeneration; a copied producer algorithm is not independent evidence.

For cross-platform tasks, derive assertions from the target capability and contract. A macOS observation that a process field is unavailable is not a Linux invariant. Test required identity semantics and permitted optional fields on their applicable platforms; preserve actual target results and keep a narrowly corrected portability test separate from the original failed run.

In the existing `interface_contracts.inputs`/`returns`, `source_paths` and constraints, distinguish return value, in-place mutation and ownership: an API can mutate its argument and return `None`, so assigning that return can discard valid state. Trace the actual selected implementation and configuration provider (including defaults) through the real caller; a pinned file with the right name is not evidence that this invocation consumed it. Preserve original arguments, return/exception behavior and invocation count. Where the contract requires verified identity before delegation, unavailable identity remains NOT VERIFIED and must take the specified rejection path, not silently allow execution. Keep that runtime requirement scoped to its actual owner; do not impose CLI/OS identity checks on imports or pure APIs. When source identity requires several fields, compare each required field (for example resolved path, size and digest) against the independently pinned values; matching bytes alone do not prove the same source. Trace the actual selected owner rather than accepting the first same-named candidate. Distinguish aliases of the same resolved source from conflicting distinct owners according to the contract; do not invent a universal identity schema.

## Observer neutrality, callbacks and strict JSON

Apply these clauses only when the actual contract requires them; do not generalize one project's observer, callback, CPU, rollback or overhead rule into a universal invariant, and do not import another project's thresholds or case names.

- **No additional work when disabled.** When a contract explicitly requires observer-neutrality (the disabled/absent observer adds no work), trace diagnostic-only bookkeeping in source and trap its construction/copy/helper seams on the real disabled runtime path; verify absence of the forbidden work, not merely equal outputs or noisy timing measurements. Equal outputs can hide work the contract forbids.
- **Real consumer callbacks.** Exercise the existing production consumer path, not a re-implementation or copied helper. Assert the callback at the point the contract specifies: exact invocation count, the original error/partial-event sequence, and the resulting post-state — including routes where the contract requires no callback. Capturing events with `dict.update` and emitting them afterwards is not the same failure boundary when the contract requires emitting inside the callback, and a rejected update can retain earlier sample mutations; assert the callback's own point and post-state. Follow the reached-branch and independent-oracle rules in [public acceptance](public-acceptance.md#contract-requirements-at-the-public-acceptance-boundary).
- **Strict JSON and diagnostics.** Where the contract defines a nonfinite or diagnostic conversion (for example NaN/Infinity handling for serialization), use the contract-defined null plus explicit value status for measured nonfinite values, and distinguish unexecuted/unmeasured fields as specified. Keep algorithm inputs and branch conditions unchanged; sanitizing a diagnostic must not alter scientific values consumed downstream.

## Acceptance chain

1. Identify the actual consumer-facing serialization entrypoint, not a copied dictionary or a mock of the project service. In an isolated fixture, call real project components with temporary storage; mock only external process/network/robot boundaries. Seed meaningful cases from the business requirements, including empty and error/cancel paths when applicable.
2. Export the bytes delivered to the consumer. Preserve field names, types, units and nesting. Document normalization of nondeterministic timestamps, temporary paths or tokens; review every normalized field. Do not use normalization to repair contract mismatches. If a response envelope is unwrapped, use the real client's parsing path or test that adapter too.
3. In one acceptance command, generate into `TemporaryDirectory`, compare against any committed fixture, then feed the newly generated data into the real consumer. Assert visible state, confirmation availability and emitted requests as applicable. A checked-in fixture must never be silently refreshed by acceptance tests; regeneration differences fail until reviewed.
4. Cover missing keys, wrong types and mistaken names. For a strict adapter over a permissive parser, apply the [exact-export checks](#exact-export-adapters-over-permissive-helpers). Validate that the integration test detects an isolated consumer-field rename and a stale committed fixture. These mutations belong in disposable copies, never in live source or a frozen candidate. They demonstrate sensitivity to those faults, not universal schema correctness.
5. Select the producer, generator, normalization code, consumer, tests and any committed fixture into the same task snapshot/fingerprint. Record the exact generation/consumer argv, source manifest and generated-fixture hash with the target test logs. Bind them to the reviewed candidate; reject source changes during testing. For differing backend/frontend hosts, transport the immutable generated bytes with their hash and retain both environments' receipts.

Use the existing `test_commands` and `integration_test_commands` for the project's combined check, not the demonstration below. Put observable assertions in `acceptance`; specify the serializer entrypoint and normalization in `constraints`/`interface_contracts`. Source hashing binds files, not semantic provenance: Sol must trace the call chain and confirm the consumer reads the generated data. No metadata field or self-reported PASS replaces that inspection.

If dependencies are unavailable locally, report NOT VERIFIED and run the original check in an existing target test environment. Do not install fake packages or substitute a hand-typed JSON result. Do not exercise production motion, restarts or map operations to generate a fixture.

## User-visible scope

Translate UI requirements into assertions about the whole expected content set and its actual visible boundary, not the mere existence of a tag/control. For example, “context is collapsed by default” requires every expected contact-sheet image/link and context link to be inside the intended same closed `details` container. Check expected identities and counts, including duplicates, and reject matching content outside that container. One closed `details` tag does not prove that sibling images are hidden; counts alone can hide a missing item replaced by a duplicate. Use the project's actual expected paths/counts, not hard-coded counts from a prior task.

Exercise the generated consumer output, initial closed state and expanded state as required by the contract. Inspect rendered behavior when CSS/scripts can override structural assumptions. A scoped regression should fail if a required image/link is moved outside the container or duplicated outside it, while accepting the complete correct set inside. These are targeted sensitivity checks in disposable fixtures, not a reason to rerun accepted scientific producers or exports. Reuse already bound business evidence when it closes the question; retain the failed earlier candidate separately.

## Archived evidence semantics

Trace each consumed field through the actual serializer/caller and the helpers, constants and configuration that determine its meaning. Include these selected sources in the task context/fingerprint; a top-level producer alone may hide a conversion that returns `None`. If a necessary dependency was omitted, report the proposed invariant as unverified and obtain the missing context for a new bounded task before enforcing it. Do not infer a two-way equivalence from a field name, an observed correlation or the current capture containing no counterexample.

For result pipelines, distinguish these layers when relevant to the project's contract:

| Layer | Evidence supports | Does not by itself establish |
| --- | --- | --- |
| Raw result exists | An actual raw result record exists, or a declared count is verified against the raw records and is greater than zero; count-field presence alone is metadata | The required conversion or usable transform exists |
| Usable transform/result produced | The producer's named conversion succeeded under its documented prerequisites | A reference exists or the result can be scored |
| Scorable | Required reference, identity correspondence, units and metric inputs are available under the scoring contract | Accuracy, physical correctness or task acceptance |
| Scientifically/physically correct | Independent criteria and appropriate validation support that conclusion | Broader operational readiness without its own acceptance checks |

These are semantic distinctions, not universal Boolean formulas. For example, a helper can return `None` when grid assets are missing or a matrix is invalid. Then a nonempty raw result with `transform_produced=false` is a legitimate state. A documented producer may support `transform_produced=true ⇒ complete state and raw result_count > 0` without supporting the reverse implication. Equality of a declared raw output count and the actual raw result count is a different check. Derive each implication, nullability and count relationship from the actual frozen producer and task contract; review both accepted and rejected states.

### Preserve the consumed envelope and references

Before delegation, Astra checks a complete representative input for each consumed format against the actual producer and written contract. Distinguish the serialized input schema from the consumer's returned representation: returning a map keyed by `(method, frame_id, ki)` does not authorize inventing a colon-keyed input document when the real input contains nested records in `pairs`. Resolve contract/source disagreements and supply missing context before declaring the interface settled. Record coordinator context/contract defects separately from worker implementation defects.

For every proposed cross-file equality or accepted value set, name the contract/source that authorizes it. Descriptive fields such as `scope` may have different origins and need not be identity keys. Preserve their provenance and any authoritative frozen addendum; choose exported metadata explicitly. Require description equality only when the actual contract defines that relationship. Bind compatibility to the specified identities, types and inventory instead, retaining exact missing/extra/duplicate checks. An older example with matching descriptions cannot replace the intended consumer's input contract. A focused positive case should preserve a known-valid difference in descriptions while exercising the real public loader.

A task-specific allowed set can be narrower than a reused producer's enum. If the consumer accepts only one arm and must reject unexpected arms, reject every other arm, including recognized legacy values; silently filtering them changes the contract. Keep a complete valid case plus a public rejection of a recognized-but-forbidden value, asserting the required absence of a success receipt. Do not widen the set from legacy helper behavior or narrow it from one observed dataset. These checks consume owned fixtures or frozen bytes; they do not authorize regenerating scientific inputs.

A verbatim subtree proves only that subtree. Preserve the consumed outer keys, nesting, types, required provenance and semantic fields in the public regression. For example, a threshold `min_matches: 20` and a Boolean `would_pass` must not become a renamed count and an integer predicate merely to fit the implementation. Required `counts` or selected-source mappings cannot silently become optional through defaults. Use only the actual project's fields and meanings; the examples are not a universal schema.

Prefer unmodified complete archived documents for format-compatibility checks when practical. A bounded subset must retain the real envelope and references, with any aggregate adjustments derived from documented producer semantics and labelled as adaptations. Do not call an implementation-defined replacement wrapper, reconstructed camera or hand-built label file byte-identical archived input. Bind original and adapted hashes separately, preserving the originals. Exercise the actual public loader before claiming compatibility; accepting copied inner records with a fabricated envelope is insufficient.

Keep unknown values on an existing record, a permitted absent annotation, a missing required field and an orphan reference distinct. Preserve contract-permitted unknown labels and false flags; reject missing required relationships rather than treating them as unknown or silently dropping them. Add the smallest public fault cases for missing required containers, changed predicate types and unresolved references alongside legitimate unknown/unlabelled cases.

Path remapping or opaque placeholder bytes are allowed only where the task explicitly permits them and the substitution does not replace the schema/provenance being tested. For an input-only layer that transports but never decodes a PLY, a declared harmless placeholder with its own test-plan digest can avoid model execution; it does not validate real PLY content. Retain actual diagnostic/label/camera/producer bytes for their relevant acceptance claims. Missing complete input evidence remains NOT VERIFIED, not permission to regenerate experiments or invent a producer format.

### Hash integrity, independent oracles and fault completeness

For a contract that binds a result to consumed bytes, capture each input before its first use and calculate the digest over the exact bytes passed to the parser/decoder, or pass an owned immutable snapshot to a path-based loader. Read a manifest this way first, then resolve and capture its nested inputs before consuming them. A separate hash-before-read plus hash-after-read cannot prove the consumed bytes if a mutable path changes and reverts between checks. Capture the selected implementation and meaning dependencies before their authorized execution, and keep their initial identities; post-analysis source hashing cannot establish which version produced the result. A file digest alone also does not attest already-loaded code. Preserve a stable selected source snapshot or disclose that execution-identity limitation rather than re-importing forbidden historical scripts.

Immediately before publishing the contract's success receipt/metadata, recheck the complete captured set against those original digests, including declared source/dependencies and decoded image inputs. Do not silently refresh the baseline or check only the top-level plan/pairs while omitting other bound files. Retain the frozen hashes in the receipt. Use a small local mapping/loader at the existing public boundary; this does not require a general provenance framework or new workflow gate. Add bounded public fault cases for the bound input/source/image categories: mutate owned copies after the relevant read or analysis, reach the intended drift check and assert that no success metadata is published. Keep any partial output and original scientific evidence according to the task contract. These tests do not authorize worker Bash probes, extra commands or scientific reruns.

A pinned digest authenticates input bytes; it does not establish that a complete envelope satisfies the required schema or meaning. Enumerate required fields and types at every consumed level, then test rejection through the real public entrypoint. For a contract like D3, this includes plan `frame_id` (nonnegative integer, excluding Boolean), Boolean `config.do_pnp`, and each consumed log's `path`, `resolved`, `sha256` and `bytes`. Validate the actual specified types; do not require a Boolean flag to be true without a contract rule. A provenance path such as `resolved` can be validated as data without opening that historical/remote location. These names illustrate the observed contract, not a universal schema.

When the contract requires unique serialized label/identity keys, detect duplicate JSON object keys while decoding the captured bytes, before converting key/value pairs to a dictionary. Ordinary dictionary construction can silently keep the last value, so a later set/inventory check cannot establish serialized uniqueness. Use the real decoder's pairs hook or equivalent at the required object scope, checking decoded key equality (including escaped spellings) rather than matching source text. Build the negative fixture as raw JSON bytes containing repeated keys; a Python dictionary cannot represent that fault. Keep other preconditions valid and bind fixture digests to those bytes before invoking the public loader, so an earlier hash mismatch does not hide the duplicate-key check. Cover conflicting and identical duplicate values when both are forbidden, require the intended error and no success receipt, and retain a valid unique-key case. This decoding rule complements, rather than replaces, the same-consumed-byte binding above; neither reparsing another path nor rehashing after consumption establishes it.

Separate semantic-fault fixtures from integrity-drift tests. For a schema fault, change only disposable complete input copies and deliberately rebind their test fixture digests, so an earlier hash mismatch cannot hide missing/type validation. Assert the intended public input error and absence of normal output. For an integrity-drift test, keep the expected digest frozen. Preserve original archived bytes in both cases.

Apply each mutation after the fixture's final construction/write, then pass that same object or path into the real public entrypoint. Trace intervening wrappers: a helper that rebuilds the fixture can erase the mutation and make an equality or rejection test vacuous. Establish the changed value at the actual consumer read boundary using the consumed bytes/object, an existing diagnostic or a scoped observation that still calls the real loader. For an intentionally ignored field, require the changed field to reach that boundary and compare independently expected relevant outputs; an unchanged output alone does not prove the field was ignored. Keep this invariant case separate from an invalid-input rejection so the latter cannot mask it. If rebuilding is required, inject after that rebuild and preserve the same provenance/drift semantics.

Derive expected values from the frozen producer's raw records and independently supplied selection plan. Rehashing a matrix exported by the consumer under test checks internal consistency only. For stage/final transform exports, select the raw producer stage by the declared identity, take the original final matrix separately, and compare every selected output's matrices, top-level hashes and all required metric transform references against those independently derived expectations. A regression that emits another valid matrix and updates all its hashes consistently must fail. Reading archived matrices and canonicalizing their bytes does not require rerunning matching, registration, ICP or scientific scoring.

Cover missing and duplicate/ambiguous cases separately in the declared fault matrix, even when one implementation condition handles both. Removing a unique required stage/log marker is a distinct regression from adding a second marker. Each public fault test must reach its intended semantic guard and assert no accepted output; keep the complete positive fixture too. A later scoped exporter PASS establishes that reviewed layer, not scientific correctness or completion of later integration. An Astra takeover PASS never relabels the original failed DS run as helper-verified.

### Bind decoder fixtures to the consumed path

When acceptance includes an external decoder, the real public entrypoint must read valid format bytes through its ordinary loader and decoder. Constructing a valid object or byte buffer elsewhere, directly testing the decoder alone, or mocking its decoded return does not prove that integration. A placeholder permitted for an earlier input-only layer cannot carry forward as decoder evidence. In authorized synthetic tests, write the valid fixture to the path selected by the test plan/validator and bind its actual bytes, digest and any enclosing plan digest before invocation. Keep original archives unchanged and identify synthetic replacements as such.

Trace the public call through the loader to the consumed bytes. Verify the selected path, file digest and bytes reaching the real decoder agree with the intended fixture; use existing bindings or a scoped observer that forwards to the actual decoder, never a replacement result. Assert an independently specified decoded value or public output that depends on that fixture. An unused valid buffer, a valid file at another path, or a stale placeholder hidden by a mock must not satisfy the check. Keep setup rebinding separate from drift tests: freeze their expected hashes before mutation, rather than updating expectations to conceal drift. Reuse the existing declared integration test, with all fixture consumers inside its owned temporary scope.

A local missing dependency may produce an explicit skip, but report the collected/pass/skip counts and mark that decoder boundary NOT VERIFIED locally. Target acceptance must show the named required decoder test executed without a skip using the declared interpreter and real dependency, through the public entrypoint. A successful suite exit with that test skipped is insufficient. Bind the target log, candidate and fixture-routing assertions together; preserve earlier failures and later repaired receipts separately. A decoder fixture repair does not close independent renderer findings or prove acceptance of actual scientific artifacts.

### Direct versus nested calls, mutation stage and counters

For an integration handoff, distinguish direct calls from nested helper calls by actual caller boundary or source-defined phase, not argument count alone. State which calls the requirement covers and retain internal-call observations separately. Trace the relevant pre/post mutation state and return at that boundary; a nested caller may forward the result, wrap it or return nothing while mutating shared state. Add only the stage observations the contract needs.

Bind counters to their actual transitions: decoded/submitted work can exceed completed work after a failure. Derive expected last-attempted/last-completed values and event roles from that trace. If state evidence must include a helper's returned counts, preserve those original returns rather than recomputing a substitute or recording them only in a different event stream. Use the complete source-role inventory required by the real producer; these are semantic distinctions, not universal field names or equal-count rules.

### Write order, failure paths and optional fields

Before proposing cross-file/state invariants, trace the actual writer and its save helper: write order, commit/rename points, exceptions, and mutations of shared state dictionaries. Single-file atomic replacement does not make multiple output/status files a transaction. Identify which fields are set, retained or removed at each reachable persistence boundary, including failure of a save itself and a snapshot taken between saves. A final-state schema alone does not describe all legitimate archived states; use the project's actual consistency/snapshot guarantees.

For example, a writer may save raw results, mutate one dictionary to `complete` with a count/reason, then fail while saving status. Its exception handler may change that same dictionary to `infrastructure_failed` and add an error without clearing the count/reason. A reader between writes can also see raw results while persisted status is still `running`. Such combinations are not inherently corrupt. Confirm the actual producer permits them; do not generalize this example into permission for arbitrary state/count/error combinations. Validate the proven state domain and field relationships for each branch, including which count fields are optional and when counts can be compared under the task's snapshot contract.

For a new runner whose contract requires failure-safe completion, name the authoritative completion decision and the consumer that reads it. Earlier events may report readiness or partial progress, but must not independently claim completion while required stream/evidence/receipt finalization can still fail. Trace actual execution order, including `finally` and swallowed exceptions: required flush/close must succeed before authoritative success. Metadata or prose claiming that order is not evidence. Merely swapping two fallible success writes leaves another contradictory failure window. One authoritative final receipt with non-success earlier events is a possible task-local design, not a required framework.

Error-path cleanup must preserve the primary failure. If partial success-path output can block failure recording, provide the contract-required failure evidence through a viable destination; retrying exclusive creation of the same already-created file may fail again. Owned staging and exclusive publication can help, but require the actual write/flush/close/publication behavior to be verified and do not by themselves prove crash consistency. Exercise the relevant owned finalization failures through the real entrypoint, including its CLI wrapper when in scope. A required partial-write or close fault must reach that stage and retain invocation/partial-state evidence; throwing before `open` does not cover a partial-write collision. Assert the public outcome, required absence of success and required partial/failure evidence. Use small reached cases, not an exhaustive matrix. These new-runner requirements do not reinterpret legitimate archived partial states as corruption.

Preserve reachable partial/failed raw evidence without promoting it to a completed request, usable transform or scientific success. Producer legitimacy and task readiness are separate: if a task requires completed/coherent evidence, report that acceptance condition separately instead of inventing a producer invariant. Unknown state values, malformed reasons/errors or unexplained inconsistencies still need rejection or an explicit unresolved diagnosis according to the contract. “Stricter” validation is not evidence that an excluded state is impossible.

Prove requiredness independently of successful output. A field populated through `record.get(...)` can become null; trace subsequent guards and consumers before deciding whether that is legal. Where the actual contract permits missing/null metadata (for example, an optional stage label), preserve the documented missing-to-null behavior, validate a supplied non-null value's type, and do not infer a value or require it solely because a transform exists. Do not turn every `.get` into an optional-field rule: later producer checks may impose a requirement.

### Preserve required aggregation dimensions

For a requested summary, resolve whether the contract requires joint strata, separate marginal totals, or both, and state the output keys and denominator unit before implementation. When the required strata are request role × target availability × policy, retain that complete grouping key; a per-policy total plus separate role and availability marginals is not the same output. One request evaluated by three policies is one observation in each policy's applicable stratum, not three requests in a pooled bucket. Pooled outcome slots may be an additional explicitly labelled metric only when allowed. Preserve required zero-count outcomes, empty groups and per-window detail according to the actual contract; do not invent a universal Cartesian product or reinterpret unavailable inventory as physical absence.

Use a small asymmetric public fixture whose independently enumerated request/outcome rows make pooling, swapping dimensions or dropping a required group visible. Compare the actual returned/saved group keys, each denominator and each outcome count with literal expected groups derived from those rows, including the contract's treatment of unknown outcomes. A global sum invariant or successful suite exit can hold for the wrong grouping. Keep the expected groups independent of the production aggregation routine and retain separate ordinary valid coverage. Use existing declared tests and frozen/synthetic authorized inputs; this does not authorize rerunning archived scoring or scientific experiments.

### Numeric zero, null and rejected observations

When consumed fields distinguish missing/null, numeric zero and rejection, include a minimal complete real producer record plus the relevant serializer/helper sources in the selected task context before delegation. Pin their identities/hashes and write the allowed state combinations in the existing acceptance contract. A rejection decision does not by itself erase an observed value; avoid truthiness or `accepted == false` normalization to null.

For example, a depth contract may define:

| Sampling state | Raw value | Acceptance |
| --- | --- | --- |
| In bounds, zero observed | `0.0` | false |
| In bounds, negative observed | Actual finite negative value | false |
| Out of bounds | `null` | false |
| In bounds, valid positive observation | Actual numeric value | true |

These combinations apply only when supported by that producer and contract. Keep required-key presence, observation value, rejection reason and acceptance as separate checks; do not infer every rejected record must be null or that zero means unmeasured.

Feed the relevant real record through the public consumer/gate in the existing regression, including acceptance of legitimate zero/rejected evidence. Retain the record's actual index, dimensions, units and state relationships when adapting it. If authorized synthetic driving inputs are needed, declare that substitution without rewriting the archived expected values or claiming an experiment rerun. In a disposable copy, change a required numeric zero to null (and test the contract's other relevant invalid combinations); the independent check must reject for the semantic mismatch. Hand-built expected values derived from the consumer's normalization cannot establish compatibility. Use the [gate-adequacy procedure](public-acceptance.md#gate-adequacy) to verify the assertion detects a disabled gate.

### Required nulls and exported scope

Requiredness and nullability are separate. If an export contract requires a key with explicit `null` on a no-output branch, omitting that key is a schema defect. `bindings.get(key) is None` cannot distinguish these cases. Assert key presence (or the exact key set for a closed schema), then inspect the value by direct indexing. Preserve genuine optional fields where the actual contract allows omission; do not impose a universal null schema.

For example, an archived-viewer contract may require these four bindings even when no transform/model/station output exists:

```python
bindings = exported_row["source_bindings"]
assert set(bindings) == {"rgb_key", "alignment_key", "model_key", "station_key"}
assert bindings["rgb_key"] == expected_rgb_key  # From the frozen source plan.
for key in ("alignment_key", "model_key", "station_key"):
    assert bindings[key] is None
```

Adapt the field set to the actual contract, including any permitted extension keys. Test the real serializer/exported JSON, not only a hand-built dictionary: in disposable copies remove each required-null key and replace null with a fabricated zero or string, and confirm the contract check rejects each mutation. Also verify the legitimate populated branch; the no-output branch must not invent provenance to fill its fields.

Carry the declared scope from the frozen plan through machine-readable output and the viewer's required visible description. Compare against that independently supplied scope, not a value derived from the export under test; copying a field name or inserting the same fixed phrase in both implementation and test is insufficient. Test omitted and changed scope, plus missing viewer propagation when required. For example, a common-downstream archived comparison must retain that boundary rather than imply native method admission or end-to-end system success. These are contract-specific assertions, not a universal scope string or a new global helper gate.

Before requesting a fix, bind each new invariant to writer/helper source lines and the relevant normal/exception branch. Check both acceptance of reachable boundary states and rejection of truly invalid ones. Use temporary consumer fixtures or an authorized isolated fault-injection seam with external effects mocked; static source inspection may be sufficient to establish the branch. For frozen historical work, do not execute the historical writer, assets, alignment or scoring to manufacture these cases. Document what each probe actually demonstrates and retain review corrections as described below.

For required Boolean fields, require actual JSON booleans instead of coercing `null`, strings or integers with `bool(...)`; preserve permitted unknown values as unknown. Reject `NaN`/`Infinity` and non-finite numeric values when the contract requires finite metrics, exclude booleans from numeric validation, and enforce field-specific ranges/nullability from the producer. Missing/unmeasured errors must not become measured-looking zeroes. When the report asserts an archived selection policy, validate the policy field against its actual producer constant/contract as well as checking the selected ID/rank; an unchanged winner does not prove an unchanged rule. Do not invent project-specific rule text or metric requirements.

Use copied, hash-bound historical inputs to exercise the real consumer/public CLI in a temporary context. Verify complete relevant records and selection behavior, not just a hand-written fragment. Make fault mutations only in disposable copies and cover legitimate edge states too: a test rejecting a valid producer state is itself a defect. Preserve original source identity, hashes and archived flags. An evidence assembler may validate schema, counts and justified one-way relationships without loading historical assets, recomputing poses, rerunning alignment/scoring or reselecting results; do not add those operations unless the task explicitly requires them. Matching hashes establish byte identity, not correct interpretation.

### Correcting a review's proposed invariant

When later producer evidence disproves a finding, preserve the original review and verdict. Append a separate addendum naming the original artifact/hash and candidate fingerprint, the exact withdrawn or narrowed claim, the new source identity/hash and relevant branch, the revised requirement, and findings that still stand. A partial withdrawal does not turn a remaining `changes_requested` into `pass`; state the current assessment and unresolved findings explicitly. A later repaired candidate receives its own bound review. Do not overwrite earlier failures or silently relabel their tests as valid acceptance evidence.

The included live API fixture example below demonstrates only its own serialization contract. It neither establishes these scientific semantics nor authorizes regeneration of frozen experiments.

## Executable pattern to adapt

The skill includes [a small Python/SQLite → JSON → JavaScript example](../assets/producer-consumer/check_contract.py). It uses Python 3.9+ and Node.js 20+ with standard libraries only; it installs nothing. Run from the skill directory:

```text
python3 assets/producer-consumer/check_contract.py
```

If Node is outside PATH, pass `--node /absolute/path/to/node`. Missing Node fails with NOT VERIFIED. The example is deliberately self-contained and is not the robot project's producer or tests:

- `producer.py` is the example service: its serializer queries real SQLite rows and emits `station_count`.
- `generate_fixture.py` seeds 0, 1 and 42 rows in temporary SQLite and invokes that serializer. It does not re-create the returned fields. No normalization is used.
- `consumer.mjs` updates a small view interface (`textContent` and `disabled`); its tests read the generated fixture path and assert display and confirmation state. A real web project should use its real DOM/state/request layer and original framework.
- `check_contract.py` runs generation, checks the committed `preview.fixture.json` byte-for-byte, then executes the consumer tests against the fresh temporary JSON. A failure never overwrites the committed fixture. It requires a nonzero TAP test count, all passed, with no skips. It prints a JSON receipt containing source hashes, fixture hashes, argv, runtime versions and captured test output after checking source stability. All those assertions must pass for exit zero.
- `scripts/test_producer_contract_example.py` runs the intact chain and mutates isolated copies to prove it rejects producer-key drift, consumer-key drift, wrong producer types, stale fixtures, an empty consumer test file and an unavailable runtime. Its shared-assumption regression first shows the existing consumer suite passing on its old fixture after the copied producer changes a primitive field into a nested object, then requires the real generation-to-consumer acceptance chain to reject the mismatch. That counterexample demonstrates why green consumer tests alone are insufficient; it does not certify another project's handoff or replace its authentic integration sample.

To deliberately regenerate the example fixture after reviewing a contract change, invoke `generate_fixture.py` and save its stdout to the exact declared fixture file in the working candidate, then inspect the diff and rerun the check. Production adaptations should reuse the project's real serializer and consumer; copying this example's producer into tests would recreate the original testing gap.

This demonstration covers a successful preview and an empty preview, plus malformed input. It does not claim transaction, request, startup or concurrency coverage. Retain those distinct integration tests for the actual application.
