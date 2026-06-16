"""
AST-aware code chunker using tree-sitter.
Splits on function/method/class boundaries. One semantic unit = one chunk.
"""

import tiktoken
from tree_sitter import Language, Parser

_MAX_CHUNK_TOKENS = 1500
_tokenizer_inst = None


def _tokenizer():
    global _tokenizer_inst
    if _tokenizer_inst is None:
        _tokenizer_inst = tiktoken.get_encoding("cl100k_base")
    return _tokenizer_inst

# Language grammars — imported on demand
_LANGUAGES: dict[str, Language] = {}
_PARSERS: dict[str, Parser] = {}

# Map extension → (module_name, Language object constructor)
_GRAMMAR_MAP = {
    ".java": "tree_sitter_java",
    ".py": "tree_sitter_python",
    ".go": "tree_sitter_go",
    ".js": "tree_sitter_javascript",
    ".jsx": "tree_sitter_javascript",
    ".ts": "tree_sitter_typescript",
    ".tsx": "tree_sitter_typescript",
    ".rs": "tree_sitter_rust",
    ".cpp": "tree_sitter_cpp",
    ".cc": "tree_sitter_cpp",
    ".cxx": "tree_sitter_cpp",
    ".hpp": "tree_sitter_cpp",
    ".hh": "tree_sitter_cpp",
    ".h": "tree_sitter_cpp",
    ".c": "tree_sitter_cpp",
    ".tf": "tree_sitter_hcl",
    ".tfvars": "tree_sitter_hcl",
    ".hcl": "tree_sitter_hcl",
}

# Tree-sitter node types that represent top-level semantic units per language
_SYMBOL_TYPES = {
    ".java": [
        "method_declaration",
        "constructor_declaration",
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
    ],
    ".py": [
        "function_definition",
        "class_definition",
    ],
    ".go": [
        "function_declaration",
        "method_declaration",
        "type_declaration",
    ],
    ".js": [
        "function_declaration",
        "class_declaration",
        "export_statement",
        "lexical_declaration",
    ],
    ".jsx": [
        "function_declaration",
        "class_declaration",
        "export_statement",
        "lexical_declaration",
    ],
    ".ts": [
        "function_declaration",
        "class_declaration",
        "export_statement",
        "lexical_declaration",
        "interface_declaration",
        "type_alias_declaration",
    ],
    ".tsx": [
        "function_declaration",
        "class_declaration",
        "export_statement",
        "lexical_declaration",
        "interface_declaration",
        "type_alias_declaration",
    ],
    ".rs": [
        "function_item",
        "impl_item",
        "struct_item",
        "enum_item",
        "trait_item",
        "mod_item",
    ],
    ".cpp": [
        "function_definition",
        "class_specifier",
        "struct_specifier",
        "namespace_definition",
        "template_declaration",
    ],
}
# C/C++ extensions share the same symbol types
for _ext in (".cc", ".cxx", ".hpp", ".hh", ".h", ".c"):
    _SYMBOL_TYPES[_ext] = _SYMBOL_TYPES[".cpp"]

# HCL/Terraform: every resource/module/variable/output/provider/data
# is a `block` node. One chunk per top-level block; name derived from
# the block type + its string labels (see _get_symbol_name).
_SYMBOL_TYPES[".tf"] = ["block"]
for _ext in (".tfvars", ".hcl"):
    _SYMBOL_TYPES[_ext] = _SYMBOL_TYPES[".tf"]


def _get_parser(ext: str) -> Parser | None:
    if ext in _PARSERS:
        return _PARSERS[ext]

    module_name = _GRAMMAR_MAP.get(ext)
    if not module_name:
        return None

    try:
        import importlib
        mod = importlib.import_module(module_name)
        lang = Language(mod.language())
        parser = Parser(lang)
        _LANGUAGES[ext] = lang
        _PARSERS[ext] = parser
        return parser
    except (ImportError, AttributeError):
        return None


_NAME_NODE_TYPES = (
    "identifier", "name", "property_identifier",
    "field_identifier", "namespace_identifier",
)


