"""Recognize bounded direct Python-to-filter requests, without executing shell."""
from pathlib import PurePosixPath
import re
import shlex

from command_audit import MAX_CHARS

STATUSES = ('simple_lists', 'unsupported_syntax', 'unavailable')
FILTERS = ('head', 'tail', 'grep', 'tee')
OBSERVATION = 'python_live_output_filter'
TOKEN = re.compile(r'''(?P<space>[ \t]+)|(?P<redirect>[12]>&[12])|(?P<op>&&|\|\||[;|\n])|(?P<quote>'[^']*'|"(?:[^"\\]|\\.)*")|(?P<word>[A-Za-z0-9_./:=+,%@~-]+)''')


def tokens(command):
    """Only whole literal words/quotes, list/pipeline ops and fd duplication.

    Quotes stay opaque until after token classification. Reject expansions,
    comments, heredocs, other redirections, concatenated quotes and compound
    shell syntax instead of treating code/examples as executable shell stages.
    """
    result, position, adjacent_word = [], 0, False
    while position < len(command):
        match = TOKEN.match(command, position)
        if match is None:
            return None
        kind, value = match.lastgroup, match.group()
        position = match.end()
        if kind == 'space':
            adjacent_word = False
            continue
        if kind in ('word', 'quote'):
            if adjacent_word:
                return None
            if kind == 'quote':
                if value.startswith('"') and any(c in value for c in ('$','`')):
                    return None
                value = shlex.split(value)[0]
            result.append(('word', value))
            adjacent_word = True
        else:
            result.append((kind, value))
            adjacent_word = False
    return result


def program(words):
    index = 0
    while index < len(words):
        word = words[index]
        if re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*=.*', word):
            index += 1
        elif PurePosixPath(word).name in ('env','command','exec'):
            index += 1
        else:
            break
    if index == len(words) or words[index].startswith('-'):
        return None
    return PurePosixPath(words[index]).name


def inspect(args):
    command = args.get('command') if isinstance(args, dict) else None
    result = {'version': 1, 'status': 'unavailable', 'observations': []}
    if not isinstance(command, str) or not command.strip() or len(command) > MAX_CHARS:
        return result
    parts = tokens(command)
    result['status'] = 'unsupported_syntax'
    if parts is None:
        return result
    pipelines, pipeline, words = [], [], []
    for kind, value in parts + [('op',';')]:
        if kind == 'word':
            words.append(value)
        elif kind == 'op':
            if not words and (value == '|' or pipeline):
                return result
            if words:
                if words[0] in ('if','then','else','fi','for','while','do','done','case','esac','function'):
                    return result
                pipeline.append(program(words))
                words = []
            if value != '|':
                if pipeline:
                    pipelines.append(pipeline)
                pipeline = []
    result['status'] = 'simple_lists'
    for stages in pipelines:
        for index, name in enumerate(stages):
            if name and re.fullmatch(r'python(?:\d+(?:\.\d+)*)?', name):
                if any(item in FILTERS for item in stages[index+1:]):
                    result['observations'] = [OBSERVATION]
    return result


class LiveOutputAudit:
    def __init__(self):
        self.calls = 0
        self.counts = {key: 0 for key in STATUSES}
        self.filters = 0

    def event(self, item):
        if item.get('type') != 'tool_execution_start' or item.get('toolName') != 'bash':
            return
        self.calls += 1
        value = item.get('liveOutputAudit')
        if (not isinstance(value, dict) or type(value.get('version')) is not int or
                value.get('version') != 1 or value.get('status') not in STATUSES):
            self.counts['unavailable'] += 1
            return
        self.counts[value['status']] += 1
        if OBSERVATION in value.get('observations', []):
            self.filters += 1

    def summary(self):
        return {'version': 1, 'bash_calls': self.calls, 'status_counts': dict(self.counts),
                'python_live_output_filter_count': self.filters,
                'scope': 'requested_simple_python_pipelines_not_execution_or_log_lifecycle',
                'compliance': 'not_assessed'}
