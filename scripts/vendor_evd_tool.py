"""Vendor rs_elf/tools/evd_tool.py into core/evd/, keeping only what the app calls.

Upstream `evd_tool.py` is a ~23k-line command-line program: a decoder/encoder
for the EVD script format wrapped in a CLI that also disassembles R5900
handlers, audits the dispatch table, walks SLZ containers, generates markdown
references and runs corpus round-trips. This project uses the format half and
none of the program around it -- it is handed raw EVD payloads by the
compression handler and drives everything through `core.evd.api`.

So the vendored copy is the transitive closure of what `api.py` actually
reaches, and nothing else. That is computed here rather than hand-edited, which
is what keeps re-syncing cheap:

    python scripts/vendor_evd_tool.py --check     # is the vendored copy current?
    python scripts/vendor_evd_tool.py             # re-vendor from upstream

Reachability is a static walk of the module's own global names. It is sound for
this module because it dispatches through direct references and dict literals
holding function objects, never through `globals()[name]` or `getattr` on the
module -- verify that still holds if the strip ever starts dropping something
that turns out to be live.
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
UPSTREAM = PROJECT.parent / "tools" / "evd_tool.py"
VENDORED = PROJECT / "core" / "evd" / "evd_tool.py"
API = PROJECT / "core" / "evd" / "api.py"

HEADER = '''"""Vendored from rs_elf/tools/evd_tool.py -- do not edit by hand.

Reduced to the parts `core.evd.api` reaches. Regenerate with
`python scripts/vendor_evd_tool.py` after updating the upstream copy; that
script explains what is dropped and why.

{provenance}
"""
'''


def api_entry_points(source: str) -> set[str]:
    """Every `evd_tool.NAME` the API layer touches."""
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "evd_tool":
                names.add(node.attr)
    return names


def defined_names(node: ast.stmt) -> set[str]:
    """The global names a top-level statement binds."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    if isinstance(node, ast.Assign):
        return {t.id for t in node.targets if isinstance(t, ast.Name)}
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return {node.target.id}
    if isinstance(node, ast.For):
        return {t.id for t in ast.walk(node.target) if isinstance(t, ast.Name)}
    return set()


def is_definition(node: ast.stmt) -> bool:
    """Whether a statement introduces a name, as opposed to changing one.

    `TABLE["form"]["field"] = ...` is an `ast.Assign` but binds nothing: it
    edits a table defined earlier. Treating it as a definition drops it, and the
    module then names that one field differently while assembling identically.
    """
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return True
    if isinstance(node, ast.Assign):
        return all(isinstance(t, ast.Name) for t in node.targets)
    if isinstance(node, ast.AnnAssign):
        return isinstance(node.target, ast.Name)
    return False


def is_module_guard(node: ast.stmt) -> bool:
    """The `if __name__ == "__main__":` block, which only calls the CLI."""
    return isinstance(node, ast.If) and "__name__" in referenced_names(node.test)


def referenced_names(node: ast.stmt) -> set[str]:
    """Every bare name a statement mentions, at any depth."""
    return {inner.id for inner in ast.walk(node) if isinstance(inner, ast.Name)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="exit non-zero if the vendored copy is out of date")
    parser.add_argument("--upstream", type=Path, default=UPSTREAM)
    args = parser.parse_args()

    if not args.upstream.is_file():
        print(f"upstream not found: {args.upstream}", file=sys.stderr)
        return 2

    source = args.upstream.read_text(encoding="utf-8")
    tree = ast.parse(source)

    imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    bindings: dict[str, ast.stmt] = {}
    for node in tree.body:
        for name in defined_names(node):
            bindings.setdefault(name, node)

    # Statements that are not definitions but change what the definitions hold:
    # `FORM_NAME_ALIASES.update(...)` and a loop that fills in parameter notes.
    # Dropping these leaves a module that still assembles the same bytes but
    # names forms and parameters differently, which is a silent difference and
    # exactly the kind this script has to not make.
    side_effects = [
        node for node in tree.body
        if not is_definition(node)
        and not isinstance(node, (ast.Import, ast.ImportFrom))
        and not is_module_guard(node)
        and not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
    ]

    keep: set[ast.stmt] = set()
    seen: set[str] = set()

    def close_over(names) -> None:
        queue = [n for n in names if n in bindings]
        while queue:
            name = queue.pop()
            if name in seen:
                continue
            seen.add(name)
            node = bindings[name]
            if node in keep:
                continue
            keep.add(node)
            queue.extend(referenced_names(node) & set(bindings))

    close_over(api_entry_points(API.read_text(encoding="utf-8")))

    # A side effect is kept when it touches something kept, which can pull in
    # further definitions, which can make another side effect relevant.
    changed = True
    while changed:
        changed = False
        kept_names = {n for node in keep for n in defined_names(node)}
        for node in side_effects:
            if node in keep or isinstance(node, ast.Delete):
                continue
            if referenced_names(node) & kept_names:
                keep.add(node)
                close_over(referenced_names(node))
                changed = True
    # `del` of a loop's temporaries, kept only alongside the loop that made them.
    kept_names = {n for node in keep for n in defined_names(node)}
    for node in side_effects:
        if isinstance(node, ast.Delete) and referenced_names(node) <= kept_names:
            keep.add(node)

    kept = [n for n in tree.body if n in keep]
    lines = source.splitlines(keepends=True)

    def segment(node: ast.stmt) -> str:
        start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])]) - 1
        return "".join(lines[start:node.end_lineno])

    used_by_kept: set[str] = set()
    for node in kept:
        used_by_kept |= referenced_names(node)
        used_by_kept |= {a.attr for a in ast.walk(node) if isinstance(a, ast.Attribute)}
        used_by_kept |= {
            a.value.id for a in ast.walk(node)
            if isinstance(a, ast.Attribute) and isinstance(a.value, ast.Name)
        }

    kept_imports = []
    for node in imports:
        alias_names = {(a.asname or a.name).split(".")[0] for a in node.names}
        if alias_names & used_by_kept:
            kept_imports.append(segment(node))

    dropped = len(tree.body) - len(kept) - len(imports)
    provenance = (
        f"Upstream: {len(lines)} lines, {len(tree.body)} top-level statements.\n"
        f"Vendored: {len(kept)} kept, {dropped} dropped as unreachable."
    )
    body = HEADER.format(provenance=provenance)
    body += "\nfrom __future__ import annotations\n\n"
    body += "".join(kept_imports)
    body += "\n\n"
    body += "\n".join(segment(node).rstrip("\n") + "\n" for node in kept)

    if args.check:
        current = VENDORED.read_text(encoding="utf-8") if VENDORED.is_file() else ""
        if current != body:
            print("vendored evd_tool.py is out of date; run scripts/vendor_evd_tool.py")
            return 1
        print("vendored evd_tool.py is up to date")
        return 0

    VENDORED.write_text(body, encoding="utf-8")
    print(f"{VENDORED.relative_to(PROJECT)}: {len(body.splitlines())} lines "
          f"({len(lines)} upstream), {len(kept)} of {len(tree.body)} statements kept")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
