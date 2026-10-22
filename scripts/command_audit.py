"""Small, payload-free observations of requested shell syntax; not an interceptor."""
import hashlib
from pathlib import PurePosixPath
import re
import shlex

STATUSES = ('inspected_literal_subset', 'missing_command', 'unsupported_syntax', 'over_limit')
OBSERVATIONS = ('requested_recursive_rm', 'test_output_pipeline',
                'python_without_explicit_bytecode_guard')
TEXT_OBSERVATIONS = ('explicit_bytecode_writer_mention', 'absolute_tmp_path_mention')
MAX_CHARS = 32768
REVIEW_CUES = ('worker_log_envelope_mention', 'worker_log_exit_tail_not_observed',
               'output_filter_pipeline_mention', 'python_header_without_explicit_guard_mention',
               'dynamic_interpreter_probe_mention')


def probe_cues(command):
    """Bounded lexical hints only: no expansion, shell AST, dataflow or execution.

    Even an observed exit tail cannot certify the envelope body or test outcome.
    Quoted examples and heredoc bodies can produce cues; review the private args.
    """
    cues = set()
    envelope = 'team_dev_log_dir' in command and 'team_dev_test_exit' in command
    if envelope:
        cues.add('worker_log_envelope_mention')
    # Deliberately broad: includes dynamic/quoted commands and manual probes.
    if re.search(r'(?<!\|)\|(?!\|)\s*(?:[\w./-]+/)?(?:head|tail|grep|tee)\b', command):
        cues.add('output_filter_pipeline_mention')
    lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|<>()\n')
    lexer.whitespace = ' \t\r'
    lexer.whitespace_split = True
    lexer.commenters = ''
    try:
        tokens = list(lexer)
    except ValueError:
        if envelope:
            cues.add('worker_log_exit_tail_not_observed')
        return sorted(cues), 'unparsed'
    if envelope:
        tail = list(tokens)
        while tail and set(tail[-1]) <= set(';\n'):
            tail.pop()
        if not (len(tail) >= 2 and tail[-2:] == ['exit', '$team_dev_test_exit']
                and (len(tail) == 2 or set(tail[-3]) <= set(';\n'))):
            cues.add('worker_log_exit_tail_not_observed')
    segment = []
    for index, token in enumerate(tokens):
        if token and set(token) <= set(';&|<>()\n'):
            segment = []
            continue
        if '$' in token and index + 1 < len(tokens) and tokens[index + 1] in ('-c', '-m', '-B', '-IB'):
            cues.add('dynamic_interpreter_probe_mention')
        if re.fullmatch(r'python(?:\d+(?:\.\d+)*)?', PurePosixPath(token).name):
            flags, _ = python_options(tokens[index + 1:])
            # Only a plain local prefix establishes a textual env guard here.
            # Unknown wrappers remain unverified, even with a variable mention.
            prefix = segment[1:] if segment[:1] == ['if'] else segment
            env_guard, known_prefix = False, True
            for part in prefix:
                if re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*=.*', part):
                    if part.split('=', 1)[0] == 'PYTHONDONTWRITEBYTECODE':
                        env_guard = part == 'PYTHONDONTWRITEBYTECODE=1'
                elif PurePosixPath(part).name not in ('env', 'command', 'exec'):
                    known_prefix = False
            if 'B' not in flags and not (known_prefix and env_guard and not any(x in flags for x in 'EI')):
                cues.add('python_header_without_explicit_guard_mention')
        segment.append(token)
    return sorted(cues), 'scanned'


def inspect_bash(args, *, version=2):
    """Keep v1 byte-for-byte reproducible for historical event/review bindings."""
    if type(version) is not int or version not in (1, 2):
        raise ValueError('Unsupported command audit version')
    result = _inspect_bash_v1(args)
    if version == 1:
        return result
    result.update(version=2, review_cues=[], probe_scan_status='not_scanned')
    if result['text_observation_status'] == 'scanned':
        result['review_cues'], result['probe_scan_status'] = probe_cues(args['command'])
    return result


