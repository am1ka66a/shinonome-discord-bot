"""Audit: no await / nested DB acquisition inside a held DB connection scope."""
import ast
import pathlib

CM_NAMES = {"db_cursor", "db_conn"}
problems = []


def is_db_with(node):
    for item in node.items:
        call = item.context_expr
        if isinstance(call, ast.Call):
            fn = call.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if name in CM_NAMES:
                return name
    return None


class Scanner(ast.NodeVisitor):
    def __init__(self, path):
        self.path = path
        self.depth = 0

    def visit_With(self, node):
        self._with(node)

    def visit_AsyncWith(self, node):
        self._with(node)

    def _with(self, node):
        name = is_db_with(node)
        if name:
            self.depth += 1
            for child in node.body:
                self.generic_visit_body(child)
            self.depth -= 1
        else:
            self.generic_visit(node)

    def generic_visit_body(self, node):
        self.visit(node)

    def visit_Await(self, node):
        if self.depth > 0:
            problems.append((self.path, node.lineno, "await while holding a DB connection"))
        self.generic_visit(node)

    def visit_Call(self, node):
        if self.depth > 0:
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if name in ACQUIRERS:
                problems.append(
                    (self.path, node.lineno, f"calls {name}() (opens its own connection) while holding one")
                )
        self.generic_visit(node)


root = pathlib.Path(".")
files = [
    p
    for p in root.rglob("*.py")
    if not any(part in {".venv", "__pycache__", ".git"} for part in p.parts)
]

# Derive the acquirer set from the code itself: any function whose body opens a connection,
# plus anything that transitively calls such a function.
trees = {}
for p in files:
    try:
        trees[p] = ast.parse(p.read_text(encoding="utf-8"))
    except SyntaxError:
        pass

calls_of = {}
direct = set()
for p, tree in trees.items():
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names = set()
        opens = False
        for sub in ast.walk(fn):
            if isinstance(sub, ast.Call):
                f = sub.func
                n = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
                if n:
                    names.add(n)
                    if n in CM_NAMES or n == "get_db_connection":
                        opens = True
        calls_of.setdefault(fn.name, set()).update(names)
        if opens:
            direct.add(fn.name)

ACQUIRERS = set(direct)
for _ in range(6):  # transitive closure
    grown = {n for n, callees in calls_of.items() if callees & ACQUIRERS}
    if grown <= ACQUIRERS:
        break
    ACQUIRERS |= grown
ACQUIRERS -= CM_NAMES

for p in files:
    try:
        tree = ast.parse(p.read_text(encoding="utf-8"))
    except SyntaxError as e:
        problems.append((p, e.lineno, f"syntax error: {e.msg}"))
        continue
    Scanner(p).visit(tree)

if problems:
    for path, line, msg in problems:
        print(f"FAIL {path}:{line}: {msg}")
    raise SystemExit(1)
print(
    f"OK: scanned {len(files)} files against {len(ACQUIRERS)} derived connection-acquiring "
    "functions; no await and no nested acquisition inside a DB scope"
)
