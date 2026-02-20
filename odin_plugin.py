"""
Odin Language Plugin for Sublime Text 4

Provides: autocomplete with signatures, go-to-definition, find references,
hover info, struct field completion, enum variant completion (including
implicit selectors).
"""

import sublime
import sublime_plugin
import os
import re
import threading
import time
from collections import defaultdict


# ============================================================================
# Data Structures
# ============================================================================

class Symbol:
    """An Odin symbol: proc, struct, enum, union, type, const, or var."""
    __slots__ = (
        'name', 'kind', 'signature', 'file', 'line', 'col', 'package_dir',
        'package_name', 'fields', 'variants', 'params', 'return_type',
        'is_private', 'underlying_enum', 'using_types',
    )

    def __init__(self, name, kind, **kw):
        self.name = name
        self.kind = kind
        self.signature = kw.get('signature', '')
        self.file = kw.get('file', '')
        self.line = kw.get('line', 0)
        self.col = kw.get('col', 0)
        self.package_dir = kw.get('package_dir', '')
        self.package_name = kw.get('package_name', '')
        self.fields = kw.get('fields', {})        # {name: type_str}
        self.variants = kw.get('variants', [])     # [name, ...]
        self.params = kw.get('params', [])         # [(name, type_str), ...]
        self.return_type = kw.get('return_type', '')
        self.is_private = kw.get('is_private', False)
        self.underlying_enum = kw.get('underlying_enum', '')
        self.using_types = kw.get('using_types', [])


class ImportInfo:
    """An import statement."""
    __slots__ = ('alias', 'collection', 'rel_path', 'resolved_dir')

    def __init__(self, alias, collection, rel_path, resolved_dir=''):
        self.alias = alias
        self.collection = collection
        self.rel_path = rel_path
        self.resolved_dir = resolved_dir


# ============================================================================
# Utility
# ============================================================================

