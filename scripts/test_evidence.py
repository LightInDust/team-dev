"""Cooperative test-contract checks, not a sandbox or general Python dataflow analyzer."""
import ast
from pathlib import PurePath
import re


def _runs_unittest(arguments):
    """Recognize Python's module invocation after common interpreter options."""
    i = 0
    while i < len(arguments):
        arg = arguments[i]
        if arg == '-m':
            return arguments[i+1:i+2] == ['unittest']
        if arg.startswith('-m'):
            return arg[2:] == 'unittest'
        if arg in ('-W', '-X', '--check-hash-based-pycs'):
            if i+1 >= len(arguments): return False
            i += 2
        elif arg.startswith(('-W', '-X', '--check-hash-based-pycs=')):
            i += 1
        elif re.fullmatch(r'-[bBdEiIOPqRsSuvx]+', arg):
            i += 1
        else:
            # Script path, -c, --, or an unsupported option: not a direct module command.
            return False
    return False


def unittest_evidence(argv, stdout, stderr):
    recognized = any(re.fullmatch(r'python(?:\d+(?:\.\d+)*)?', PurePath(token).name)
                     and _runs_unittest(argv[i+1:]) for i, token in enumerate(argv))
    if not recognized:
        return {'framework': None, 'count': None, 'passed': True, 'reason': None}
    matches = re.findall(r'^Ran (\d+) tests? in [^\r\n]+$', stdout+'\n'+stderr, re.MULTILINE)
    count = int(matches[-1]) if matches else None
    return {'framework': 'unittest', 'count': count, 'passed': count is not None and count > 0,
            'reason': 'missing_test_summary' if count is None else 'no_tests_collected' if count == 0 else None}


def module_cache_writes(source):
    tree = ast.parse(source)
    sys_names, caches, mock_names, unittest_names, patches = {'sys'}, set(), set(), set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name == 'sys': sys_names.add(item.asname or 'sys')
                if item.name == 'unittest': unittest_names.add(item.asname or 'unittest')
                if item.name == 'unittest.mock':
                    (mock_names if item.asname else unittest_names).add(item.asname or 'unittest')
        elif isinstance(node, ast.ImportFrom):
            for item in node.names:
                if node.module == 'sys' and item.name == 'modules': caches.add(item.asname or item.name)
                if node.module == 'unittest' and item.name == 'mock': mock_names.add(item.asname or item.name)
                if node.module == 'unittest.mock' and item.name == 'patch': patches.add(item.asname or item.name)

    def cache(node):
        return ((isinstance(node, ast.Name) and node.id in caches) or
                (isinstance(node, ast.Attribute) and node.attr == 'modules' and
                 isinstance(node.value, ast.Name) and node.value.id in sys_names))

    def cache_target(node):
        if isinstance(node, (ast.Tuple, ast.List)):
            return any(cache_target(item) for item in node.elts)
        if isinstance(node, ast.Starred):
            return cache_target(node.value)
        return ((isinstance(node, ast.Attribute) and cache(node)) or
                (isinstance(node, ast.Subscript) and cache(node.value)))

    # Deliberately limited: direct aliases, not dynamic lookup or rebinding analysis.
    for _ in range(3):
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and cache(node.value):
                caches.update(target.id for target in node.targets if isinstance(target, ast.Name))

    def mock(node):
        return ((isinstance(node, ast.Name) and node.id in mock_names) or
                (isinstance(node, ast.Attribute) and node.attr == 'mock' and
                 isinstance(node.value, ast.Name) and node.value.id in unittest_names))

    def patch(node):
        return ((isinstance(node, ast.Name) and node.id in patches) or
                (isinstance(node, ast.Attribute) and node.attr == 'patch' and mock(node.value)))

    def patches_cache(node):
        if not isinstance(node, ast.Call): return False
        first = node.args[0] if node.args else next((kw.value for kw in node.keywords
                                                   if kw.arg in ('target', 'in_dict')), None)
        target_string = isinstance(first, ast.Constant) and isinstance(first.value, str) and (
            first.value == 'sys.modules' or first.value.startswith('sys.modules.'))
        if patch(node.func): return target_string
        return (isinstance(node.func, ast.Attribute) and node.func.attr == 'dict' and
                patch(node.func.value) and (cache(first) or target_string))

    findings = set()

    class Visitor(ast.NodeVisitor):
        function_depth = 0

        def add(self, node, kind): findings.add((node.lineno, kind))

        def visit_FunctionDef(self, node):
            # Decorators are scoped patch wrappers; defaults execute at definition time.
            for value in (*node.args.defaults, *node.args.kw_defaults):
                if value is not None: self.visit(value)
            self.function_depth += 1
            for statement in node.body: self.visit(statement)
            self.function_depth -= 1

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Assign(self, node):
            if any(cache_target(t) for t in node.targets):
                self.add(node, 'module_cache_assignment')
            self.generic_visit(node)

        def visit_AnnAssign(self, node):
            if node.value is not None and cache_target(node.target):
                self.add(node, 'module_cache_assignment')
            self.generic_visit(node)

        def visit_AugAssign(self, node):
            if cache(node.target) or cache_target(node.target):
                self.add(node, 'module_cache_assignment')
            self.generic_visit(node)

        def visit_Delete(self, node):
            if any(cache_target(t) for t in node.targets):
                self.add(node, 'module_cache_delete')
            self.generic_visit(node)

        def visit_Call(self, node):
            if isinstance(node.func, ast.Attribute) and cache(node.func.value) and node.func.attr in (
                    'update', 'setdefault', 'pop', 'popitem', 'clear', '__setitem__', '__delitem__'):
                self.add(node, 'module_cache_mutation')
            if self.function_depth == 0 and patches_cache(node):
                self.add(node, 'import_time_module_patch')
            if isinstance(node.func, ast.Attribute) and node.func.attr == 'start' and patches_cache(node.func.value):
                self.add(node, 'unscoped_module_patch')
            self.generic_visit(node)

    Visitor().visit(tree)
    return [{'line': line, 'kind': kind} for line, kind in sorted(findings)]
