import builtins, symtable, sys, pathlib

BUILTINS = set(dir(builtins))

def module_bound(tree):
    """Names bound at module scope, visible everywhere in the file."""
    out = set()
    def walk(tbl, depth):
        for sym in tbl.get_symbols():
            if sym.is_assigned() or sym.is_namespace() or sym.is_imported():
                out.add(sym.get_name())
        for child in tbl.get_children():
            if child.get_type() == "class":
                out.add(child.get_name())
                walk(child, depth)
            elif tbl.get_type() == "module":
                walk(child, depth)
    walk(tree, 0)
    return out

def scan(path):
    src = pathlib.Path(path).read_text(encoding="utf-8")
    tbl = symtable.symtable(src, str(path), "exec")
    bound = module_bound(tbl)
    hits = []
    def visit(t, chain):
        # chain: names visible from enclosing function scopes (params/locals)
        visible = set(bound)
        for c in chain:
            visible |= c
        for sym in t.get_symbols():
            nm = sym.get_name()
            if not sym.is_referenced():
                continue
            if nm in visible or nm in BUILTINS:
                continue
            if sym.is_local() or sym.is_parameter() or sym.is_free():
                continue
            if sym.is_assigned() or sym.is_imported():
                continue
            hits.append((t.get_name(), t.get_lineno(), nm))
        # names bound in this scope, for children
        here = {s.get_name() for s in t.get_symbols() if s.is_assigned() or s.is_parameter() or s.is_imported()}
        for child in t.get_children():
            if child.get_type() == "function":
                visit(child, chain + [here])
            elif t.get_type() == "module":
                visit(child, chain + [here])
    visit(tbl, [])
    return hits

total = 0
for p in sorted(sys.argv[1:]):
    hits = scan(p)
    if hits:
        print(f"== {p}")
        for fname, lineno, nm in hits:
            print(f"   line {lineno} in {fname}() -> UNDEFINED '{nm}'")
        total += len(hits)
print(f"TOTAL={total}")