def _split_balanced(text, sep=','):
    """Split text by sep, respecting balanced parens/brackets/braces."""
    parts = []
    current = []
    depth = 0
    for ch in text:
        if ch in '([{':
            depth += 1
        elif ch in ')]}':
            depth -= 1
        if ch == sep and depth == 0:
            parts.append(''.join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append(''.join(current))
    return parts


def _find_colon_depth0(text):
    """Find index of first ':' at bracket depth 0, or -1."""
    depth = 0
    for i, ch in enumerate(text):
        if ch in '([{':
            depth += 1
        elif ch in ')]}':
            depth -= 1
        elif ch == ':' and depth == 0:
            return i
    return -1


def _normalize_path(p):
    return p.replace('\\', '/')


# ============================================================================
# Parser
# ============================================================================

def _parse_struct_fields(body_lines):
    """Parse struct field lines -> (fields_dict, using_types_list)."""
    fields = {}
    using_types = []

    # Join lines into one comma-separated string
    parts_text = ', '.join(
        line.strip().rstrip(',')
        for line in body_lines
        if line.strip() and not line.strip().startswith('//')
    )
    parts = _split_balanced(parts_text)

    pending_names = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        is_using = False
        if part.startswith('using '):
            is_using = True
            part = part[6:].strip()

        colon_pos = _find_colon_depth0(part)
        if colon_pos >= 0:
            names_str = part[:colon_pos].strip()
            type_str = part[colon_pos + 1:].strip()
            all_names = pending_names + [n.strip() for n in names_str.split(',') if n.strip()]
            for name in all_names:
                fields[name] = type_str
            if is_using:
                using_types.append(type_str)
            pending_names = []
        else:
            pending_names.append(part)

    return fields, using_types


def _parse_enum_variants(body_lines):
    """Parse enum body lines -> [variant_name, ...]"""
    variants = []
    # Join all lines and split by comma to handle single-line enums
    all_text = ', '.join(
        line.strip().rstrip(',')
        for line in body_lines
        if line.strip() and not line.strip().startswith('//')
    )
    for part in _split_balanced(all_text):
        part = part.strip()
        if not part or part.startswith('//'):
            continue
        # VARIANT or VARIANT = value
        m = re.match(r'^(\w+)', part)
        if m:
            variants.append(m.group(1))
    return variants


def _parse_proc_params(param_str):
    """Parse proc parameter string -> [(name, type_str), ...]"""
    params = []
    parts = _split_balanced(param_str.strip())
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # name := default (type inferred)
        if ':=' in part:
            eq_pos = part.index(':=')
            name = part[:eq_pos].strip()
            # Try to figure out type from default value... usually not possible
            params.append((name, ''))
            continue
        # name: Type or name: Type = default
        colon_pos = _find_colon_depth0(part)
        if colon_pos >= 0:
            names_str = part[:colon_pos].strip()
            rest = part[colon_pos + 1:].strip()
            # Remove default value (= ...) at depth 0
            type_str = rest
            eq_depth = 0
            for i, ch in enumerate(rest):
                if ch in '([{':
                    eq_depth += 1
                elif ch in ')]}':
                    eq_depth -= 1
                elif ch == '=' and eq_depth == 0:
                    type_str = rest[:i].strip()
                    break
            for name in names_str.split(','):
                name = name.strip()
                if name:
                    params.append((name, type_str))
    return params


def _extract_proc_signature(full_text):
    """
    From a full proc declaration line (possibly joined from multiple lines),
    extract: signature_display, params_list, return_type.

    Input looks like: name :: [#force_inline] proc ["cc"] (params) [-> ret] {
    """
    # Find the opening paren of the param list
    # Skip past proc keyword and optional calling convention
    proc_match = re.search(r'proc\s*(?:"[^"]*"\s*)?\(', full_text)
    if not proc_match:
        return full_text, [], ''

    paren_start = proc_match.end() - 1  # index of '('

    # Find matching closing paren
    depth = 0
    paren_end = paren_start
    for i in range(paren_start, len(full_text)):
        if full_text[i] == '(':
            depth += 1
        elif full_text[i] == ')':
            depth -= 1
            if depth == 0:
                paren_end = i
                break

    param_str = full_text[paren_start + 1:paren_end]
    params = _parse_proc_params(param_str)

    # Look for return type after closing paren
    after_params = full_text[paren_end + 1:].strip()
    return_type = ''
    if after_params.startswith('->'):
        ret_str = after_params[2:].strip()
        # Return type goes until '{' or end of string
        brace = ret_str.find('{')
        if brace >= 0:
            return_type = ret_str[:brace].strip()
        else:
            return_type = ret_str.strip()
        # Clean up trailing whitespace/commas
        return_type = return_type.rstrip(' ,')

    # Build display signature: everything up to (and including) return type, before '{'
    brace_pos = full_text.find('{', paren_end)
    if brace_pos >= 0:
        sig = full_text[:brace_pos].strip()
    else:
        sig = full_text.strip()

    return sig, params, return_type


def parse_file(filepath, content=None):
    """
    Parse an Odin file and extract symbols + imports.
    Returns (package_name, symbols_list, imports_list).
    """
    if content is None:
        try:
            with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
        except (IOError, OSError):
            return '', [], []

    lines = content.splitlines()
    package_name = ''
    symbols = []
    imports = []
    pkg_dir = _normalize_path(os.path.dirname(filepath))

    in_block_comment = False
    pending_private = False
    # Multi-line collection state
    collecting = None  # 'struct', 'enum', 'proc', 'union', 'skip_block'
    collect_sym = None
    collect_depth = 0
    collect_lines = []

    for line_num, raw_line in enumerate(lines, 1):
        stripped = raw_line.strip()

        # --- Block comments ---
        if in_block_comment:
            if '*/' in stripped:
                in_block_comment = False
                idx = stripped.index('*/') + 2
                stripped = stripped[idx:].strip()
                if not stripped:
                    continue
            else:
                continue

        if '/*' in stripped:
            ci = stripped.index('/*')
            if '*/' in stripped[ci:]:
                stripped = re.sub(r'/\*.*?\*/', '', stripped).strip()
            else:
                stripped = stripped[:ci].strip()
                in_block_comment = True
                if not stripped:
                    continue

        # Remove line comments (not inside strings)
        cc = stripped.find('//')
        if cc >= 0 and stripped[:cc].count('"') % 2 == 0:
            stripped = stripped[:cc].strip()

        if not stripped:
            continue

        # --- Collecting multi-line bodies ---
        if collecting == 'struct':
            for ch in stripped:
                if ch == '{': collect_depth += 1
                elif ch == '}': collect_depth -= 1
            if collect_depth <= 0:
                fields, using = _parse_struct_fields(collect_lines)
                collect_sym.fields = fields
                collect_sym.using_types = using
                collect_sym.signature = f'{collect_sym.name} :: struct'
                symbols.append(collect_sym)
                collecting = None
                continue
            collect_lines.append(stripped)
            continue

        if collecting == 'enum':
            for ch in stripped:
                if ch == '{': collect_depth += 1
                elif ch == '}': collect_depth -= 1
            if collect_depth <= 0:
                collect_sym.variants = _parse_enum_variants(collect_lines)
                symbols.append(collect_sym)
                collecting = None
                continue
            collect_lines.append(stripped)
            continue

        if collecting == 'proc':
            collect_lines.append(stripped)
            for ch in stripped:
                if ch == '(': collect_depth += 1
                elif ch == ')': collect_depth -= 1
            if collect_depth <= 0:
                full_sig = ' '.join(collect_lines)
                sig, params, ret = _extract_proc_signature(full_sig)
                collect_sym.signature = sig
                collect_sym.params = params
                collect_sym.return_type = ret
                symbols.append(collect_sym)
                collecting = None
            continue

        if collecting in ('union', 'skip_block'):
            for ch in stripped:
                if ch == '{': collect_depth += 1
                elif ch == '}': collect_depth -= 1
            if collect_depth <= 0:
                if collect_sym:
                    symbols.append(collect_sym)
                collecting = None
            continue

        # --- Package ---
        m = re.match(r'^package\s+(\w+)', stripped)
        if m:
            package_name = m.group(1)
            continue

        # --- Import ---
        m = re.match(r'^import\s+(?:(\w+)\s+)?"([^"]+)"', stripped)
        if m:
            alias = m.group(1) or ''
            path = m.group(2)
            collection, rel_path = '', path
            if ':' in path:
                collection, rel_path = path.split(':', 1)
            if not alias:
                alias = rel_path.rstrip('/').rsplit('/', 1)[-1]
            imports.append(ImportInfo(alias, collection, rel_path))
            continue

        # --- @private / @(...) attributes ---
        if stripped.startswith('@'):
            attr_match = re.match(r'^@(?:\w+|\([^)]*\))\s*', stripped)
            if attr_match:
                if 'private' in stripped[:attr_match.end()]:
                    pending_private = True
                rest_after_attr = stripped[attr_match.end():]
                if not rest_after_attr:
                    continue  # attribute on its own line
                stripped = rest_after_attr

        # --- Declaration: NAME :: ... ---
        m = re.match(r'^(\w+)\s*::\s*(.*)', stripped)
        if m:
            name = m.group(1)
            rest = m.group(2).strip()
            is_priv = pending_private
            pending_private = False

            # Find column of the symbol name in the raw line (1-based)
            col = raw_line.find(name) + 1

            base_kw = dict(file=filepath, line=line_num, col=col,
                           package_dir=pkg_dir, package_name=package_name,
                           is_private=is_priv)

            # -- Proc group --
            if re.match(r'proc\s*\{', rest):
                symbols.append(Symbol(name, 'proc_group',
                    signature=f'{name} :: {rest}', **base_kw))
                continue

            # -- Proc --
            pm = re.match(r'(?:#\w+\s+)?proc\s*(?:"[^"]*"\s*)?\(', rest)
            if pm:
                depth = sum(1 if c == '(' else (-1 if c == ')' else 0) for c in rest)
                sym = Symbol(name, 'proc', **base_kw)
                if depth <= 0:
                    sig, params, ret = _extract_proc_signature(stripped)
                    sym.signature = sig
                    sym.params = params
                    sym.return_type = ret
                    symbols.append(sym)
                else:
                    collecting = 'proc'
                    collect_sym = sym
                    collect_depth = depth
                    collect_lines = [stripped]
                continue

            # -- Struct --
            sm = re.match(r'struct\s*(?:\([^)]*\)\s*)?\{', rest)
            if sm:
                sym = Symbol(name, 'struct', **base_kw)
                depth = rest.count('{') - rest.count('}')
                if depth <= 0:
                    body = re.search(r'\{(.*)\}', rest)
                    if body:
                        fields, using = _parse_struct_fields([body.group(1)])
                        sym.fields = fields
                        sym.using_types = using
                    sym.signature = f'{name} :: struct'
                    symbols.append(sym)
                else:
                    collecting = 'struct'
                    collect_sym = sym
                    collect_depth = depth
                    collect_lines = []
                continue

            # -- Enum --
            em = re.match(r'enum\s*(?:\w+\s*)?\{', rest)
            if em:
                sig_part = rest[:rest.index('{')].strip()
                sym = Symbol(name, 'enum',
                    signature=f'{name} :: {sig_part}', **base_kw)
                depth = rest.count('{') - rest.count('}')
                if depth <= 0:
                    body = re.search(r'\{(.*)\}', rest)
                    if body:
                        sym.variants = _parse_enum_variants([body.group(1)])
                    symbols.append(sym)
                else:
                    collecting = 'enum'
                    collect_sym = sym
                    collect_depth = depth
                    collect_lines = []
                continue

            # -- Union --
            if re.match(r'union\s*\{', rest):
                sym = Symbol(name, 'union',
                    signature=f'{name} :: union', **base_kw)
                depth = rest.count('{') - rest.count('}')
                if depth <= 0:
                    symbols.append(sym)
                else:
                    collecting = 'union'
                    collect_sym = sym
                    collect_depth = depth
                continue

            # -- bit_set --
            bm = re.match(r'(?:distinct\s+)?bit_set\[(\w+)', rest)
            if bm:
                symbols.append(Symbol(name, 'type',
                    signature=f'{name} :: {rest}',
                    underlying_enum=bm.group(1), **base_kw))
                continue

            # -- distinct type --
            if rest.startswith('distinct '):
                symbols.append(Symbol(name, 'type',
                    signature=f'{name} :: {rest}', **base_kw))
                continue

            # -- #config --
            if rest.startswith('#config('):
                symbols.append(Symbol(name, 'const',
                    signature=f'{name} :: {rest}', **base_kw))
                continue

            # -- Generic: constant, type alias, or value --
            # Check if it looks like a type (starts with uppercase or is a known
            # type keyword). We'll just store as 'const' generically.
            symbols.append(Symbol(name, 'const',
                signature=f'{name} :: {rest}', **base_kw))
            continue

        # --- Top-level variable: NAME := ... ---
        m = re.match(r'^(\w+)\s*:=\s*(.*)', stripped)
        if m:
            name = m.group(1)
            is_priv = pending_private
            pending_private = False
            rest = m.group(2)
            # If the value spans multiple lines (opens braces), skip the block
            depth = rest.count('{') - rest.count('}')
            sym = Symbol(name, 'var',
                signature=f'{name} := ...',
                file=filepath, line=line_num,
                package_dir=pkg_dir, package_name=package_name,
                is_private=is_priv)
            if depth > 0:
                collecting = 'skip_block'
                collect_sym = sym
                collect_depth = depth
            else:
                symbols.append(sym)
            continue

        pending_private = False

    return package_name, symbols, imports


# ============================================================================
# Index
# ============================================================================

class OdinIndex:
    def __init__(self):
        self._by_name = defaultdict(list)   # name -> [Symbol]
        self._by_pkg = defaultdict(dict)    # pkg_dir -> {name: Symbol}
        self._file_syms = {}                # filepath -> [Symbol]
        self._file_imports = {}             # filepath -> [ImportInfo]
        self._pkg_names = {}                # pkg_dir -> package_name
        self._odin_roots = {}               # project_folder -> odin_root
        self._lock = threading.RLock()

    def clear(self):
        with self._lock:
            self._by_name.clear()
            self._by_pkg.clear()
            self._file_syms.clear()
            self._file_imports.clear()
            self._pkg_names.clear()

    def remove_file(self, filepath):
        filepath = _normalize_path(filepath)
        with self._lock:
            old_syms = self._file_syms.pop(filepath, [])
            for sym in old_syms:
                lst = self._by_name.get(sym.name, [])
                self._by_name[sym.name] = [s for s in lst if s.file != filepath]
                pkg = self._by_pkg.get(sym.package_dir, {})
                if sym.name in pkg and pkg[sym.name].file == filepath:
                    del pkg[sym.name]
            self._file_imports.pop(filepath, None)

    def index_file(self, filepath, content=None):
        filepath = _normalize_path(filepath)
        pkg_name, syms, imps = parse_file(filepath, content)

        with self._lock:
            # Remove old data for this file
            self.remove_file(filepath)

            pkg_dir = _normalize_path(os.path.dirname(filepath))
            if pkg_name:
                self._pkg_names[pkg_dir] = pkg_name

            self._file_syms[filepath] = syms
            self._file_imports[filepath] = imps

            for sym in syms:
                self._by_name[sym.name].append(sym)
                self._by_pkg[sym.package_dir][sym.name] = sym

    def index_directory(self, dirpath, recursive=True):
        """Index all .odin files in a directory."""
        dirpath = _normalize_path(dirpath)
        if recursive:
            for root, dirs, files in os.walk(dirpath):
                # Skip hidden dirs and common non-source dirs
                dirs[:] = [d for d in dirs if not d.startswith('.')]
                for f in files:
                    if f.endswith('.odin'):
                        self.index_file(os.path.join(root, f))
        else:
            for f in os.listdir(dirpath):
                if f.endswith('.odin'):
                    self.index_file(os.path.join(dirpath, f))

    def find_odin_root(self, project_folder):
        """Find the Odin root (directory containing core/ and vendor/) for a project."""
        project_folder = _normalize_path(project_folder)
        if project_folder in self._odin_roots:
            return self._odin_roots[project_folder]

        # Search upward from project folder
        d = project_folder
        for _ in range(10):  # max 10 levels up
            if (os.path.isdir(os.path.join(d, 'core')) and
                    os.path.isdir(os.path.join(d, 'vendor'))):
                self._odin_roots[project_folder] = _normalize_path(d)
                return self._odin_roots[project_folder]
            # Also check for Odin subdirectory
            odin_sub = os.path.join(d, 'Odin')
            if (os.path.isdir(odin_sub) and
                    os.path.isdir(os.path.join(odin_sub, 'core'))):
                self._odin_roots[project_folder] = _normalize_path(odin_sub)
                return self._odin_roots[project_folder]
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
        return None

    def resolve_import_dir(self, filepath, imp):
        """Resolve an ImportInfo to an absolute directory path."""
        filepath = _normalize_path(filepath)
        file_dir = os.path.dirname(filepath)

        if imp.resolved_dir:
            return imp.resolved_dir

        if imp.collection:
            # Collection import: core:math -> {odin_root}/core/math
            # Find which project folder this file belongs to
            for folder in self._odin_roots:
                if filepath.startswith(folder):
                    root = self._odin_roots[folder]
                    resolved = _normalize_path(
                        os.path.join(root, imp.collection, imp.rel_path))
                    if os.path.isdir(resolved):
                        imp.resolved_dir = resolved
                        return resolved
            # Try all known roots
            for root in set(self._odin_roots.values()):
                resolved = _normalize_path(
                    os.path.join(root, imp.collection, imp.rel_path))
                if os.path.isdir(resolved):
                    imp.resolved_dir = resolved
                    return resolved
        else:
            # Relative import
            resolved = _normalize_path(
                os.path.normpath(os.path.join(file_dir, imp.rel_path)))
            if os.path.isdir(resolved):
                imp.resolved_dir = resolved
                return resolved

        return None

    def get_file_imports(self, filepath):
        """Get imports for a file, with resolved directories."""
        filepath = _normalize_path(filepath)
        return self._file_imports.get(filepath, [])

    def get_package_symbols(self, pkg_dir):
        """Get all symbols in a package directory."""
        pkg_dir = _normalize_path(pkg_dir)
        return self._by_pkg.get(pkg_dir, {})

    def get_symbols_by_name(self, name):
        """Get all symbols with a given name."""
        return self._by_name.get(name, [])

    def get_all_accessible_symbols(self, filepath):
        """Get all symbols accessible from a file (same package + imported)."""
        filepath = _normalize_path(filepath)
        file_dir = os.path.dirname(filepath)

        result = {}
        # Same package symbols
        pkg_syms = self._by_pkg.get(file_dir, {})
        result.update(pkg_syms)
        return result

    def lookup_type(self, type_name, from_file):
        """Look up a type by name, searching accessible packages."""
        # Strip pointer prefix
        clean = type_name.lstrip('^').strip()
        if not clean:
            return None

        # Check if it's qualified: pkg.Type
        if '.' in clean:
            pkg_alias, type_part = clean.split('.', 1)
            from_file = _normalize_path(from_file)
            for imp in self.get_file_imports(from_file):
                if imp.alias == pkg_alias:
                    pkg_dir = self.resolve_import_dir(from_file, imp)
                    if pkg_dir:
                        pkg_syms = self.get_package_symbols(pkg_dir)
                        return pkg_syms.get(type_part)
            return None

        # Search in current package first
        from_file = _normalize_path(from_file)
        file_dir = os.path.dirname(from_file)
        pkg_syms = self.get_package_symbols(file_dir)
        if clean in pkg_syms:
            return pkg_syms[clean]

        # Search imported packages
        for imp in self.get_file_imports(from_file):
            pkg_dir = self.resolve_import_dir(from_file, imp)
            if pkg_dir:
                pkg_syms = self.get_package_symbols(pkg_dir)
                if clean in pkg_syms:
                    return pkg_syms[clean]

        return None

    def resolve_fields(self, sym):
        """Get all fields for a struct, including using'd fields."""
        if not sym or sym.kind != 'struct':
            return {}
        fields = dict(sym.fields)
        for using_type in sym.using_types:
            parent = self.lookup_type(using_type, sym.file)
            if parent and parent.kind == 'struct':
                fields.update(self.resolve_fields(parent))
        return fields

    def resolve_enum_for_type(self, type_name, from_file):
        """
        Given a type name, resolve to the enum Symbol if it is an enum
        or a bit_set/distinct bit_set of an enum.
        """
        sym = self.lookup_type(type_name, from_file)
        if not sym:
            return None
        if sym.kind == 'enum':
            return sym
        if sym.kind == 'type' and sym.underlying_enum:
            return self.lookup_type(sym.underlying_enum, from_file)
        return None


# Global index
_index = OdinIndex()
_index_lock = threading.Lock()
_indexing = False

# Completion cache: (pkg_dir, file_mod_count) -> [CompletionItem]
_completion_cache = {}
_completion_cache_lock = threading.Lock()


# ============================================================================
# Background indexing
# ============================================================================

def _index_project_folders(window):
    global _indexing
    if _indexing:
        return
    _indexing = True

    def do_index():
        global _indexing
        try:
            folders = window.folders()
            for folder in folders:
                folder = _normalize_path(folder)
                # Find Odin root for this folder
                _index.find_odin_root(folder)
                # Index the project folder
                _index.index_directory(folder)

                # Index imported packages (stdlib/vendor)
                _index_imported_packages(folder)

            count = sum(len(v) for v in _index._by_name.values())
            sublime.status_message(f'Odin: Indexed {count} symbols')
        finally:
            _indexing = False

    threading.Thread(target=do_index, daemon=True).start()


def _index_imported_packages(project_folder):
    """Index stdlib/vendor packages that are actually imported."""
    project_folder = _normalize_path(project_folder)
    indexed_dirs = set()

    with _index._lock:
        all_imports = list(_index._file_imports.values())

    for imp_list in all_imports:
        for imp in imp_list:
            if not imp.collection:
                continue
            # This is a collection import (core:, vendor:, base:)
            # Try to resolve and index
            for filepath in list(_index._file_imports.keys()):
                resolved = _index.resolve_import_dir(filepath, imp)
                if resolved and resolved not in indexed_dirs:
                    indexed_dirs.add(resolved)
                    _index.index_directory(resolved, recursive=False)
                    break


# ============================================================================
# Type resolution helpers (for completions)
# ============================================================================

def _get_word_before_dot(view, point):
    """
    Given a point right after a '.', extract what's before the dot.
    Returns (prefix_text, chain_parts) where chain_parts is like ['ctx', 'style'].
    """
    # Walk backwards from the dot
    dot_pos = point - 1
    if dot_pos < 0 or view.substr(dot_pos) != '.':
        return '', []

    # Get the region before the dot
    line_region = view.line(dot_pos)
    line_start = line_region.begin()
    text_before = view.substr(sublime.Region(line_start, dot_pos))

    # Extract the chain: e.g. "ctx.style" from "  foo := ctx.style"
    # Walk backwards through identifiers and dots
    parts = []
    i = len(text_before) - 1
    while i >= 0:
        # Skip whitespace
        while i >= 0 and text_before[i] in ' \t':
            i -= 1
        if i < 0:
            break
        # Collect identifier
        if text_before[i] == '.' and parts:
            i -= 1
            continue
        end = i + 1
        while i >= 0 and (text_before[i].isalnum() or text_before[i] == '_'):
            i -= 1
        start = i + 1
        if start < end:
            parts.append(text_before[start:end])
        else:
            break
        # Check if there's a dot before this identifier
        if i >= 0 and text_before[i] == '.':
            i -= 1
            continue
        break

    parts.reverse()
    return '.'.join(parts), parts


def _resolve_type_chain(view, filepath, chain_parts):
    """
    Resolve a dotted chain like ['ctx', 'style'] to the final type Symbol.
    Returns the Symbol of the final type (struct for field completion, enum for variant).
    """
    if not chain_parts:
        return None

    first = chain_parts[0]

    # Check if first part is a package alias
    imports = _index.get_file_imports(filepath)
    for imp in imports:
        if imp.alias == first:
            if len(chain_parts) == 1:
                # User typed "pkg." - return the package dir for package completions
                return ('package', _index.resolve_import_dir(filepath, imp))
            # chain is pkg.something... - look up 'something' in that package
            pkg_dir = _index.resolve_import_dir(filepath, imp)
            if pkg_dir:
                pkg_syms = _index.get_package_symbols(pkg_dir)
                sym = pkg_syms.get(chain_parts[1])
                if sym:
                    if len(chain_parts) == 2:
                        return sym
                    # Continue resolving through struct fields
                    return _resolve_field_chain(sym, chain_parts[2:], filepath)
            return None

    # Check if first part is a known type (for Enum.VARIANT access)
    type_sym = _index.lookup_type(first, filepath)
    if type_sym and len(chain_parts) == 1:
        return type_sym

    # Try to find the variable declaration in the current file
    var_type = _find_variable_type(view, first)
    if var_type:
        sym = _index.lookup_type(var_type, filepath)
        if sym:
            if len(chain_parts) == 1:
                return sym
            return _resolve_field_chain(sym, chain_parts[1:], filepath)

    return None


def _resolve_field_chain(sym, remaining_parts, filepath):
    """Resolve remaining field chain from a struct symbol."""
    if not sym or sym.kind != 'struct':
        return None

    fields = _index.resolve_fields(sym)

    for i, part in enumerate(remaining_parts):
        if part not in fields:
            return None
        field_type = fields[part]
        field_sym = _index.lookup_type(field_type, filepath)
        if not field_sym:
            return None
        if i == len(remaining_parts) - 1:
            return field_sym
        if field_sym.kind == 'struct':
            fields = _index.resolve_fields(field_sym)
        else:
            return None
    return None


def _find_variable_type(view, var_name):
    """
    Search the current file for a variable declaration and return its type string.
    Checks proc params and local declarations.
    """
    # Get the content of the current view
    content = view.substr(sublime.Region(0, min(view.size(), 100000)))

    # Search for proc parameter: var_name: Type or var_name: ^Type
    # This is a simple heuristic - look for the pattern in proc signatures
    patterns = [
        # proc param: name: Type
        rf'\b{re.escape(var_name)}\s*:\s*([^^][^\s,)={{}}]+)',
        # proc param: name: ^Type
        rf'\b{re.escape(var_name)}\s*:\s*(\^[\w.]+)',
        # local var: name : Type =
        rf'\b{re.escape(var_name)}\s*:\s*([^\s=,){{}}]+)\s*[=:]',
    ]

    for pattern in patterns:
        m = re.search(pattern, content)
        if m:
            return m.group(1).strip()

    return None


def _find_expected_enum_type(view, point):
    """
    For implicit enum selectors (.VARIANT), determine the expected enum type
    from context. Returns the enum Symbol or None.
    """
    filepath = _normalize_path(view.file_name() or '')

    # Get text from start of line (or a bit before) to the cursor
    line_region = view.line(point)
    line_start = line_region.begin()
    # Get up to 5 lines before for multi-line function calls
    for _ in range(5):
        prev_line = view.line(max(0, line_start - 1))
        if prev_line.begin() == line_start:
            break
        line_start = prev_line.begin()

    text_before = view.substr(sublime.Region(line_start, point))

    # Case 1: Function argument - find enclosing function call and param index
    func_name, param_idx = _find_enclosing_call(text_before)
    if func_name:
        # Look up the function
        syms = _index.get_symbols_by_name(func_name)
        for sym in syms:
            if sym.kind == 'proc' and sym.params:
                if param_idx < len(sym.params):
                    param_type = sym.params[param_idx][1]
                    enum_sym = _index.resolve_enum_for_type(param_type, filepath)
                    if enum_sym:
                        return enum_sym

        # Also check package-qualified function calls
        # If func_name has a dot, it's already qualified
        if '.' in func_name:
            pkg_alias, fn = func_name.rsplit('.', 1)
            for imp in _index.get_file_imports(filepath):
                if imp.alias == pkg_alias:
                    pkg_dir = _index.resolve_import_dir(filepath, imp)
                    if pkg_dir:
                        pkg_syms = _index.get_package_symbols(pkg_dir)
                        sym = pkg_syms.get(fn)
                        if sym and sym.kind == 'proc' and sym.params:
                            if param_idx < len(sym.params):
                                param_type = sym.params[param_idx][1]
                                enum_sym = _index.resolve_enum_for_type(
                                    param_type, filepath)
                                if enum_sym:
                                    return enum_sym

    # Case 2: Assignment with type annotation - var: Type = .
    m = re.search(r':\s*([\w.^]+)\s*=\s*$', text_before)
    if m:
        type_name = m.group(1)
        return _index.resolve_enum_for_type(type_name, filepath)

    # Case 3: Comparison - if x == . or x != .
    m = re.search(r'(\w+)\s*[!=]=\s*$', text_before)
    if m:
        var_name = m.group(1)
        var_type = _find_variable_type(view, var_name)
        if var_type:
            return _index.resolve_enum_for_type(var_type, filepath)

    # Case 4: Struct literal field - { field = .
    m = re.search(r'(\w+)\s*=\s*$', text_before)
    if m and '{' in text_before:
        # TODO: resolve struct literal type and field type
        pass

    return None


def _find_enclosing_call(text):
    """
    Find the enclosing function call and parameter index.
    Returns (function_name, param_index) or (None, 0).
    """
    # Walk backwards through text, tracking paren depth
    depth = 0
    comma_count = 0
    i = len(text) - 1

    while i >= 0:
        ch = text[i]
        if ch == ')':
            depth += 1
        elif ch == '(':
            if depth == 0:
                # Found the opening paren of our call
                # Extract function name before this paren
                j = i - 1
                while j >= 0 and text[j] in ' \t':
                    j -= 1
                end = j + 1
                while j >= 0 and (text[j].isalnum() or text[j] in '_.'):
                    j -= 1
                func_name = text[j + 1:end].strip()
                if func_name:
                    return func_name, comma_count
                return None, 0
            depth -= 1
        elif ch == ',' and depth == 0:
            comma_count += 1
        elif ch == '{':
            depth -= 1  # Handle bit_set literals etc
        elif ch == '}':
            depth += 1
        i -= 1

    return None, 0


# ============================================================================
# Sublime Text Integration
# ============================================================================

KIND_PROC = (sublime.KIND_ID_FUNCTION, 'p', 'proc')
KIND_STRUCT = (sublime.KIND_ID_TYPE, 's', 'struct')
KIND_ENUM = (sublime.KIND_ID_TYPE, 'e', 'enum')
KIND_TYPE = (sublime.KIND_ID_TYPE, 't', 'type')
KIND_CONST = (sublime.KIND_ID_VARIABLE, 'c', 'const')
KIND_VAR = (sublime.KIND_ID_VARIABLE, 'v', 'var')
KIND_FIELD = (sublime.KIND_ID_VARIABLE, 'f', 'field')
KIND_VARIANT = (sublime.KIND_ID_MARKUP, 'E', 'enum')

KIND_MAP = {
    'proc': KIND_PROC,
    'proc_group': KIND_PROC,
    'struct': KIND_STRUCT,
    'enum': KIND_ENUM,
    'union': KIND_TYPE,
    'type': KIND_TYPE,
    'const': KIND_CONST,
    'var': KIND_VAR,
}


def _is_odin(view):
    if not view:
        return False
    fn = view.file_name()
    if fn and fn.endswith('.odin'):
        return True
    return view.match_selector(0, 'source.odin')


def _make_completion(sym):
    """Create a sublime.CompletionItem for a symbol."""
    kind = KIND_MAP.get(sym.kind, KIND_VAR)
    annotation = ''
    details = ''
    completion = sym.name

    if sym.kind == 'proc':
        # Show params in annotation
        if sym.params:
            param_strs = [f'{n}: {t}' if t else n for n, t in sym.params]
            annotation = f'({", ".join(param_strs)})'
            if sym.return_type:
                annotation += f' -> {sym.return_type}'
        else:
            annotation = '()'
            if sym.return_type:
                annotation += f' -> {sym.return_type}'

        # Just insert name( — no snippet placeholders
        completion = f'{sym.name}('
        details = _make_location_detail(sym)
        return sublime.CompletionItem(
            trigger=sym.name,
            completion=completion,
            annotation=annotation,
            kind=kind,
            details=details,
        )

    elif sym.kind == 'proc_group':
        annotation = 'proc group'
        completion = f'{sym.name}('
        details = _make_location_detail(sym)
        return sublime.CompletionItem(
            trigger=sym.name,
            completion=completion,
            annotation=annotation,
            kind=kind,
            details=details,
        )

    elif sym.kind == 'struct':
        annotation = 'struct'
        if sym.fields:
            field_names = list(sym.fields.keys())[:5]
            details = ', '.join(field_names)
            if len(sym.fields) > 5:
                details += ', ...'
    elif sym.kind == 'enum':
        annotation = 'enum'
        if sym.variants:
            details = ', '.join(sym.variants[:5])
            if len(sym.variants) > 5:
                details += ', ...'
    else:
        # Truncate long signatures for annotation
        sig = sym.signature
        if '::' in sig:
            annotation = sig.split('::', 1)[1].strip()[:60]

    if not details:
        details = _make_location_detail(sym)

    return sublime.CompletionItem(
        trigger=sym.name,
        annotation=annotation,
        completion=completion,
        kind=kind,
        details=details,
    )


def _get_cached_completions(file_dir, filepath):
    """Get or build cached completion list for a package directory."""
    global _completion_cache
    # Use the set of files in the index for this dir as cache key
    file_count = len(_index._file_syms)
    cache_key = (file_dir, file_count)

    with _completion_cache_lock:
        if cache_key in _completion_cache:
            return _completion_cache[cache_key]

    completions = []
    pkg_syms = _index.get_package_symbols(file_dir)
    for name, sym in pkg_syms.items():
        completions.append(_make_completion(sym))

    for imp in _index.get_file_imports(filepath):
        completions.append(sublime.CompletionItem(
            trigger=imp.alias,
            annotation='package',
            completion=imp.alias,
            kind=(sublime.KIND_ID_NAMESPACE, 'P', 'package'),
        ))

    with _completion_cache_lock:
        _completion_cache[cache_key] = completions

    return completions


def _invalidate_completion_cache():
    global _completion_cache
    with _completion_cache_lock:
        _completion_cache.clear()


def _make_location_detail(sym):
    """Make a details string showing source location."""
    if sym.file:
        basename = os.path.basename(sym.file)
        return f'<a href="file://{sym.file}">{basename}:{sym.line}</a>'
    return ''


class OdinEventListener(sublime_plugin.EventListener):

    def on_post_save_async(self, view):
        if not _is_odin(view):
            return
        filepath = view.file_name()
        if filepath:
            _index.index_file(filepath)
            _invalidate_completion_cache()
            # Re-index any newly imported packages
            threading.Thread(
                target=_index_imported_packages,
                args=(_normalize_path(os.path.dirname(filepath)),),
                daemon=True
            ).start()

    def on_query_completions(self, view, prefix, locations):
        if not _is_odin(view):
            return None

        point = locations[0]
        filepath = _normalize_path(view.file_name() or '')
        file_dir = os.path.dirname(filepath)

        completions = []

        # Get text before cursor on current line
        line_region = view.line(point)
        line_text = view.substr(sublime.Region(line_region.begin(), point))

        # Check for dot completion
        # Text before prefix: e.g. "jui." when prefix is "draw_"
        text_before_prefix = line_text[:len(line_text) - len(prefix)]
        stripped_before = text_before_prefix.rstrip()

        # Also detect: cursor is right after a '.' with no prefix yet
        char_before_cursor = view.substr(point - 1) if point > 0 else ''
        is_dot = stripped_before.endswith('.') or (not prefix and char_before_cursor == '.')

        if is_dot:
            # Dot completion
            before_dot = stripped_before[:-1].rstrip()

            # Is the dot preceded by nothing (implicit enum selector)?
            if not before_dot or before_dot[-1] in '(,= \t\n{!<>+&|':
                # Implicit enum selector
                enum_sym = _find_expected_enum_type(view, point)
                if enum_sym:
                    for variant in enum_sym.variants:
                        completions.append(sublime.CompletionItem(
                            trigger=variant,
                            annotation=enum_sym.name,
                            completion=variant,
                            kind=KIND_VARIANT,
                            details=_make_location_detail(enum_sym),
                        ))
                    return sublime.CompletionList(
                        completions,
                        flags=sublime.INHIBIT_WORD_COMPLETIONS | sublime.INHIBIT_EXPLICIT_COMPLETIONS)

            else:
                # Get the chain before the dot
                _, chain_parts = _get_word_before_dot(view, point - len(prefix))

                if chain_parts:
                    result = _resolve_type_chain(view, filepath, chain_parts)

                    if isinstance(result, tuple) and result[0] == 'package':
                        # Package completion
                        pkg_dir = result[1]
                        if pkg_dir:
                            prefix_lower = prefix.lower()
                            pkg_syms = _index.get_package_symbols(pkg_dir)
                            for name, sym in pkg_syms.items():
                                if not sym.is_private and (
                                        not prefix_lower or name.lower().startswith(prefix_lower)):
                                    completions.append(_make_completion(sym))
                            return sublime.CompletionList(
                                completions,
                                flags=sublime.INHIBIT_WORD_COMPLETIONS | sublime.INHIBIT_EXPLICIT_COMPLETIONS)

                    elif isinstance(result, Symbol):
                        if result.kind == 'struct':
                            # Struct field completion
                            fields = _index.resolve_fields(result)
                            for fname, ftype in fields.items():
                                completions.append(sublime.CompletionItem(
                                    trigger=fname,
                                    annotation=ftype,
                                    completion=fname,
                                    kind=KIND_FIELD,
                                ))
                            return sublime.CompletionList(
                                completions,
                                flags=sublime.INHIBIT_WORD_COMPLETIONS | sublime.INHIBIT_EXPLICIT_COMPLETIONS)

                        elif result.kind == 'enum':
                            # Enum variant completion
                            for variant in result.variants:
                                completions.append(sublime.CompletionItem(
                                    trigger=variant,
                                    annotation=result.name,
                                    completion=variant,
                                    kind=KIND_VARIANT,
                                ))
                            return sublime.CompletionList(
                                completions,
                                flags=sublime.INHIBIT_WORD_COMPLETIONS | sublime.INHIBIT_EXPLICIT_COMPLETIONS)

        # Regular (non-dot) completion: use cached completions, filter by prefix
        prefix_lower = prefix.lower()
        cached = _get_cached_completions(file_dir, filepath)
        if prefix_lower:
            completions = [c for c in cached if c.trigger.lower().startswith(prefix_lower)]
        else:
            completions = cached

        if completions:
            return sublime.CompletionList(completions)
        return None

    def on_hover(self, view, point, hover_zone):
        if hover_zone != sublime.HOVER_TEXT or not _is_odin(view):
            return

        filepath = _normalize_path(view.file_name() or '')

        # Get the word under the cursor
        word_region = view.word(point)
        word = view.substr(word_region)
        if not word or not word[0].isalpha() and word[0] != '_':
            return

        # Check for qualified name (pkg.symbol)
        # Look for a dot before the word
        before = view.substr(sublime.Region(max(0, word_region.begin() - 100),
                                             word_region.begin()))
        pkg_alias = ''
        if before.rstrip().endswith('.'):
            # Get the identifier before the dot
            before_dot = before.rstrip()[:-1].rstrip()
            m = re.search(r'(\w+)$', before_dot)
            if m:
                pkg_alias = m.group(1)

        sym = None
        if pkg_alias:
            for imp in _index.get_file_imports(filepath):
                if imp.alias == pkg_alias:
                    pkg_dir = _index.resolve_import_dir(filepath, imp)
                    if pkg_dir:
                        pkg_syms = _index.get_package_symbols(pkg_dir)
                        sym = pkg_syms.get(word)
                    break
        else:
            # Search current package first
            file_dir = os.path.dirname(filepath)
            pkg_syms = _index.get_package_symbols(file_dir)
            sym = pkg_syms.get(word)

            if not sym:
                # Search all symbols
                syms = _index.get_symbols_by_name(word)
                if syms:
                    sym = syms[0]

        if not sym:
            return

        # Build hover HTML
        html = _build_hover_html(sym)
        view.show_popup(
            html,
            flags=sublime.HIDE_ON_MOUSE_MOVE_AWAY,
            location=point,
            max_width=800,
            max_height=400,
        )


def _build_hover_html(sym):
    """Build HTML for hover popup."""
    sig = _html_escape(sym.signature or f'{sym.name} :: {sym.kind}')
    location = ''
    if sym.file:
        basename = os.path.basename(sym.file)
        pkg = sym.package_name or os.path.basename(os.path.dirname(sym.file))
        location = f'<div style="color: #888; margin-top: 4px;">{pkg} · {basename}:{sym.line}</div>'

    fields_html = ''
    if sym.kind == 'struct' and sym.fields:
        field_strs = [f'  {n}: {_html_escape(t)}' for n, t in list(sym.fields.items())[:15]]
        fields_html = '<br>'.join(field_strs)
        if len(sym.fields) > 15:
            fields_html += f'<br>  ... ({len(sym.fields)} fields total)'
        fields_html = f'<div style="margin-top: 4px;"><code>{fields_html}</code></div>'

    variants_html = ''
    if sym.kind == 'enum' and sym.variants:
        v_strs = sym.variants[:15]
        variants_html = ', '.join(_html_escape(v) for v in v_strs)
        if len(sym.variants) > 15:
            variants_html += f', ... ({len(sym.variants)} total)'
        variants_html = f'<div style="margin-top: 4px;">{variants_html}</div>'

    return f'''
    <body style="margin: 0; padding: 4px;">
        <div><code>{sig}</code></div>
        {fields_html}
        {variants_html}
        {location}
    </body>
    '''


def _html_escape(text):
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


class OdinGotoDefinitionCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        view = self.view
        filepath = _normalize_path(view.file_name() or '')
        point = view.sel()[0].begin()

        word_region = view.word(point)
        word = view.substr(word_region)
        if not word:
            return

        # Check for package-qualified name
        before = view.substr(sublime.Region(max(0, word_region.begin() - 200),
                                             word_region.begin()))
        pkg_alias = ''
        if before.rstrip().endswith('.'):
            m = re.search(r'(\w+)\s*\.\s*$', before)
            if m:
                pkg_alias = m.group(1)

        sym = None
        if pkg_alias:
            for imp in _index.get_file_imports(filepath):
                if imp.alias == pkg_alias:
                    pkg_dir = _index.resolve_import_dir(filepath, imp)
                    if pkg_dir:
                        sym = _index.get_package_symbols(pkg_dir).get(word)
                    break
        else:
            # Current package
            file_dir = os.path.dirname(filepath)
            sym = _index.get_package_symbols(file_dir).get(word)
            if not sym:
                # Only search imported packages, not all global symbols
                for imp in _index.get_file_imports(filepath):
                    pkg_dir = _index.resolve_import_dir(filepath, imp)
                    if pkg_dir:
                        found = _index.get_package_symbols(pkg_dir).get(word)
                        if found:
                            sym = found
                            break

            if not sym:
                # Search globally — always show quick panel to confirm
                syms = _index.get_symbols_by_name(word)
                if len(syms) > 0:
                    # Show quick panel with transient preview on highlight
                    items = []
                    for s in syms:
                        pkg = s.package_name or os.path.basename(s.package_dir)
                        items.append(
                            sublime.QuickPanelItem(
                                trigger=s.name,
                                annotation=f'{pkg} · {os.path.basename(s.file)}:{s.line}',
                                details=s.signature[:100],
                            )
                        )
                    def on_highlight(idx):
                        if idx >= 0:
                            s = syms[idx]
                            view.window().open_file(
                                f'{s.file}:{s.line}:{s.col}',
                                sublime.ENCODED_POSITION | sublime.TRANSIENT)
                    def on_select(idx):
                        if idx >= 0:
                            s = syms[idx]
                            view.window().open_file(
                                f'{s.file}:{s.line}:{s.col}',
                                sublime.ENCODED_POSITION)
                        else:
                            # Cancelled — go back to original file
                            view.window().focus_view(view)
                    view.window().show_quick_panel(
                        items, on_select, on_highlight=on_highlight)
                    return

        if sym:
            view.window().open_file(
                f'{sym.file}:{sym.line}:{sym.col}',
                sublime.ENCODED_POSITION)
        else:
            sublime.status_message(f'Odin: No definition found for "{word}"')

    def is_enabled(self):
        return _is_odin(self.view)


class OdinFindReferencesCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        view = self.view
        point = view.sel()[0].begin()
        word = view.substr(view.word(point))
        if not word:
            return

        window = view.window()
        folders = window.folders()
        if not folders:
            return

        # Use Sublime's built-in find-in-files with the word
        # Construct a results panel
        panel = window.create_output_panel('odin_references')
        panel.set_syntax_file('Packages/Default/Find Results.hidden-tmLanguage')
        panel.settings().set('result_file_regex', r'^(.+):(\d+):')
        panel.settings().set('result_line_regex', r'^(\d+):')

        threading.Thread(
            target=self._search,
            args=(window, word, folders, panel),
            daemon=True,
        ).start()

        window.run_command('show_panel', {'panel': 'output.odin_references'})

    def _search(self, window, word, folders, panel):
        import subprocess
        results = []
        results.append(f'References to "{word}":\n\n')

        for folder in folders:
            for root, dirs, files in os.walk(folder):
                dirs[:] = [d for d in dirs if not d.startswith('.')]
                for f in files:
                    if not f.endswith('.odin'):
                        continue
                    fpath = os.path.join(root, f)
                    try:
                        with open(fpath, 'r', encoding='utf-8', errors='replace') as fh:
                            for line_num, line in enumerate(fh, 1):
                                if word in line:
                                    # Verify it's a whole word match
                                    import re as _re
                                    if _re.search(rf'\b{_re.escape(word)}\b', line):
                                        results.append(
                                            f'{fpath}:{line_num}: {line.rstrip()}\n')
                    except (IOError, OSError):
                        pass

        results.append(f'\n{len(results) - 2} references found.\n')

        def write_results():
            panel.run_command('append', {
                'characters': ''.join(results),
                'force': True,
            })

        sublime.set_timeout(write_results, 0)

    def is_enabled(self):
        return _is_odin(self.view)


class OdinReindexCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        window = self.view.window()
        if window:
            _index.clear()
            _index._odin_roots.clear()
            _index_project_folders(window)
            sublime.status_message('Odin: Reindexing...')


# ============================================================================
# Plugin lifecycle
# ============================================================================

def plugin_loaded():
    # Index all open windows
    for window in sublime.windows():
        if window.folders():
            _index_project_folders(window)


def plugin_unloaded():
    pass