def python_options(argv):
    flags, index = '', 0
    while index < len(argv):
        option = argv[index]
        if option == '-m':
            return flags, argv[index + 1] if index + 1 < len(argv) else None
        if option.startswith('-m'):
            return flags, option[2:]
        if option in ('-W', '-X', '--check-hash-based-pycs'):
            index += 2
            continue
        if option.startswith(('-W', '-X', '--check-hash-based-pycs=')):
            index += 1
            continue
        if not re.fullmatch(r'-[bBdEiIOPqRsSuvx]+', option):
            break
        flags += option[1:]
        index += 1
    return flags, None


def _inspect_bash_v1(args):
    """Do not persist argv, paths, environment values, stdout, or reasoning.

    Deliberately inspect only unquoted literal command lists. Unsupported syntax
    remains uninspected, never clean. A requested branch need not have executed.
    """
    command = args.get('command') if isinstance(args, dict) else None
    result = {'version': 1, 'status': 'missing_command', 'observations': [],
              'text_observations': [], 'text_observation_status': 'not_scanned'}
    if not isinstance(command, str) or not command.strip():
        return result
    result.update(command_sha256=hashlib.sha256(command.encode('utf-8', errors='surrogatepass')).hexdigest(),
                  command_chars=len(command))
    if len(command) > MAX_CHARS:
        result['status'] = 'over_limit'
        return result
    # These are bounded text cues, not shell/Python parsing or execution evidence.
    # Keep them separate even when quoted scripts remain unsupported below.
    result['text_observation_status'] = 'scanned'
    if re.search(r'\b(?:py_compile|compileall)\b', command):
        result['text_observations'].append('explicit_bytecode_writer_mention')
    if re.search(r'(?<![\w./-])/(?:private/)?(?:var/)?tmp(?:/|(?=[\s\x27\x22]|$))', command):
        result['text_observations'].append('absolute_tmp_path_mention')
    # Reject quoting/expansion/control syntax instead of pretending to parse it.
    if any(c in command for c in "'\"`\\$(){}#\x00\r"):
        result['status'] = 'unsupported_syntax'
        return result
    # Permit only literal stderr/stdout fd duplication; other redirects are unknown.
    literal = re.sub(r'(?<!\S)[12]?>&[12](?=\s|$)', ' ', command)
    if '<' in literal or '>' in literal or '&' in literal.replace('&&', ''):
        result['status'] = 'unsupported_syntax'
        return result
    parts = re.split(r'(&&|\|\||[;|\n])', literal)
    observations = set()
    unsupported_wrapper = False
    for index in range(0, len(parts), 2):
        tokens = parts[index].split()
        if not tokens:
            continue
        env_guard = False
        wrapped = False
        # Support assignment prefixes, plain env, and command/exec wrappers only.
        while tokens:
            if re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*=.*', tokens[0]):
                if tokens[0].split('=', 1)[0] == 'PYTHONDONTWRITEBYTECODE':
                    env_guard = tokens[0] == 'PYTHONDONTWRITEBYTECODE=1'
                tokens.pop(0)
            elif PurePosixPath(tokens[0]).name in ('env', 'command', 'exec'):
                wrapped = True
                tokens.pop(0)
            else:
                break
        if not tokens:
            unsupported_wrapper = unsupported_wrapper or wrapped
            continue
        if wrapped and tokens[0].startswith('-'):
            # Unknown wrapper options can change environment, parsing or execution.
            # Preserve other segments' findings, but never label this coverage clean.
            unsupported_wrapper = True
            continue
        program, argv = PurePosixPath(tokens[0]).name, tokens[1:]
        if program == 'rm':
            for option in argv:
                if option == '--':
                    break
                if option == '--recursive' or (option.startswith('-') and not option.startswith('--')
                                                and ('r' in option or 'R' in option)):
                    observations.add('requested_recursive_rm')
        if re.fullmatch(r'python(?:\d+(?:\.\d+)*)?', program):
            short, module = python_options(argv)
            if 'B' not in short and not (env_guard and not any(x in short for x in 'EI')):
                observations.add('python_without_explicit_bytecode_guard')
            module_test = module in ('unittest', 'pytest')
        else:
            module_test = program in ('pytest', 'unittest')
        if module_test and index + 1 < len(parts) and parts[index + 1] == '|':
            observations.add('test_output_pipeline')
    result.update(status='unsupported_syntax' if unsupported_wrapper else 'inspected_literal_subset',
                  observations=sorted(observations))
    return result


