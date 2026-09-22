"""One-off: report names loaded but never defined anywhere in scope (regression catcher)."""
import ast, builtins, sys, pathlib

BUILTINS = set(dir(builtins))

def collect_stores(node):
    """All names bound anywhere inside node (approximate, scope-insensitive on purpose)."""
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
            args = getattr(n, "args", None)
            if args:
                for a in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
                    out.add(a.arg)
                if args.vararg: out.add(args.vararg.arg)
                if args.kwarg: out.add(args.kwarg.arg)
            out.update(getattr(n, "decorator_list", []) and [])  # noop
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        elif isinstance(n, ast.Import):
            for a in n.names: out.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names: out.add(a.asname or a.name)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, ast.Global) or isinstance(n, ast.Nonlocal):
            out.update(n.names)
    return out

def check(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    module_names = collect_stores(tree)
    problems = []
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        local = collect_stores(fn)
        # names bound in any enclosing function
        enclosing = set(module_names)
        for n in ast.walk(fn):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                nm = n.id
                if nm in local or nm in enclosing or nm in BUILTINS:
                    continue
                problems.append((fn.name, n.lineno, nm))
    return problems

for arg in sys.argv[1:]:
    p = pathlib.Path(arg)
    probs = check(p)
    print(f"== {p} == {len(probs)} suspicious")
    seen = set()
    for fname, lineno, nm in probs:
        key = (fname, nm)
        if key in seen: continue
        seen.add(key)
        print(f"  {p.name}:{lineno} in {fname}() -> undefined name '{nm}'")
