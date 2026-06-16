"""
Code graph extractor: parse files with tree-sitter, emit symbols (nodes)
and call/import edges to Postgres.

Resolution strategy: name-only with import hints. Edges store the raw
callee_name; to_symbol is resolved in a second pass after all symbols
are inserted (so cross-file resolution works).
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from psycopg import sql

from ..chunking.code import _get_parser, _SYMBOL_TYPES, _get_symbol_name, _get_class_context
from .graph_db import get_conn, setup_schema, reset_graph

# Per-language node types that represent a call site, and how to read
# the callee name + receiver from them.
# Format: ext → list of (call_node_type, name_path, receiver_path)
#   name_path / receiver_path are sequences of child field/type names to follow.
_CALL_PATTERNS: dict[str, list[tuple[str, ...]]] = {
    ".java": [
        ("method_invocation",),
        ("object_creation_expression",),
    ],
    ".py": [
        ("call",),
    ],
    ".go": [
        ("call_expression",),
    ],
    ".js": [("call_expression",)],
    ".jsx": [("call_expression",)],
    ".ts": [("call_expression",), ("new_expression",)],
    ".tsx": [("call_expression",), ("new_expression",)],
    ".rs": [
        ("call_expression",),
        ("macro_invocation",),
    ],
    ".cpp": [("call_expression",)],
}
for _e in (".cc", ".cxx", ".hpp", ".hh", ".h", ".c"):
    _CALL_PATTERNS[_e] = _CALL_PATTERNS[".cpp"]

# Per-language import statement node types and how to extract the imported path
_IMPORT_TYPES = {
    ".java": ("import_declaration",),
    ".py": ("import_statement", "import_from_statement"),
    ".go": ("import_declaration",),
    ".js": ("import_statement",),
    ".jsx": ("import_statement",),
    ".ts": ("import_statement",),
    ".tsx": ("import_statement",),
    ".rs": ("use_declaration",),
    ".cpp": ("preproc_include",),
}
for _e in (".cc", ".cxx", ".hpp", ".hh", ".h", ".c"):
    _IMPORT_TYPES[_e] = _IMPORT_TYPES[".cpp"]

_JAVA_PACKAGE_RE = re.compile(r"^package\s+([\w.]+)\s*;", re.MULTILINE)


@dataclass
class Symbol:
    repo: str
    filepath: str
    name: str
    kind: str
    class_name: str
    fqn: str
    language: str
    start_line: int
    end_line: int
    db_id: int | None = None


@dataclass
class Edge:
    from_symbol: Symbol
    relation: str
    callee_name: str
    callee_class: str
    line: int


# ─────────────────────────────────────────────────────────────────────
# Extraction
# ─────────────────────────────────────────────────────────────────────

def _node_text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="ignore")


def _extract_imports(root, ext: str, source: bytes) -> dict[str, str]:
    """
    Returns map of {short_name: fully_qualified_path}.
    e.g. import com.example.util.CryptoUtils → {"CryptoUtils": "com.example.util.CryptoUtils"}
    """
    imports: dict[str, str] = {}
    types = _IMPORT_TYPES.get(ext, ())

    def walk(node):
        if node.type in types:
            text = _node_text(node, source).strip()
            # Strip leading keyword and trailing semicolon/quotes
            for kw in ("import static", "import", "use", "from", "#include"):
                if text.startswith(kw):
                    text = text[len(kw):].strip()
                    break
            text = text.rstrip(";").strip().strip('"').strip("<>")
            if not text or text.endswith("*"):
                return
            short = text.split(".")[-1].split("::")[-1].split("/")[-1]
            imports[short] = text
            return
        for child in node.children:
            walk(child)

    walk(root)
    return imports


def _extract_callee(call_node, ext: str, source: bytes) -> tuple[str, str]:
    """
    Given a call-site AST node, return (callee_name, receiver).
    e.g. CryptoUtils.signCSR(...) → ("signCSR", "CryptoUtils")
         foo()                    → ("foo", "")
    """
    name = ""
    receiver = ""

    if ext == ".java":
        if call_node.type == "method_invocation":
            n = call_node.child_by_field_name("name")
            o = call_node.child_by_field_name("object")
            name = _node_text(n, source) if n else ""
            receiver = _node_text(o, source) if o else ""
        elif call_node.type == "object_creation_expression":
            t = call_node.child_by_field_name("type")
            name = _node_text(t, source) if t else ""
    elif ext == ".py":
        fn = call_node.child_by_field_name("function")
        if fn is None:
            return "", ""
        if fn.type == "attribute":
            o = fn.child_by_field_name("object")
            a = fn.child_by_field_name("attribute")
            receiver = _node_text(o, source) if o else ""
            name = _node_text(a, source) if a else ""
        else:
            name = _node_text(fn, source)
    elif ext == ".go":
        fn = call_node.child_by_field_name("function")
        if fn is None:
            return "", ""
        if fn.type == "selector_expression":
            o = fn.child_by_field_name("operand")
            f = fn.child_by_field_name("field")
            receiver = _node_text(o, source) if o else ""
            name = _node_text(f, source) if f else ""
        else:
            name = _node_text(fn, source)
    elif ext in (".js", ".jsx", ".ts", ".tsx"):
        fn = call_node.child_by_field_name("function") or call_node.child_by_field_name("constructor")
        if fn is None:
            return "", ""
        if fn.type == "member_expression":
            o = fn.child_by_field_name("object")
            p = fn.child_by_field_name("property")
            receiver = _node_text(o, source) if o else ""
            name = _node_text(p, source) if p else ""
        else:
            name = _node_text(fn, source)
    elif ext == ".rs":
        if call_node.type == "macro_invocation":
            m = call_node.child_by_field_name("macro")
            name = _node_text(m, source) if m else ""
        else:
            fn = call_node.child_by_field_name("function")
            if fn is None:
                return "", ""
            if fn.type in ("scoped_identifier", "field_expression"):
                # Foo::bar or self.bar
                txt = _node_text(fn, source)
                parts = re.split(r"::|\.", txt)
                name = parts[-1]
                receiver = parts[-2] if len(parts) > 1 else ""
            else:
                name = _node_text(fn, source)
    else:  # cpp / c
        fn = call_node.child_by_field_name("function")
        if fn is None:
            return "", ""
        if fn.type in ("field_expression", "qualified_identifier"):
            txt = _node_text(fn, source)
            parts = re.split(r"::|->|\.", txt)
            name = parts[-1]
            receiver = parts[-2] if len(parts) > 1 else ""
        else:
            name = _node_text(fn, source)

    # Strip generics / template args
    name = re.sub(r"<.*>$", "", name).strip()
    receiver = re.sub(r"<.*>$", "", receiver).strip()
    return name, receiver


def _walk_calls(node, ext: str, source: bytes, results: list):
    call_types = {p[0] for p in _CALL_PATTERNS.get(ext, [])}
    if node.type in call_types:
        name, receiver = _extract_callee(node, ext, source)
        if name and not name.startswith(("(", "[")):
            results.append((name, receiver, node.start_point[0] + 1))
        # don't return — calls can nest
    for child in node.children:
        _walk_calls(child, ext, source, results)


def _build_fqn(package: str, class_name: str, name: str, kind: str) -> str:
    parts = [p for p in (package, class_name, name) if p]
    return ".".join(parts)


def extract_file(filepath: str, text: str, repo: str, ext: str) -> tuple[list[Symbol], list[Edge]]:
    parser = _get_parser(ext)
    if not parser:
        return [], []

    source = text.encode("utf-8")
    tree = parser.parse(source)
    root = tree.root_node

    symbol_types = _SYMBOL_TYPES.get(ext, [])
    if not symbol_types:
        return [], []

    package = ""
    if ext == ".java":
        m = _JAVA_PACKAGE_RE.search(text)
        if m:
            package = m.group(1)

    imports = _extract_imports(root, ext, source)

    symbols: list[Symbol] = []
    edges: list[Edge] = []

    def collect(node):
        if node.type in symbol_types:
            name = _get_symbol_name(node, source)
            if not name:
                return
            class_name = _get_class_context(node, source)
            kind = _normalize_kind(node.type)
            sym = Symbol(
                repo=repo,
                filepath=filepath,
                name=name,
                kind=kind,
                class_name=class_name,
                fqn=_build_fqn(package, class_name, name, kind),
                language=ext.lstrip("."),
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
            )
            symbols.append(sym)

            # Extract calls within this symbol's body (for callable kinds)
            if kind in ("method", "function", "constructor"):
                calls: list = []
                _walk_calls(node, ext, source, calls)
                for callee_name, receiver, line in calls:
                    callee_class = imports.get(receiver, receiver)
                    edges.append(Edge(
                        from_symbol=sym,
                        relation="calls",
                        callee_name=callee_name,
                        callee_class=callee_class,
                        line=line,
                    ))

            # Recurse into containers (classes, impls) to find nested methods
            if kind in ("class", "interface", "struct", "enum", "trait", "impl", "namespace", "module"):
                for child in node.children:
                    collect(child)
            return

        for child in node.children:
            collect(child)

    collect(root)
    return symbols, edges


def _normalize_kind(node_type: str) -> str:
    mapping = {
        "method_declaration": "method",
        "constructor_declaration": "constructor",
        "function_definition": "function",
        "function_declaration": "function",
        "function_item": "function",
        "class_declaration": "class",
        "class_definition": "class",
        "class_specifier": "class",
        "interface_declaration": "interface",
        "struct_specifier": "struct",
        "struct_item": "struct",
        "enum_declaration": "enum",
        "enum_item": "enum",
        "trait_item": "trait",
        "impl_item": "impl",
        "mod_item": "module",
        "namespace_definition": "namespace",
        "type_declaration": "type",
        "template_declaration": "template",
        "export_statement": "export",
        "lexical_declaration": "const",
        "type_alias_declaration": "type",
    }
    return mapping.get(node_type, node_type)


# ─────────────────────────────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────────────────────────────

def delete_graph_for(repo: str, filepath: str | None = None) -> None:
    """
    Delete symbols (and via FK: outgoing edges CASCADE, inbound edges
    SET NULL → re-resolved later) for a whole repo or a single file.
    """
    with get_conn().cursor() as cur:
        if filepath:
            cur.execute(
                "DELETE FROM code_symbols WHERE repo = %s AND filepath = %s",
                (repo, filepath),
            )
        else:
            cur.execute("DELETE FROM code_symbols WHERE repo = %s", (repo,))


def index_files_graph(repo_path: Path, repo_name: str, files: list[str]) -> tuple[int, int]:
    """Extract symbols + edges for a specific list of files (relative paths)."""
    all_symbols: list[Symbol] = []
    all_edges: list[Edge] = []

    for rel in files:
        fpath = repo_path / rel
        ext = fpath.suffix.lower()
        if ext not in _CALL_PATTERNS or not fpath.exists():
            continue
        try:
            text = fpath.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if not text.strip():
            continue
        syms, edges = extract_file(rel, text, repo_name, ext)
        all_symbols.extend(syms)
        all_edges.extend(edges)

    _persist(all_symbols, all_edges)
    return len(all_symbols), len(all_edges)


def index_repo_graph(repo_path: Path, repo_name: str, extensions: set[str]) -> tuple[int, int]:
    """Walk a repo, extract symbols + edges, insert into Postgres."""
    all_symbols: list[Symbol] = []
    all_edges: list[Edge] = []

    for fpath in repo_path.rglob("*"):
        if not fpath.is_file():
            continue
        ext = fpath.suffix.lower()
        if ext not in _CALL_PATTERNS:
            continue
        if any(part.startswith(".") for part in fpath.relative_to(repo_path).parts):
            continue
        if any(p in fpath.parts for p in ("vendor", "node_modules", "build", "target")):
            continue

        try:
            text = fpath.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if not text.strip():
            continue

        rel = str(fpath.relative_to(repo_path))
        syms, edges = extract_file(rel, text, repo_name, ext)
        all_symbols.extend(syms)
        all_edges.extend(edges)

    _persist(all_symbols, all_edges)
    return len(all_symbols), len(all_edges)


def _persist(symbols: list[Symbol], edges: list[Edge]) -> None:
    conn = get_conn()
    with conn.cursor() as cur:
        # Insert symbols, capture ids
        for s in symbols:
            cur.execute(
                """INSERT INTO code_symbols
                   (repo, filepath, name, kind, class_name, fqn, language, start_line, end_line)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (s.repo, s.filepath, s.name, s.kind, s.class_name, s.fqn,
                 s.language, s.start_line, s.end_line),
            )
            s.db_id = cur.fetchone()["id"]

        # Insert edges (to_symbol left NULL — resolved in second pass)
        for e in edges:
            cur.execute(
                """INSERT INTO code_edges
                   (from_symbol, relation, callee_name, callee_class, to_symbol, line)
                   VALUES (%s,%s,%s,%s,NULL,%s)""",
                (e.from_symbol.db_id, e.relation, e.callee_name, e.callee_class, e.line),
            )