class CommandAudit:
    """Aggregate only fixed classifications. It cannot certify process compliance."""
    def __init__(self):
        self.bash_calls = 0
        self.status_counts = {name: 0 for name in STATUSES}
        self.observation_counts = {name: 0 for name in OBSERVATIONS}
        self.text_observation_counts = {name: 0 for name in TEXT_OBSERVATIONS}
        self.text_scanned_calls = 0
        self.text_unavailable_calls = 0
        self.v2_calls = 0
        self.review_cue_counts = {name: 0 for name in REVIEW_CUES}
        self.probe_scanned_calls = 0
        self.probe_unavailable_calls = 0

    def event(self, item):
        if item.get('type') != 'tool_execution_start' or item.get('toolName') != 'bash':
            return
        self.bash_calls += 1
        self.text_unavailable_calls += 1
        audit = item.get('commandAudit')
        if not isinstance(audit, dict) or type(audit.get('version')) is not int or audit.get('version') not in (1, 2) or audit.get('status') not in STATUSES:
            self.status_counts['missing_command'] += 1
            return
        self.status_counts[audit['status']] += 1
        if audit['version'] == 2:
            self.v2_calls += 1
            if audit.get('probe_scan_status') == 'scanned' and isinstance(audit.get('review_cues'), list):
                self.probe_scanned_calls += 1
            else:
                self.probe_unavailable_calls += 1
            if isinstance(audit.get('review_cues'), list):
                for name in self.review_cue_counts:
                    if name in audit['review_cues']:
                        self.review_cue_counts[name] += 1
        observations = audit.get('observations', [])
        if isinstance(observations, list):
            for name in self.observation_counts:
                if name in observations:
                    self.observation_counts[name] += 1
        text_observations = audit.get('text_observations')
        if audit.get('text_observation_status') == 'scanned' and isinstance(text_observations, list):
            self.text_unavailable_calls -= 1
            self.text_scanned_calls += 1
            for name in self.text_observation_counts:
                if name in text_observations:
                    self.text_observation_counts[name] += 1

    def summary(self):
        result = {'version': 1, 'bash_calls': self.bash_calls,
                'status_counts': dict(self.status_counts),
                'observation_counts': dict(self.observation_counts),
                'text_observation_counts': dict(self.text_observation_counts),
                'text_scanned_calls': self.text_scanned_calls,
                'text_unavailable_calls': self.text_unavailable_calls,
                'text_observation_scope': 'bounded_text_mentions_not_execution',
                'review_required': bool(self.text_unavailable_calls) or any(self.observation_counts.values()) or any(self.text_observation_counts.values()) or any(
                    count for name, count in self.status_counts.items() if name != 'inspected_literal_subset'),
                'compliance': 'not_assessed', 'observation_scope': 'requested_literal_shell_subset'}
        if self.v2_calls:
            unavailable = self.probe_unavailable_calls + self.bash_calls - self.v2_calls
            result.update(version=2, review_cue_counts=dict(self.review_cue_counts),
                          probe_scanned_calls=self.probe_scanned_calls, probe_unavailable_calls=unavailable,
                          probe_scope='bounded_lexical_cues_not_execution_or_envelope_validation')
            result['review_required'] = result['review_required'] or bool(unavailable) or any(self.review_cue_counts.values())
        return result