def _get_symbol_name(node, source_bytes: bytes) -> str:
    # HCL/Terraform `block`: name = "<type>.<label1>.<label2>", e.g.
    #   resource "google_service_account" "sa" {…} → resource.google_service_account.sa
    #   provider "google" {…}                      → provider.google
    #   terraform {…}                              → terraform
    # Children before `body` are: one `identifier` (the type) then 0-N
    # `string_lit` labels.
    if node.type == "block":
        parts: list[str] = []
        for child in node.children:
            if child.type == "body" or child.type == "block_start":
                break
            if child.type in ("identifier", "string_lit",
                              "quoted_template"):
                txt = source_bytes[child.start_byte:child.end_byte].decode(
                    "utf-8", errors="ignore").strip().strip('"')
                if txt:
                    parts.append(txt)
        return ".".join(parts)

    # Prefer the grammar's named 'name' field — this skips return types,
    # modifiers, etc. and goes straight to the identifier.
    name_node = node.child_by_field_name("name")
    if name_node is not None:
        return source_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="ignore")

    # C++: function_definition → declarator: function_declarator → declarator: identifier
    decl = node.child_by_field_name("declarator")
    if decl is not None:
        return _get_symbol_name(decl, source_bytes)

    # Rust impl_item: type field holds the struct/trait name
    type_node = node.child_by_field_name("type")
    if type_node is not None and node.type in ("impl_item", "type_declaration"):
        return source_bytes[type_node.start_byte:type_node.end_byte].decode("utf-8", errors="ignore")

    # Fallback: first identifier-like child (NOT type_identifier — that's a return type)
    for child in node.children:
        if child.type in _NAME_NODE_TYPES:
            return source_bytes[child.start_byte:child.end_byte].decode("utf-8", errors="ignore")
        if child.type == "qualified_identifier":
            return source_bytes[child.start_byte:child.end_byte].decode("utf-8", errors="ignore")
    return ""


_CONTAINER_TYPES = (
    "class_declaration", "class_definition", "interface_declaration",
    "class_specifier", "struct_specifier", "namespace_definition",
    "impl_item", "trait_item", "struct_item", "mod_item",
)


def _get_class_context(node, source_bytes: bytes) -> str:
    parent = node.parent
    while parent:
        if parent.type in _CONTAINER_TYPES:
            return _get_symbol_name(parent, source_bytes)
        parent = parent.parent
    return ""


def _collect_symbols(node, symbol_types: list[str], results: list):
    if node.type in symbol_types:
        results.append(node)
        return
    for child in node.children:
        _collect_symbols(child, symbol_types, results)


def _split_large_chunk(text: str, filepath: str, repo: str, base_meta: dict) -> list[dict]:
    """If a symbol exceeds _MAX_CHUNK_TOKENS, split with signature prefix on each sub-chunk."""
    tokens = _tokenizer().encode(text)
    if len(tokens) <= _MAX_CHUNK_TOKENS:
        return [{"text": text, "metadata": base_meta}]

    lines = text.split("\n")
    signature = lines[0] if lines else ""
    prefix = f"// Context: {filepath} | {base_meta.get('symbol_name', '')}\n{signature}\n// ...\n"

    chunks = []
    chunk_size = _MAX_CHUNK_TOKENS - len(_tokenizer().encode(prefix))
    start = 0
    idx = 0
    while start < len(tokens):
        end = start + chunk_size
        chunk_tokens = tokens[start:end]
        chunk_text = _tokenizer().decode(chunk_tokens)

        if idx > 0:
            chunk_text = prefix + chunk_text

        meta = {**base_meta, "chunk_part": idx}
        chunks.append({"text": chunk_text, "metadata": meta})
        start = end
        idx += 1

    return chunks


def chunk_code_file(filepath: str, text: str, repo: str, ext: str) -> list[dict]:
    parser = _get_parser(ext)
    if not parser:
        return []

    source_bytes = text.encode("utf-8")
    tree = parser.parse(source_bytes)

    symbol_types = _SYMBOL_TYPES.get(ext, [])
    if not symbol_types:
        return []

    symbols = []
    _collect_symbols(tree.root_node, symbol_types, symbols)

    if not symbols:
        return []

    chunks = []
    for node in symbols:
        chunk_text = source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="ignore")

        if not chunk_text.strip():
            continue

        symbol_name = _get_symbol_name(node, source_bytes)
        class_name = _get_class_context(node, source_bytes)

        # Prepend context line for embedding enrichment
        context_prefix = f"// File: {filepath} | {node.type}: {symbol_name}"
        if class_name:
            context_prefix += f" | Class: {class_name}"
        enriched_text = f"{context_prefix}\n{chunk_text}"

        base_meta = {
            "repo": repo,
            "filepath": filepath,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "symbol_name": symbol_name,
            "class_name": class_name,
            "symbol_type": node.type,
            "language": ext.lstrip("."),
            "chunk_type": "ast_symbol",
        }

        sub_chunks = _split_large_chunk(enriched_text, filepath, repo, base_meta)
        chunks.extend(sub_chunks)

    return chunks