def resolve_edges() -> int:
    """
    Second pass: link edges to symbols by name (+ class hint when available).
    Run after all repos are indexed so cross-repo resolution works.
    """
    conn = get_conn()
    with conn.cursor() as cur:
        # Prefer match on (name, class_name) when callee_class is a known class
        cur.execute("""
            UPDATE code_edges e
            SET to_symbol = s.id
            FROM code_symbols s
            WHERE e.to_symbol IS NULL
              AND e.relation = 'calls'
              AND s.name = e.callee_name
              AND s.kind IN ('method','function','constructor')
              AND (
                    e.callee_class = '' OR e.callee_class IS NULL
                    OR s.class_name = e.callee_class
                    OR s.class_name = split_part(e.callee_class, '.', -1)
                  )
        """)
        # Fallback: name-only match for anything still unresolved, but only
        # where a matching symbol actually exists.
        cur.execute("""
            UPDATE code_edges e
            SET to_symbol = s.id
            FROM (
                SELECT DISTINCT ON (name) id, name
                FROM code_symbols
                WHERE kind IN ('method','function','constructor')
                ORDER BY name, id
            ) s
            WHERE e.to_symbol IS NULL
              AND e.relation = 'calls'
              AND s.name = e.callee_name
        """)
        cur.execute("SELECT count(*) AS n FROM code_edges WHERE to_symbol IS NOT NULL")
        return cur.fetchone()["n"]


def build_graph_for_repos(repos_dir: Path) -> dict:
    """Entry point: reset graph, index all repos, resolve edges."""
    setup_schema()
    reset_graph()

    stats = {"symbols": 0, "edges": 0, "resolved": 0, "repos": {}}
    repos = [d for d in repos_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]

    for repo_path in repos:
        n_sym, n_edge = index_repo_graph(repo_path, repo_path.name, set())
        stats["symbols"] += n_sym
        stats["edges"] += n_edge
        stats["repos"][repo_path.name] = {"symbols": n_sym, "edges": n_edge}

    stats["resolved"] = resolve_edges()
    return stats
