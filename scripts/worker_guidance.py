"""Versioned selectable domain guidance for new worker preparations.

The catalog below is a fixed snapshot of the conditional domain instructions
that used to be inlined in ``team_dev.prepare``. It is consulted only while
preparing a *new* run: an omitted ``worker_guidance`` field selects every
section (safe compatibility default), an explicit list selects the named
sections, and an explicit ``[]`` selects none. Existing runs keep the prompt
bytes written at their own preparation; nothing here rewrites or re-compares an
already prepared run.
"""
import hashlib

VERSION = 1

# Canonical render order, independent of the order a task lists the names in.
CANONICAL_ORDER = ('rendered_artifacts', 'archived_evidence',
                   'model_isolation', 'live_serialization')

SECTIONS = {
    'rendered_artifacts': (
        'For rendered artifacts, derive validators from the contract and actual frozen inputs; exercise the public export entrypoint without bypassing validation. Preserve frozen inputs and scientific results. Synthetic-only checks leave actual-data visual acceptance NOT VERIFIED.\n'
        'For scientific raw-image pixel overlays, explicitly use nearest interpolation and disable resampling in original/overlay image artists; assert the real artist properties, arrays and UV alignment under conflicting rcParams, then inspect the saved output.\n'
        'When the contract fixes the original image frame, explicitly fix both image-axis limits and disable autoscale. Test the final populated overlay after scatter/layout/draw, including valid fractional boundary UVs; correct imshow extent alone does not prove unchanged axes framing. Keep the declared validity rule and coordinates, and inspect affected saved outputs.\n'
        'For those rendered artifacts, check plotted data/projection identities and both coordinate ranges independently of renderer indexing; verify final dimensions/DPI, required text bounds, declared separation, readable glyphs and required dates/source labels in saved outputs. Unreadable required text is not cosmetic.\n'
        'For rendering resources, cover cleanup from acquisition through construction/layout, handoff and export; a save-only failure test does not establish builder cleanup. Use the smallest relevant checks within the original test framework.\n'
    ),
    'archived_evidence': (
        'For corruption/swap fixtures, establish a valid baseline and assert the mutated property really differs before testing rejection; swapping byte-identical images cannot trigger content-hash mismatch. Freeze baseline expectations before mutation, do not refresh them afterward, and verify the intended public validation boundary was reached with the specified rejection reason.\n'
        'When the contract binds outputs to input/source bytes, capture each input before its first consumption and hash the same bytes that reach parsing/decoding (or an owned immutable snapshot); capture selected source/meaning dependencies before their authorized execution. Resolve nested inputs from the captured manifest. Before success metadata, recheck the entire captured set, including declared images and dependencies, against frozen initial hashes; never replace the baseline with post-computation hashes. Use a small task-local binding, not a new framework. Exercise drift through the public path in declared tests on owned copies; a failure must withhold success metadata. File hashes alone do not attest already-loaded code; disclose unresolved execution identity and never import forbidden historical code for this check.\n'
        'For frozen historical evidence instead, read the producer and meaning-defining helpers without executing historical producer scripts, then test the real consumer on hash-bound archived copies; do not invoke/import producer code to regenerate fixtures or experiments. Distinguish raw results, usable transforms, scoring eligibility and scientific correctness. Enforce only source-supported implications, preserve null/unknown, and report missing semantic context instead of inventing invariants.\n'
        'Before enforcing cross-file/state invariants, trace write order, save failures and shared mutable state through the actual writer/helper; per-file atomic saves are not a multi-file transaction. Preserve source-permitted partial/failed raw evidence without calling it usable/successful. Prove field requiredness through guards and consumers; preserve contract-permitted missing/null metadata instead of making it required because a transform exists.\n'
    ),
    'model_isolation': (
        'When this task forbids real model calls, use the shared default-deny model boundary fixture before model-loading imports/setup and in every affected test, including fault/export cases. Latch forbidden attempts and assert none in fixture cleanup so caught exceptions cannot become empty-result success; trace actual lookup locations and scoped model mocks. This does not authorize extra probes.\n'
        'Install shared guards and task-required device settings before real fixture constructors, backprojection helpers or superclass setup. For model-free CPU fixtures use the actual supported explicit CPU option; synthetic inputs do not prevent a CUDA default. Retain a constructor-time missing-mock/caught-attempt regression and valid guarded setup, not only test-body coverage.\n'
        'Fault injection tests must reach the injected boundary: provide valid nonempty upstream state and required initialization, assert the intended error/result and expected call count/arguments. Empty outputs, an earlier setup/import error or a model guard exception cannot stand in for the intended fault. Report executed and skipped counts; all-skipped local tests remain NOT VERIFIED until required target checks execute.\n'
    ),
    'live_serialization': (
        'For authorized live serialized API/UI integration, generate consumer test input through the real producer serialization path in an isolated fixture; hand-written JSON alone is not integration evidence.\n'
        'Where the export contract requires an explicit null, assert key presence and null value separately; get(key) alone cannot prove presence. Preserve the frozen scope in exported JSON and required viewer text; do not turn a common-downstream comparison into a claim of native end-to-end success. Test omitted/changed fields against the actual serializer.\n'
        'For the authorized live API/UI case, run generation and the real consumer assertions in the same acceptance command. If a fixture is committed, check regeneration equality without overwriting it during acceptance.\n'
        'Normalize only documented nondeterministic values, never contract keys, types, units or nesting. Include missing/renamed/wrong-type field checks and assert visible behavior or requests.\n'
    ),
}

ASSESSORS = ('astra', 'sol', 'user')


def resolve(task):
    """Resolve a task's selection into a JSON-safe version 1 policy.

    No semantic keyword inference: only the explicit ``worker_guidance`` field
    and, for an explicit selection, the ``guidance_assessment`` object decide
    the result.
    """
    if 'worker_guidance' not in task:
        selected = list(CANONICAL_ORDER)
        assessment = None
    else:
        selected = _selection(task['worker_guidance'])
        assessment = _assessment(task.get('guidance_assessment'))
    return {'version': VERSION,
            'sections': selected,
            'content_sha256': {name: hashlib.sha256(SECTIONS[name].encode('utf-8')).hexdigest()
                               for name in selected},
            'assessment': assessment}


def text(policy, section):
    """Return the selected section content, or '' when it was not selected."""
    if section not in policy.get('sections', []):
        return ''
    return SECTIONS[section]


def _selection(value):
    if not isinstance(value, list):
        raise ValueError('worker_guidance must be a list of section names')
    seen = set()
    for entry in value:
        if not isinstance(entry, str):
            raise ValueError('worker_guidance entries must be strings')
        if entry not in SECTIONS:
            raise ValueError('Unknown worker_guidance section: ' + entry)
        if entry in seen:
            raise ValueError('Duplicate worker_guidance section: ' + entry)
        seen.add(entry)
    return [name for name in CANONICAL_ORDER if name in seen]


def _assessment(value):
    if not isinstance(value, dict):
        raise ValueError('An explicit worker_guidance selection requires a guidance_assessment object')
    by = value.get('by')
    if by not in ASSESSORS:
        raise ValueError('guidance_assessment by must be astra, sol or user')
    reason = value.get('reason')
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('guidance_assessment reason must be a nonblank string')
    return {'by': by, 'reason': reason}
