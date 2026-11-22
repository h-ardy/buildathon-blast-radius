"""Blast Radius selector.

For each patch against sqlglot @ eb11aec, pick the tests that can fail and pack them onto workers.

How tests are chosen (all maps were built from the pinned commit, never from patches):
  * sqlglot/**.py: parse the old and new file. Same AST (comments, blank lines) -> nothing.
    New file does not compile -> every test. Otherwise each changed statement is mapped to the
    tests whose recorded line coverage touches it. Statements that run at import time (class
    bodies, module constants) fall back to the users of the class / module / dialect.
  * tests/fixtures/**: the tests that read that fixture (recorded opens + names in test code).
  * tests/**.py: the tests defined around the change (whole file if module level).
  * anything else (docs, CI files): nothing, unless it is a file sqlglot or its tests load.
"""

import ast
import gzip
import io
import json
import os
import re
import sys
import tarfile
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("BR_DATA", "/data"))
WORKER_COST = 2.0
MAX_WORKERS = 8


def log(*a):
    print(*a, file=sys.stderr)


# ----------------------------------------------------------------------------- maps
M = json.load(gzip.open(HERE / "map" / "map.json.gz", "rt"))
SETS = M["sets"]
MAP_TESTS = M["tests"]
COV = {f: {int(k): v for k, v in lines.items()} for f, lines in M["cov"].items()}
IMP = {f: set(v) for f, v in M["imp"].items()}

SRC = {}
with tarfile.open(HERE / "map" / "src.tar.gz") as tf:
    for member in tf.getmembers():
        if member.isfile():
            SRC[member.name] = tf.extractfile(member).read().decode("utf-8")

ALL = set(range(len(MAP_TESTS)))
BY_FILE = defaultdict(set)
BY_CLASS = defaultdict(set)
for i, t in enumerate(MAP_TESTS):
    BY_FILE[t.split("::")[0]].add(i)
    BY_CLASS[t.rsplit("::", 1)[0]].add(i)


def tid(name):
    for i, t in enumerate(MAP_TESTS):
        if t.endswith(name):
            return i
    return None


T_LAZY_LOAD = tid("TestDialect::test_lazy_load")
T_TYPING_META = tid("TestTyping::test_no_redundant_expression_metadata")


IMPTRIG = M.get("imptrig", {"triggers": [], "lines": {}})


def import_triggers(path, lines):
    """module/class-level statements that ran these lines while sqlglot was being imported"""
    d = IMPTRIG["lines"].get(path, {})
    out = set()
    for ln in lines:
        for t in d.get(str(ln), ()):
            out.add(tuple(IMPTRIG["triggers"][t]))
    return out


def line_tests(path, lines):
    out = set()
    cov = COV.get(path, {})
    for ln in lines:
        s = cov.get(ln)
        if s is not None:
            out.update(SETS[s])
    return out


def module_runtime_users(path):
    out = set()
    for s in set(COV.get(path, {}).values()):
        out.update(SETS[s])
    return out


# modules that import a module, transitively
IMPORTERS = M["importers"]


def transitive_importers(path):
    seen = {path}
    stack = [path]
    while stack:
        p = stack.pop()
        for q in IMPORTERS.get(p, ()):
            if q not in seen:
                seen.add(q)
                stack.append(q)
    return seen


def dialect_users_of_module(path):
    """Tests that instantiate a dialect whose dialect/parser/generator/tokenizer classes come
    from this module, or from a module that (transitively) imports it."""
    mods = transitive_importers(path)
    modnames = {M["modnames"].get(p) for p in mods}
    out = set()
    for dkey, info in M["dialects"].items():
        if any(c.rsplit(".", 1)[0] in modnames or ".".join(c.split(".")[:-2]) in modnames for c in info["comps"]):
            out.update(info["users"])
    return out


# ----------------------------------------------------------------------------- patches
def parse_patch(text):
    """-> list of dicts {old, new, hunks:[(old_start, old_lines[], new_lines[], body[])], status}"""
    files = []
    cur = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            cur = {"old": m.group(1) if m else None, "new": m.group(2) if m else None, "hunks": [], "status": "modified", "binary": False}
            files.append(cur)
        elif cur is not None and line.startswith("new file mode"):
            cur["status"] = "added"
        elif cur is not None and line.startswith("deleted file mode"):
            cur["status"] = "deleted"
        elif cur is not None and line.startswith("rename from "):
            cur["old"] = line[len("rename from "):]
            cur["status"] = "renamed"
        elif cur is not None and line.startswith("rename to "):
            cur["new"] = line[len("rename to "):]
        elif cur is not None and line.startswith("Binary files"):
            cur["binary"] = True
        elif cur is not None and line.startswith("--- "):
            p = line[4:].split("\t")[0]
            if p == "/dev/null":
                cur["status"] = "added"
            elif p.startswith("a/"):
                cur["old"] = p[2:]
        elif cur is not None and line.startswith("+++ "):
            p = line[4:].split("\t")[0]
            if p == "/dev/null":
                cur["status"] = "deleted"
            elif p.startswith("b/"):
                cur["new"] = p[2:]
        elif cur is not None and line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            ostart = int(m.group(1))
            olen = int(m.group(2)) if m.group(2) is not None else 1
            nlen = int(m.group(4)) if m.group(4) is not None else 1
            body = []
            i += 1
            o = n = 0
            while i < len(lines) and (o < olen or n < nlen):
                l = lines[i]
                if l.startswith("\\"):
                    i += 1
                    continue
                tag = l[:1] if l else " "
                if tag not in " +-":
                    break
                body.append((tag, l[1:]))
                if tag in " -":
                    o += 1
                if tag in " +":
                    n += 1
                i += 1
            while i < len(lines) and lines[i].startswith("\\"):
                i += 1
            cur["hunks"].append((ostart, body))
            continue
        i += 1
    return files


def apply_hunks(old_text, hunks):
    """apply unified-diff hunks; raises ValueError if a hunk's context does not match the pinned file"""
    old = old_text.split("\n")
    new = []
    pos = 0  # 0-based index into old
    for ostart, body in hunks:
        start = ostart - 1 if ostart > 0 else 0
        want = [text for tag, text in body if tag in " -"]

        def fits(at):
            return at >= pos and old[at:at + len(want)] == want

        if want and not fits(start):
            # the line numbers everything else uses would be wrong too
            raise ValueError("patch does not apply at its stated lines")
        new.extend(old[pos:start])
        pos = start
        for tag, text in body:
            if tag == " ":
                new.append(old[pos] if pos < len(old) else text)
                pos += 1
            elif tag == "-":
                pos += 1
            else:
                new.append(text)
    new.extend(old[pos:])
    return "\n".join(new)


def changed_old_lines(hunks):
    """old line numbers removed, and insertion points (inserted before old line p)."""
    removed, inserts = set(), set()
    for ostart, body in hunks:
        o = ostart if ostart > 0 else 1
        for tag, text in body:
            if tag == " ":
                o += 1
            elif tag == "-":
                removed.add(o)
                o += 1
            else:
                inserts.add(o)
    return removed, inserts


def change_blocks(hunks):
    """-> list of (removed old lines, insertion point or None, changed texts)"""
    blocks = []
    for ostart, body in hunks:
        o = ostart if ostart > 0 else 1
        cur = None
        for tag, text in body:
            if tag == " ":
                if cur:
                    blocks.append(cur)
                cur = None
                o += 1
                continue
            if cur is None:
                cur = ([], None, [])
            if tag == "-":
                cur[0].append(o)
                o += 1
            else:
                if not cur[0] and cur[1] is None:
                    cur = (cur[0], o, cur[2])
            cur[2].append(text)
        if cur:
            blocks.append(cur)
    return [(r, (None if r else i), t) for r, i, t in blocks]


def is_comment_or_blank(s):
    s = s.strip()
    return s == "" or s.startswith("#")


# ----------------------------------------------------------------------------- python analysis
class Scope:
    __slots__ = ("node", "kind", "parent")

    def __init__(self, node, kind, parent):
        self.node, self.kind, self.parent = node, kind, parent


def stmt_index(tree):
    """list of (start, end, node, chain) for every statement; chain = enclosing def/class nodes."""
    out = []

    def visit(body, chain):
        for st in body:
            first = min([st.lineno] + [d.lineno for d in getattr(st, "decorator_list", [])])
            out.append((first, st.end_lineno, st, chain))
            for field in ("body", "orelse", "finalbody", "handlers", "cases"):
                sub = getattr(st, field, None)
                if not sub:
                    continue
                if field == "handlers" or field == "cases":
                    for h in sub:
                        out.append((h.lineno if hasattr(h, "lineno") else st.lineno, getattr(h, "end_lineno", st.end_lineno), h, chain + ((st,) if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else ())))
                        visit(h.body, chain + ((st,) if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else ()))
                    continue
                nchain = chain + (st,) if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else chain
                visit(sub, nchain)

    visit(tree.body, ())
    return out


def header_end(st):
    """last line of a compound statement's header (before its first child statement)."""
    firsts = []
    for field in ("body", "orelse", "finalbody", "handlers", "cases"):
        sub = getattr(st, field, None)
        if sub:
            firsts.append(sub[0].lineno)
    if hasattr(st, "decorator_list") and st.decorator_list:
        pass
    if not firsts:
        return st.end_lineno
    return min(firsts) - 1


def innermost(index, line):
    best = None
    for start, end, st, chain in index:
        if start <= line <= end:
            if best is None or (end - start) <= (best[1] - best[0]):
                best = (start, end, st, chain)
    return best


def func_body_lines(fn):
    return range(fn.body[0].lineno, fn.end_lineno + 1)


def in_function(chain):
    for n in reversed(chain):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return n
    return None


def lambda_containing(st, line):
    for node in ast.walk(st):
        if isinstance(node, ast.Lambda) and node.lineno <= line <= node.end_lineno:
            return node
    return None


def string_spans(tree):
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)) and node.end_lineno > node.lineno:
            spans.append((node.lineno, node.end_lineno))
        elif isinstance(node, ast.JoinedStr) and node.end_lineno > node.lineno:
            spans.append((node.lineno, node.end_lineno))
    return spans


def scope_body(tree, chain):
    body = tree.body
    for n in chain:
        if isinstance(n, ast.ClassDef):
            node = next((x for x in body if isinstance(x, ast.ClassDef) and x.name == n.name), None)
            if node is None:
                return None
            body = node.body
        else:
            return None
    return body


def added_def_names(old_tree, new_tree, chain):
    ob = scope_body(old_tree, chain)
    nb = scope_body(new_tree, chain)
    if ob is None or nb is None:
        return set()
    names = lambda b: {x.name for x in b if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    return names(nb) - names(ob)


def scope_classes(path, chain):
    modname = M["modnames"].get(path)
    classes = [n.name for n in chain if isinstance(n, ast.ClassDef)]
    if not modname or not classes:
        return []
    return [f"{modname}." + ".".join(classes)]


def scope_info(fn):
    """names a function declares nonlocal/global, binds, and reads (nested scopes included)"""
    decl, stores, loads = set(), set(), set()
    a = fn.args
    for arg in a.posonlyargs + a.args + a.kwonlyargs + [x for x in (a.vararg, a.kwarg) if x]:
        stores.add(arg.arg)
    for node in ast.walk(ast.Module(body=fn.body, type_ignores=[])):
        if isinstance(node, ast.Nonlocal):
            decl.update(("nonlocal", n) for n in node.names)
        elif isinstance(node, ast.Global):
            decl.update(("global", n) for n in node.names)
        elif isinstance(node, ast.Name):
            (loads if isinstance(node.ctx, ast.Load) else stores).add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stores.add(node.name)
        elif isinstance(node, ast.alias):
            stores.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            stores.add(node.name)
    return decl, stores - {n for _, n in decl}, loads


def functions_by_name(tree):
    out = {}

    def walk(node, prefix):
        # defs can only sit in statement lists, so only those are searched
        for field in ("body", "orelse", "finalbody", "handlers", "cases"):
            for child in getattr(node, field, None) or ():
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    q = prefix + (child.name,)
                    if not isinstance(child, ast.ClassDef):
                        out.setdefault(q, child)
                    walk(child, q)
                elif isinstance(child, (ast.stmt, ast.excepthandler, ast.match_case)):
                    walk(child, prefix)

    walk(tree, ())
    return out


def scope_changes(old_tree, new_tree, lines):
    """functions whose name scoping changed (a nonlocal/global added or removed, or a name that is read
    becoming local or global): every line of such a function can behave differently.
    Only functions spanning one of the changed old `lines` can change."""
    old_f, new_f = functions_by_name(old_tree), functions_by_name(new_tree)
    out = []
    for q, fo in old_f.items():
        if not any(fo.lineno <= ln <= fo.end_lineno for ln in lines):
            continue
        fnew = new_f.get(q)
        if fnew is None:
            continue
        d0, s0, l0 = scope_info(fo)
        d1, s1, l1 = scope_info(fnew)
        if d0 != d1 or ((s0 ^ s1) & (l0 | l1)):
            out.append((q, fo))
    return out


def defined_names(body):
    """names a module or class body defines"""
    out = set()
    for st in body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(st.name)
        elif isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            out.update(assigned_names(st))
        elif isinstance(st, (ast.Import, ast.ImportFrom)):
            out.update((a.asname or a.name).split(".")[0] for a in st.names if a.name != "*")
    return out


def removed_names(old_tree, new_tree):
    """module-level names, and per class the class-level names, that the patch removes or renames away:
    -> (module names, {class qualname: names})"""
    mod = defined_names(old_tree.body) - defined_names(new_tree.body)
    old_c = {q: n for q, n in classes_by_name(old_tree).items()}
    new_c = classes_by_name(new_tree)
    cls = {}
    for q, node in old_c.items():
        if q in new_c:
            gone = defined_names(node.body) - defined_names(new_c[q].body)
            if gone:
                cls[q] = gone
    return mod, cls


def classes_by_name(tree):
    out = {}

    def walk(node, prefix):
        for field in ("body", "orelse", "finalbody", "handlers", "cases"):
            for child in getattr(node, field, None) or ():
                if isinstance(child, ast.ClassDef):
                    q = prefix + (child.name,)
                    out.setdefault(q, child)
                    walk(child, q)
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    walk(child, prefix + (child.name,))
                elif isinstance(child, (ast.stmt, ast.excepthandler, ast.match_case)):
                    walk(child, prefix)

    walk(tree, ())
    return out


def inherited(ckey, name):
    """True if a base class of ckey (at the pinned commit) defines name"""
    if name.startswith("__") and name.endswith("__"):
        return True
    seen, stack = set(), list(M["classes"].get(ckey, ()))
    while stack:
        b = stack.pop()
        if b in seen:
            continue
        seen.add(b)
        node = class_node(b)
        if node is not None and name in defined_names(node.body):
            return True
        stack.extend(M["classes"].get(b, ()))
    return False


def mention_tests(names):
    """tests whose own code mentions one of `names` (bare or as an attribute), at the statement level"""
    out = set()
    for f, ts in BY_FILE.items():
        imports, stmts = test_code_index(f)
        for ids, attrs, lines in stmts:
            if (ids | attrs) & names:
                got = line_tests(f, lines)
                out |= got if got else ts
    return out


def select_python(path, old_text, hunks, why):
    new_text = apply_hunks(old_text, hunks)
    try:
        old_tree = ast.parse(old_text)
    except SyntaxError:
        why.append(f"{path}: old unparsable")
        return set(ALL)
    try:
        new_tree = ast.parse(new_text)
        # compile from bytes so a coding declaration is honoured the way imports honour it
        compile(new_text.encode("utf-8"), path, "exec")
    except (SyntaxError, ValueError, UnicodeError) as e:
        why.append(f"{path}: new does not compile ({e.__class__.__name__}) -> all")
        return set(ALL)
    if ast.dump(old_tree) == ast.dump(new_tree):
        why.append(f"{path}: same AST")
        return set()

    scope_extra = set()
    # names that no longer exist: every use of them now fails (exp.Pos, self.pos_sql, imports)
    gone_mod, gone_cls = removed_names(old_tree, new_tree)
    modname_here = M["modnames"].get(path)
    gone_cls = {q: {n for n in names if not inherited(f"{modname_here}." + ".".join(q), n)}
                for q, names in gone_cls.items()}
    gone_cls = {q: names for q, names in gone_cls.items() if names}
    gone = set(gone_mod)
    for q, names in gone_cls.items():
        gone |= names
    if path.startswith(TEST_IMPORT_FILES):
        NEED_TEST_IMPORTS[0] = True
    if gone:
        NEED_IMPORT_CHECK[0] = True
        NEED_TEST_IMPORTS[0] = True
        got = attr_ref_users(gone) | mention_tests(gone)
        if gone_mod:
            got |= name_users(path, gone_mod, why, 0)
        for name in gone:
            if name.endswith("_sql"):
                ecls = M.get("exprclasses", {}).get(name[:-4])
                if ecls:
                    for q in gone_cls:
                        for c in all_subs(f"{M['modnames'].get(path)}." + ".".join(q)):
                            got |= dispatch_readers(c, {"T" + ecls})
        modname = M["modnames"].get(path)
        for cname in gone_mod:
            got |= class_users(all_subs(f"{modname}.{cname}"))
        scope_extra |= got
        why.append(f"{path}: removed names {sorted(gone)[:8]} -> {len(got)}")
    _rem, _ins = changed_old_lines(hunks)
    for q, fo in scope_changes(old_tree, new_tree, _rem | _ins | {p - 1 for p in _ins}):
        got = line_tests(path, func_body_lines(fo))
        why.append(f"{path}: scoping changed in {'.'.join(q)} -> {len(got)}")
        scope_extra |= got
    removed, inserts = changed_old_lines(hunks)
    index = stmt_index(old_tree)
    spans = string_spans(old_tree)
    out = set()
    import_level = []

    # each contiguous block of -/+ lines is one change; comment/blank-only blocks outside strings
    # are skipped (the AST check above already proved the file is otherwise different)
    old_lines = old_text.split("\n")
    targets = set()
    for block in change_blocks(hunks):
        rem, ins_at, texts = block
        touched = set(rem) | ({ins_at - 1, ins_at} if ins_at else set())
        in_string = any(a <= ln <= b for ln in touched for a, b in spans)
        if all(is_comment_or_blank(t) for t in texts) and not in_string:
            continue
        if rem:
            targets.update(rem)
        else:
            # pure insertion before old line ins_at: nearest code lines above and below
            up = ins_at - 1
            while up >= 1 and is_comment_or_blank(old_lines[up - 1]):
                up -= 1
            down = ins_at
            while down <= len(old_lines) and is_comment_or_blank(old_lines[down - 1]):
                down += 1
            if up >= 1:
                targets.add(up)
            if down <= len(old_lines):
                targets.add(down)

    for ln in sorted(targets):
        hit = innermost(index, ln)
        if hit is None:
            if ln <= len(old_lines) and is_comment_or_blank(old_lines[ln - 1]):
                continue
            import_level.append((ln, None, ()))
            continue
        start, end, st, chain = hit
        fn = in_function(chain)
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and ln >= st.body[0].lineno:
            continue  # a blank/comment line between the children of a def or class
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)) and ln < st.body[0].lineno:
            # def line, decorators, defaults: whoever runs the function; a renamed def can also
            # start answering to its new name (name-based dispatch, overrides)
            NEED_IMPORT_CHECK[0] = True
            out |= line_tests(path, func_body_lines(st))
            trig = import_triggers(path, func_body_lines(st))
            if trig:
                out |= triggered_tests(trig, why)
            added = added_def_names(old_tree, new_tree, chain)
            if added:
                out |= attr_ref_users(added)
                for c in scope_classes(path, chain):
                    for name in added:
                        if name.endswith("_sql"):
                            ecls = M.get("exprclasses", {}).get(name[:-4])
                            if ecls:
                                for s in all_subs(c):
                                    out |= dispatch_readers(s, {"T" + ecls})
                why.append(f"{path}: new names {sorted(added)}")
            continue
        if isinstance(st, ast.ClassDef) and ln < st.body[0].lineno:
            import_level.append((ln, st, chain))
            continue
        if fn is not None:
            # runtime statement inside a function: tests covering the statement (header only for
            # compound statements)
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                hdr = range(st.lineno, st.end_lineno + 1)
            else:
                hdr = range(start, header_end(st) + 1)
            out |= line_tests(path, hdr)
            trig = import_triggers(path, hdr)
            if trig:
                NEED_IMPORT_CHECK[0] = True
                out |= triggered_tests(trig, why)
            continue
        lam = lambda_containing(st, ln)
        if lam is not None:
            out |= line_tests(path, range(lam.lineno, lam.end_lineno + 1))
            trig = import_triggers(path, range(lam.lineno, lam.end_lineno + 1))
            if trig:
                NEED_IMPORT_CHECK[0] = True
                out |= triggered_tests(trig, why)
            continue
        import_level.append((ln, st, chain))

    if import_level:
        NEED_IMPORT_CHECK[0] = True
        if any(not any(isinstance(n, ast.ClassDef) for n in (it[2] or ())) for it in import_level):
            NEED_TEST_IMPORTS[0] = True
        # group by statement so a multi-line table is analysed once
        seen, items = set(), []
        for it in import_level:
            k = id(it[1])
            if k not in seen:
                seen.add(k)
                items.append(it)
        out |= import_level_tests(path, items, why, new_tree)
    return out | scope_extra


# ---- class attributes -------------------------------------------------------------------
ATTRS = M["attrs"]
CLASSES = M["classes"]
SUBS = defaultdict(set)
for _c, _bases in CLASSES.items():
    for _b in _bases:
        SUBS[_b].add(_c)

# attributes turned into other attributes when a class is created
DERIVED = {
    # tokens._TokenizerBase.__init_subclass__
    "QUOTES": ["_QUOTES", "_FORMAT_STRINGS", "_KEYWORD_TRIE"],
    "IDENTIFIERS": ["_IDENTIFIERS"],
    "BIT_STRINGS": ["_FORMAT_STRINGS", "_KEYWORD_TRIE"],
    "BYTE_STRINGS": ["_FORMAT_STRINGS", "_KEYWORD_TRIE"],
    "HEX_STRINGS": ["_FORMAT_STRINGS", "_KEYWORD_TRIE"],
    "NATIONAL_RAW_STRINGS": ["_FORMAT_STRINGS", "_KEYWORD_TRIE"],
    "RAW_STRINGS": ["_FORMAT_STRINGS", "_KEYWORD_TRIE"],
    "HEREDOC_STRINGS": ["_FORMAT_STRINGS", "_KEYWORD_TRIE"],
    "UNICODE_STRINGS": ["_FORMAT_STRINGS", "_KEYWORD_TRIE"],
    "STRING_ESCAPES": ["_STRING_ESCAPES", "BYTE_STRING_ESCAPES", "_BYTE_STRING_ESCAPES"],
    "BYTE_STRING_ESCAPES": ["_BYTE_STRING_ESCAPES"],
    "IDENTIFIER_ESCAPES": ["_IDENTIFIER_ESCAPES"],
    "COMMENTS": ["_COMMENTS", "_KEYWORD_TRIE"],
    "HINT_START": ["_COMMENTS", "_KEYWORD_TRIE"],
    "KEYWORDS": ["_KEYWORD_TRIE", "_COMMENTS"],
    "SINGLE_TOKENS": ["_KEYWORD_TRIE"],
    # dialect._Dialect.__new__
    "TIME_MAPPING": ["TIME_TRIE", "FORMAT_TRIE", "INVERSE_TIME_MAPPING", "INVERSE_TIME_TRIE"],
    "FORMAT_MAPPING": ["FORMAT_TRIE", "INVERSE_FORMAT_MAPPING", "INVERSE_FORMAT_TRIE"],
    "INVERSE_TIME_MAPPING": ["INVERSE_TIME_TRIE"],
    "CREATABLE_KIND_MAPPING": ["INVERSE_CREATABLE_KIND_MAPPING"],
    "UNESCAPED_SEQUENCES": ["ESCAPED_SEQUENCES", "IDENTIFIER_ESCAPED_SEQUENCES"],
    "SUPPORTED_JSON_PATH_PARTS": ["TRANSFORMS"],
    # expressions.core.Expr.__init_subclass__
    "arg_types": ["required_args"],
    # parser class body
    "SHOW_PARSERS": ["SHOW_TRIE"],
    "SET_PARSERS": ["SET_TRIE"],
}
# tokenizer attributes the dialect metaclass copies into the dialect class
TOK_TO_DIALECT = {"QUOTES", "IDENTIFIERS", "BIT_STRINGS", "HEX_STRINGS", "BYTE_STRINGS", "UNICODE_STRINGS",
                  "STRING_ESCAPES", "BYTE_STRING_ESCAPES", "NUMERIC_ESCAPES", "IDENTIFIER_ESCAPES"}
DIALECT_FROM_TOK = ["QUOTE_START", "QUOTE_END", "IDENTIFIER_START", "IDENTIFIER_END", "BIT_START", "BIT_END",
                    "HEX_START", "HEX_END", "BYTE_START", "BYTE_END", "UNICODE_START", "UNICODE_END",
                    "STRINGS_SUPPORT_ESCAPED_SEQUENCES", "BYTE_STRINGS_SUPPORT_ESCAPED_SEQUENCES",
                    "UNESCAPED_SEQUENCES", "ESCAPED_SEQUENCES", "IDENTIFIER_ESCAPED_SEQUENCES"]

AKEYS = M.get("akeys", {})   # ckey -> attr -> key -> set id (tests that looked the key up)
ASTAR = M.get("astar", {})   # ckey -> attr -> set id (tests that iterated / copied / compared it)
AANY = M.get("aany", {})     # ckey -> attr -> set id (tests with any key or star event)

_TREES = {}


def tree_of(path):
    if path not in _TREES:
        try:
            _TREES[path] = ast.parse(SRC.get(path, ""))
        except SyntaxError:
            _TREES[path] = ast.parse("")
    return _TREES[path]


MODPATH = {v: k for k, v in M["modnames"].items()}


import functools


@functools.lru_cache(maxsize=None)
def class_node(ckey):
    """ClassDef node for a class key (module.qualname), or None."""
    parts = ckey.split(".")
    for i in range(len(parts) - 1, 0, -1):
        mod = ".".join(parts[:i])
        if mod in MODPATH:
            body = tree_of(MODPATH[mod]).body
            node = None
            for name in parts[i:]:
                node = next((n for n in body if isinstance(n, ast.ClassDef) and n.name == name), None)
                if node is None:
                    return None
                body = node.body
            return node
    return None


def all_subs(ckey):
    seen = {ckey}
    stack = [ckey]
    while stack:
        c = stack.pop()
        for s in SUBS.get(c, ()):
            if s not in seen:
                seen.add(s)
                stack.append(s)
    return seen


def assigned_names(st):
    names = []
    targets = []
    if isinstance(st, ast.Assign):
        targets = st.targets
    elif isinstance(st, (ast.AnnAssign, ast.AugAssign)):
        targets = [st.target]
    for tgt in targets:
        for n in ast.walk(tgt):
            if isinstance(n, ast.Name):
                names.append(n.id)
    return names


_REFS = {}


def referenced(node):
    """names and attribute names used anywhere in node (cached: the trees are parsed once and kept)"""
    k = id(node)
    hit = _REFS.get(k)
    if hit is not None and hit[0] is node:
        return hit[1]
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
    out = frozenset(out)
    _REFS[k] = (node, out)
    return out


@functools.lru_cache(maxsize=None)
def class_body_deps(ckey):
    """attr -> names its class-body definition refers to"""
    node = class_node(ckey)
    deps = {}
    if node is None:
        return deps
    for st in node.body:
        if isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and getattr(st, "value", None) is not None:
            refs = referenced(st.value)
            for name in assigned_names(st):
                deps.setdefault(name, set()).update(refs)
    return deps


def readers(ckey, attr):
    s = ATTRS.get(ckey, {}).get(attr)
    return set(SETS[s]) if s is not None else set()


def class_users(classes):
    out = set()
    for c in classes:
        for s in ATTRS.get(c, {}).values():
            out.update(SETS[s])
    return out


def static_keys(node):
    """canonical keys a literal key/element can have at runtime, or None if unknown"""
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, str):
            return {"S" + v}
        if isinstance(v, bool) or isinstance(v, (int, float)):
            return {"N" + repr(v)}
        return None
    if isinstance(node, ast.Attribute):
        return {"E" + node.attr, "T" + node.attr}
    if isinstance(node, ast.Name):
        return {"E" + node.id, "T" + node.id}
    return None


def flatten_value(node):
    """split a table definition into (opaque parts, {key: value dump}, ok).
    Handles dict/set/list/tuple literals, ** spreads, set algebra and set()/frozenset()/dict() calls."""
    opaque, entries = [], {}

    def add_entry(knode, vnode):
        ks = static_keys(knode)
        if ks is None:
            return False
        k = "|".join(sorted(ks))
        entries[k] = entries.get(k, "") + ast.dump(vnode if vnode is not None else knode)
        return True

    def go(n, op):
        if isinstance(n, ast.Dict):
            for k, v in zip(n.keys, n.values):
                if k is None:
                    opaque.append((op, "**", ast.dump(v)))
                elif not add_entry(k, v):
                    return False
            return True
        if isinstance(n, (ast.Set, ast.List, ast.Tuple)):
            for e in n.elts:
                if isinstance(e, ast.Starred):
                    opaque.append((op, "*", ast.dump(e.value)))
                elif not add_entry(e, None):
                    return False
            return True
        if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.BitOr, ast.Sub, ast.BitAnd)):
            return go(n.left, op) and go(n.right, op + type(n.op).__name__)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("set", "frozenset", "dict", "tuple", "list") and len(n.args) <= 1 and not n.keywords:
            return go(n.args[0], op + n.func.id) if n.args else True
        opaque.append((op, "?", ast.dump(n)))
        return True

    ok = go(node, "")
    return opaque, entries, ok


def changed_keys(old_value, new_value):
    """canonical keys whose entries differ between two table definitions, or None"""
    if old_value is None or new_value is None:
        return None
    o_op, o_ent, ok1 = flatten_value(old_value)
    n_op, n_ent, ok2 = flatten_value(new_value)
    if not (ok1 and ok2) or sorted(o_op) != sorted(n_op):
        return None
    if not o_ent and not n_ent:
        return None
    out = set()
    for k in set(o_ent) | set(n_ent):
        if o_ent.get(k) != n_ent.get(k):
            out.update(k.split("|"))
    KEYSET_CHANGED[0] = set(o_ent) != set(n_ent)
    return out


def key_preserving(value, names):
    """is this definition built from `names` only by spreads, unions and literals?"""
    opaque, entries, ok = flatten_value(value)
    if not ok:
        return False
    for op, kind, dump in opaque:
        node = ast.parse(dump) if False else None
        if kind == "?" and not re.fullmatch(r"(Name|Attribute)\(.*", dump):
            return False
    return True


KEYSET_CHANGED = [True]
ALEN = M.get("alen", {})


_AREF = {}


def attr_ref_index(path):
    """(names referenced, lines) for every statement inside a function of path, built once"""
    idx = _AREF.get(path)
    if idx is not None:
        return idx
    idx = []
    for node in ast.walk(tree_of(path)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            for st in ast.walk(node):
                if isinstance(st, (ast.stmt, ast.Lambda)) and st is not node and not hasattr(st, "body") or isinstance(st, ast.Lambda):
                    idx.append((referenced(st), range(st.lineno, st.end_lineno + 1)))
                elif isinstance(st, ast.stmt) and st is not node:
                    hdr = [getattr(st, f) for f in ("test", "iter", "items", "value") if getattr(st, f, None) is not None]
                    refs = set()
                    for h in hdr:
                        if not isinstance(h, list):
                            refs |= referenced(h)
                    idx.append((frozenset(refs), range(st.lineno, header_end(st) + 1)))
    _AREF[path] = idx
    return idx


def attr_ref_users(names):
    """line coverage of every statement inside a sqlglot function that mentions one of `names`
    as an attribute (self.NAME, cls.NAME) or a bare name"""
    out = set()
    for path, text in SRC.items():
        if not path.startswith("sqlglot/") or not any(n in text for n in names):
            continue
        for refs, lines in attr_ref_index(path):
            if refs & names:
                out |= line_tests(path, lines)
    return out


def dispatch_readers(c, keys):
    out = set()
    d = AKEYS.get(c, {}).get("__dispatch__", {})
    for k in keys:
        s = d.get(k)
        if s is not None:
            out.update(SETS[s])
    return out


def keyed_readers(c, a, keys, star=True):
    """tests that looked up one of `keys` in (c, a), iterated it, or read it in a way not recorded per key"""
    out = set()
    d = AKEYS.get(c, {}).get(a, {})
    for k in keys:
        s = d.get(k)
        if s is not None:
            out.update(SETS[s])
    s = ASTAR.get(c, {}).get(a)
    if s is not None and star:
        out.update(SETS[s])
    if KEYSET_CHANGED[0]:
        s = ALEN.get(c, {}).get(a)
        if s is not None:
            out.update(SETS[s])
    r = readers(c, a)
    s = AANY.get(c, {}).get(a)
    anyk = set(SETS[s]) if s is not None else set()
    if not NO_ANYK:
        out |= r - anyk
    return out


TRIE_CHARS = set("()[]{}&^:,.-=><%!|+;/\\*~?@#'`\"$")


def attr_tests(ckey, names, why, keys=None):
    subs = all_subs(ckey)
    if len(names) == 1 and names[0].endswith("_sql") and AKEYS:
        ecls = M.get("exprclasses", {}).get(names[0][:-4])
        if ecls is not None:
            out = set()
            for c in subs:
                out |= dispatch_readers(c, {"T" + ecls})
                out |= readers(c, names[0])
            out |= attr_ref_users({names[0]})  # direct self.x_sql(...) calls
            why.append(f"{ckey}.{names[0]}: dispatch of {ecls} -> {len(out)}")
            return out
    # names affected through other class-body definitions, in this class and its subclasses
    affected = {c: set(names) for c in subs}
    for c in subs:
        for a in list(affected[c]):
            affected[c].update(DERIVED.get(a, ()))
    deps = {c: class_body_deps(c) for c in subs}
    changed = True
    while changed:
        changed = False
        for c in subs:
            all_names = set().union(*affected.values())
            for b, refs in deps[c].items():
                if b not in affected[c] and refs & all_names:
                    affected[c].add(b)
                    affected[c].update(DERIVED.get(b, ()))
                    changed = True
    # which (class, attr) pairs can be narrowed to the changed keys
    keyed = {}
    if keys is not None and AKEYS:
        simple_kw = all(k.startswith("S") and " " not in k and not (set(k[1:]) & TRIE_CHARS) for k in keys)
        for c in subs:
            for a in affected[c]:
                if a in names:
                    node = class_node(c)
                    own = None
                    if node is not None:
                        for st in node.body:
                            if isinstance(st, (ast.Assign, ast.AnnAssign)) and a in assigned_names(st):
                                own = st
                    if c == ckey or own is None or key_preserving(own.value, names):
                        keyed[(c, a)] = True
                elif a in ("_KEYWORD_TRIE", "_COMMENTS") and set(names) <= {"KEYWORDS"} and simple_kw:
                    keyed[(c, a)] = "skip"
                elif a in assigned_dependents_keyed(c, names):
                    keyed[(c, a)] = True
    out = set()
    total_readers = 0
    for c in subs:
        for a in affected[c]:
            r = readers(c, a)
            total_readers += len(r)
            mode = keyed.get((c, a))
            if mode == "skip":
                continue
            if a == "TRANSFORMS" and mode:
                # TRANSFORMS feeds the per-class dispatch table, looked up per expression type
                out |= keyed_readers(c, a, keys, star=False) | dispatch_readers(c, keys)
                continue
            if a == "TRANSFORMS":
                # entries unknown: whoever generated anything through this class's dispatch table
                out |= dispatch_readers(c, AKEYS.get(c, {}).get("__dispatch__", {}).keys())
            out |= keyed_readers(c, a, keys) if mode else r
    tok_names = set().union(*affected.values()) & TOK_TO_DIALECT
    if tok_names == {"NUMERIC_ESCAPES"} and keys is not None and all(k.startswith("S") for k in keys) and NUMERIC_KEYS_SAME[0]:
        tok_names = set()  # the dialect only looks at which escapes exist, not their values
    if tok_names:
        for dkey, info in M["dialects"].items():
            if subs & set(info["comps"]):
                for dc in all_subs(dkey):
                    for a in DIALECT_FROM_TOK:
                        out |= readers(dc, a)
    if total_readers == 0 and DATA_RULE and DATA_VALUE[0] and not any("Enum" in b for b in M["classes"].get(ckey, ())):
        why.append(f"{ckey}.{sorted(names)}: data value never read by any test -> {len(out)}")
    elif total_readers == 0:
        # not read through the class while tests run (callables are not tracked): the statements
        # that use the name, or else whoever uses the class at all
        refs = attr_ref_users(set(names))
        if refs:
            out |= refs
            why.append(f"{ckey}.{sorted(names)}: referencing statements -> {len(out)}")
        else:
            out |= class_users({ckey})
            why.append(f"{ckey}.{sorted(names)}: no readers, class users -> {len(out)}")
    else:
        why.append(f"{ckey}.{sorted(names)} (+{sum(len(v) for v in affected.values()) - len(names)} derived) -> {len(out)}")
    return out


NUMERIC_KEYS_SAME = [False]
DATA_VALUE = [False]   # the changed class attribute holds plain data (tracked per read), not a callable
DATA_RULE = os.environ.get("BR_DATA_RULE", "1") == "1"


def is_data_value(node):
    """a literal value, or a container display: such class attributes were wrapped and every read
    recorded while the map was built (callables, classes and enum members were not)"""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.operand, ast.Constant):
        return True
    return isinstance(node, (ast.Dict, ast.Set, ast.List, ast.Tuple, ast.DictComp, ast.SetComp, ast.ListComp))


def assigned_dependents_keyed(c, names):
    """attrs of class c whose class-body definition is a key-preserving composition of `names`"""
    node = class_node(c)
    out = set()
    if node is None:
        return out
    for st in node.body:
        if isinstance(st, (ast.Assign, ast.AnnAssign)) and getattr(st, "value", None) is not None:
            if referenced(st.value) & set(names) and key_preserving(st.value, names):
                out.update(assigned_names(st))
    return out


def new_class_stmt(new_tree, class_names, attr):
    body = new_tree.body
    node = None
    for name in class_names:
        node = next((n for n in body if isinstance(n, ast.ClassDef) and n.name == name), None)
        if node is None:
            return None
        body = node.body
    for st in body:
        if isinstance(st, (ast.Assign, ast.AnnAssign)) and attr in assigned_names(st):
            return st
    return None


NEED_IMPORT_CHECK = [False]
# whether the import check must also import every test module. Test modules only run sqlglot code at
# import time through these files (measured at the pinned commit), so other changes can only break a
# test module's import by removing a name it uses, or through module-level code.
NEED_TEST_IMPORTS = [False]
TEST_IMPORT_FILES = ("sqlglot/__init__.py", "sqlglot/dialects/__init__.py", "sqlglot/dialects/dialect.py",
                     "sqlglot/optimizer/__init__.py", "sqlglot/tokens.py", "sqlglot/tokenizer_core.py",
                     "sqlglot/helper.py", "sqlglot/parser.py", "sqlglot/generator.py", "sqlglot/expressions/",
                     "sqlglot/errors.py", "sqlglot/_typing.py", "sqlglot/trie.py", "sqlglot/schema.py",
                     "sqlglot/executor/__init__.py", "sqlglot/time.py")
TEST_IMPORTS_ALWAYS = os.environ.get("BR_TEST_IMPORTS_ALWAYS", "0") == "1"
_TRIG_CACHE = {}


def keys_at_line(value, line):
    """keys of the table entries of `value` that sit on `line`, or None"""
    found = set()

    def go(n):
        if isinstance(n, ast.Dict):
            for k, v in zip(n.keys, n.values):
                lo = (k or v).lineno
                if lo <= line <= v.end_lineno:
                    if k is None:
                        return False
                    ks = static_keys(k)
                    if ks is None:
                        return False
                    found.update(ks)
            return True
        if isinstance(n, (ast.Set, ast.List, ast.Tuple)):
            for e in n.elts:
                if e.lineno <= line <= e.end_lineno:
                    ks = static_keys(e)
                    if ks is None:
                        return False
                    found.update(ks)
            return True
        if isinstance(n, ast.BinOp):
            return go(n.left) and go(n.right)
        if isinstance(n, ast.Call) and n.args:
            return all(go(a) for a in n.args)
        return False

    ok = go(value)
    return found if ok and found else None


def triggered_tests(trig, why):
    """tests affected when import-time statements may now compute something else"""
    out = set()
    for tf, tl in sorted(trig):
        if (tf, tl) in _TRIG_CACHE:
            out |= _TRIG_CACHE[(tf, tl)]
            continue
        sub = []
        tree = tree_of(tf)
        idx = stmt_index(tree)
        hit = innermost(idx, tl)
        res = set()
        if hit is None:
            res |= module_runtime_users(tf) | dialect_users_of_module(tf)
        else:
            s0, e0, st, chain = hit
            if isinstance(st, ast.ClassDef) and tl < st.body[0].lineno:
                res |= import_level_tests(tf, [(tl, st, chain)], sub)
            elif in_function(chain) is None:
                keys = None
                if isinstance(st, (ast.Assign, ast.AnnAssign)) and getattr(st, "value", None) is not None:
                    keys = keys_at_line(st.value, tl)
                classes = [n for n in chain if isinstance(n, ast.ClassDef)]
                names = assigned_names(st)
                modname = M["modnames"].get(tf)
                if classes and names and modname:
                    ckey = f"{modname}." + ".".join(c.name for c in classes)
                    if ckey in CLASSES:
                        KEYSET_CHANGED[0] = False
                        res |= attr_tests(ckey, names, sub, keys)
                    else:
                        res |= import_level_tests(tf, [(tl, st, chain)], sub)
                else:
                    res |= import_level_tests(tf, [(tl, st, chain)], sub)
            else:
                res |= module_runtime_users(tf) | dialect_users_of_module(tf)
        _TRIG_CACHE[(tf, tl)] = res
        out |= res
    why.append(f"import-time callers {len(trig)} -> {len(out)}")
    return out


def import_level_tests(path, items, why, new_tree=None):
    """Code run when the module is imported (module constants, class bodies)."""
    out = set()
    modname = M["modnames"].get(path)
    for ln, st, chain in items:
        classes = [n for n in chain if isinstance(n, ast.ClassDef)]
        if isinstance(st, ast.ClassDef):
            classes = classes + [st]
            ckey = f"{modname}." + ".".join(c.name for c in classes)
            # bases or decorators of a class: everyone using it or its subclasses
            subs = all_subs(ckey)
            out |= class_users(subs) | dialect_users_of_classes(subs)
            why.append(f"{ckey}: class header -> {len(out)}")
            continue
        if classes and modname:
            ckey = f"{modname}." + ".".join(c.name for c in classes)
            names = assigned_names(st) if st is not None else []
            if isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and names and ckey in CLASSES:
                keys = None
                KEYSET_CHANGED[0] = True
                if len(names) == 1 and new_tree is not None and isinstance(st, (ast.Assign, ast.AnnAssign)):
                    nst = new_class_stmt(new_tree, [c.name for c in classes], names[0])
                    DATA_VALUE[0] = (nst is not None and getattr(st, "value", None) is not None
                                     and is_data_value(st.value) and is_data_value(nst.value))
                    if nst is not None:
                        keys = changed_keys(st.value, nst.value)
                        if names[0] == "NUMERIC_ESCAPES" and keys is not None:
                            o = flatten_value(st.value)[1]
                            n = flatten_value(nst.value)[1]
                            NUMERIC_KEYS_SAME[0] = set(o) == set(n)
                        if keys is not None and not keys:
                            why.append(f"{ckey}.{names[0]}: no entry changed")
                            continue
                out |= attr_tests(ckey, names, why, keys)
                DATA_VALUE[0] = False
            elif isinstance(st, ast.Expr) and isinstance(getattr(st, "value", None), ast.Constant):
                continue  # docstring
            else:
                subs = all_subs(ckey)
                out |= class_users(subs) | dialect_users_of_classes(subs)
                why.append(f"{ckey}: class-level statement -> {len(out)}")
            continue
        if (DATA_RULE and isinstance(st, ast.Expr) and isinstance(getattr(st, "value", None), ast.Constant)
                and isinstance(st.value.value, str) and st.lineno == tree_of(path).body[0].lineno):
            why.append(f"{path}: module docstring -> nothing")  # nothing in sqlglot or its tests reads it
            continue
        out |= module_level_tests(path, st, why)
    if path.startswith("sqlglot/typing/") and T_TYPING_META is not None:
        out.add(T_TYPING_META)
    return out


def dialect_users_of_classes(classes):
    out = set()
    for dkey, info in M["dialects"].items():
        if classes & set(info["comps"]):
            out.update(info["users"])
    return out


def module_level_tests(path, st, why):
    out = set()
    names = assigned_names(st) if st is not None else []
    simple = isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and names and M["modnames"].get(path)
    if not simple:
        out |= module_runtime_users(path) | dialect_users_of_module(path)
        if T_LAZY_LOAD is not None:
            out.add(T_LAZY_LOAD)
        why.append(f"{path}:{getattr(st, 'lineno', '?')} module statement -> {len(out)}")
        return out
    out |= name_users(path, set(names), why, depth=0)
    why.append(f"{path}: module names {names} -> {len(out)}")
    return out


_IMPS = {}


def imports_of(path):
    """the import statements of path, in ast.walk order (cached)"""
    got = _IMPS.get(path)
    if got is None:
        got = _IMPS[path] = [st for st in ast.walk(tree_of(path)) if isinstance(st, (ast.Import, ast.ImportFrom))]
    return got


def name_users(path, names, why, depth):
    """Tests reached by module-level names of `path`: functions and classes that use them, in this
    module and in modules importing them by name."""
    out = set()
    if depth > 3 or not names:
        return out
    tree = tree_of(path)
    modname = M["modnames"].get(path)
    more = set()
    for st in tree.body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out |= stmt_users_in_function(path, st, names)
        elif isinstance(st, ast.ClassDef):
            out |= class_refs(path, f"{modname}.{st.name}", st, names, why)
        elif isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and getattr(st, "value", None) is not None:
            if referenced(st.value) & names:
                more.update(n for n in assigned_names(st) if n not in names)
        else:
            if referenced(st) & names:
                # some other module-level use (loops, calls): be broad
                out |= module_runtime_users(path) | dialect_users_of_module(path)
    if more:
        out |= name_users(path, names | more, why, depth + 1)
    if depth == 0 and modname:
        out |= test_code_users(modname, names)
    # other modules importing these names
    for q in IMPORTERS.get(path, ()):
        imported = set()
        for st in imports_of(q):
            if isinstance(st, ast.ImportFrom) and st.module and (st.module == modname or st.module.endswith("." + modname.rsplit(".", 1)[-1])):
                for a in st.names:
                    if a.name in names or a.name == "*":
                        imported.add(a.asname or a.name)
            elif isinstance(st, (ast.Import, ast.ImportFrom)):
                # module imported as a whole: attribute access mod.NAME
                if any((a.asname or a.name).split(".")[-1] == modname.rsplit(".", 1)[-1] for a in st.names):
                    imported |= names
        if imported:
            out |= name_users(q, imported, why, depth + 1)
    return out


_TCU = {}


def test_code_index(f):
    """for a test file: its import statements, and per simple statement the bare names and attribute
    names it uses with its lines"""
    idx = _TCU.get(f)
    if idx is not None:
        return idx
    tree = tree_of(f)
    imports, stmts = [], []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(node)
        if isinstance(node, ast.stmt) and not hasattr(node, "body"):
            ids, attrs = set(), set()
            for n in ast.walk(node):
                if isinstance(n, ast.Name):
                    ids.add(n.id)
                elif isinstance(n, ast.Attribute):
                    attrs.add(n.attr)
            stmts.append((frozenset(ids), frozenset(attrs), range(node.lineno, node.end_lineno + 1)))
    idx = _TCU[f] = (imports, stmts)
    return idx


def test_code_users(modname, names):
    """tests whose own code reads mod.NAME or imports NAME from the module"""
    out = set()
    short = modname.rsplit(".", 1)[-1]
    for f, ts in BY_FILE.items():
        imports, stmts = test_code_index(f)
        local = set()
        mod_alias = False
        for node in imports:
            if isinstance(node, ast.ImportFrom) and node.module == modname:
                for a in node.names:
                    if a.name in names or a.name == "*":
                        local.add(a.asname or a.name)
            elif any((a.asname or a.name).split(".")[-1] == short for a in node.names):
                mod_alias = True
        look = local | (names if mod_alias else set())
        if not look:
            continue
        for ids, attrs, lines in stmts:
            if (ids & local) or (mod_alias and (attrs & names)):
                got = line_tests(f, lines)
                out |= got if got else ts
    return out


_SUF = {}


def stmt_users_index(fn):
    """per statement inside fn: (names used in it, or in its header for compound statements, lines);
    and the names used by fn's decorators and defaults (cached)"""
    hit = _SUF.get(id(fn))
    if hit is not None and hit[0] is fn:
        return hit[1], hit[2]
    idx = []
    for node in ast.walk(fn):
        if isinstance(node, ast.stmt) and node is not fn:
            own = node
            # header only for compound statements
            hdr_end = header_end(own) if hasattr(own, "body") else own.end_lineno
            hdr_nodes = [own] if not hasattr(own, "body") else [getattr(own, f) for f in ("test", "iter", "items", "target", "value") if getattr(own, f, None) is not None]
            refs = set()
            for h in hdr_nodes:
                if isinstance(h, list):
                    for x in h:
                        refs |= referenced(x)
                else:
                    refs |= referenced(h)
            idx.append((frozenset(refs), range(own.lineno, hdr_end + 1)))
    hdr = set()
    for d in fn.decorator_list + fn.args.defaults + [x for x in fn.args.kw_defaults if x is not None]:
        hdr |= referenced(d)
    _SUF[id(fn)] = (fn, idx, frozenset(hdr))
    return idx, frozenset(hdr)


def stmt_users_in_function(path, fn, names):
    out = set()
    idx, hdr = stmt_users_index(fn)
    for refs, lines in idx:
        if refs & names:
            out |= line_tests(path, lines)
    # defaults / decorators evaluated at import
    if hdr & names:
        out |= line_tests(path, func_body_lines(fn))
    return out


def class_refs(path, ckey, node, names, why):
    out = set()
    hit = []
    for st in node.body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out |= stmt_users_in_function(path, st, names)
        elif isinstance(st, ast.ClassDef):
            out |= class_refs(path, f"{ckey}.{st.name}", st, names, why)
        elif isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and getattr(st, "value", None) is not None:
            if referenced(st.value) & names:
                hit.extend(assigned_names(st))
    if any(referenced(b) & names for b in node.bases + node.decorator_list):
        subs = all_subs(ckey)
        out |= class_users(subs) | dialect_users_of_classes(subs)
    if hit and ckey in CLASSES:
        out |= attr_tests(ckey, hit, why)
    return out


def test_class_family(name):
    """test classes named `name` or inheriting from it (by name), across all test files"""
    fam = {name}
    changed = True
    while changed:
        changed = False
        for f in BY_FILE:
            for node in ast.walk(tree_of(f)):
                if isinstance(node, ast.ClassDef) and node.name not in fam:
                    bases = {b.id if isinstance(b, ast.Name) else getattr(b, "attr", None) for b in node.bases}
                    if bases & fam:
                        fam.add(node.name)
                        changed = True
    out = set()
    for i, t in enumerate(MAP_TESTS):
        parts = t.split("::")
        if len(parts) >= 3 and parts[1] in fam:
            out.add(i)
    return out


def test_module_importers(path):
    """test files importing this test module (helpers, shared base classes)"""
    mod = path[:-3].replace("/", ".")
    short = mod.rsplit(".", 1)[-1]
    out = set()
    for f, ts in BY_FILE.items():
        if f == path:
            continue
        for node in ast.walk(tree_of(f)):
            if isinstance(node, ast.ImportFrom) and node.module and (node.module == mod or node.module.endswith("." + short)):
                out |= ts
                break
            if isinstance(node, ast.Import) and any(a.name == mod for a in node.names):
                out |= ts
                break
    return out


def select_test_py(path, old_text, hunks, why):
    tests = BY_FILE.get(path, set())
    if path.endswith("__init__.py"):
        # a package __init__ runs before every test module in that package
        pkg = path[: -len("__init__.py")]
        tests = set().union(*[ts for f, ts in BY_FILE.items() if f.startswith(pkg)])
    new_text = apply_hunks(old_text, hunks)
    try:
        old_tree = ast.parse(old_text)
        new_tree = ast.parse(new_text)
        compile(new_text.encode("utf-8"), path, "exec")
    except (SyntaxError, ValueError, UnicodeError):
        why.append(f"{path}: test file does not compile")
        return set(tests) | test_module_importers(path) or set(ALL)
    if ast.dump(old_tree) == ast.dump(new_tree):
        why.append(f"{path}: same AST")
        return set()
    index = stmt_index(old_tree)
    spans = string_spans(old_tree)
    old_lines = old_text.split("\n")
    targets = set()
    for rem, ins_at, texts in change_blocks(hunks):
        touched = set(rem) | ({ins_at - 1, ins_at} if ins_at else set())
        in_string = any(a <= ln <= b for ln in touched for a, b in spans)
        if all(is_comment_or_blank(t) for t in texts) and not in_string:
            continue
        if rem:
            targets.update(rem)
        else:
            up = ins_at - 1
            while up >= 1 and is_comment_or_blank(old_lines[up - 1]):
                up -= 1
            down = ins_at
            while down <= len(old_lines) and is_comment_or_blank(old_lines[down - 1]):
                down += 1
            if up >= 1:
                targets.add(up)
            if down <= len(old_lines):
                targets.add(down)
    out = set()
    for ln in sorted(targets):
        hit = innermost(index, ln)
        if hit is None:
            out |= tests | test_module_importers(path)
            continue
        start, end, st, chain = hit
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and ln >= st.body[0].lineno:
            continue
        cls = next((n for n in reversed(chain) if isinstance(n, ast.ClassDef)), None)
        fn = in_function(chain)
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)) and ln < st.body[0].lineno:
            # a def line: the test itself (renamed or re-signatured) and whoever calls it
            out |= line_tests(path, func_body_lines(st))
            if cls is not None and st.name.startswith("test"):
                out |= {i for i in tests if MAP_TESTS[i].endswith(f"::{cls.name}::{st.name}")}
            elif cls is not None:
                out |= test_class_family(cls.name)
            continue
        if isinstance(st, ast.ClassDef):
            out |= test_class_family(st.name)
            continue
        if fn is not None:
            hdr = range(start, header_end(st) + 1)
            got = line_tests(path, hdr)
            out |= got
            if cls is not None and fn.name.startswith("test"):
                # the test owning these lines, even if coverage missed them
                out |= {i for i in tests if MAP_TESTS[i].endswith(f"::{cls.name}::{fn.name}")}
            continue
        if cls is not None:
            out |= test_class_family(cls.name)
        else:
            out |= tests | test_module_importers(path)
    why.append(f"{path}: test code -> {len(out)}")
    return out


CHECK_SCRIPT = r"""
import importlib, importlib.abc, importlib.util, json, os, pkgutil, sys
D = json.load(open(sys.argv[1]))
P = D["patched"]
class Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, name, path, target=None):
        if name not in P:
            return None
        fn, src = P[name]
        pkg = fn.endswith("__init__.py")
        spec = importlib.util.spec_from_loader(name, self, origin=fn, is_package=pkg)
        if pkg:
            spec.submodule_search_locations = [os.path.dirname(fn)]
        spec.has_location = True
        return spec
    def create_module(self, spec):
        return None
    def exec_module(self, module):
        fn, src = P[module.__name__]
        module.__file__ = fn
        exec(compile(src, fn, "exec"), module.__dict__)
sys.meta_path.insert(0, Finder())
import logging
logging.disable(logging.CRITICAL)
import sqlglot
for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
    if info.name.endswith("__main__"):
        continue
    importlib.import_module(info.name)
from sqlglot.dialects.dialect import Dialect
for d in list(Dialect.classes):
    Dialect.get_or_raise(d)
# test modules that no longer import: every test in them fails
bad = []
for name in D.get("tests", []):
    try:
        importlib.import_module(name)
    except BaseException:
        bad.append(name)
sys.stdout.write("\n" + json.dumps(bad) + "\n")
"""
SG_TREE = HERE / "sg"
# one thread per numeric library: the scorer counts threads against a 256-process limit
THREAD_ENV = {k: "1" for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                               "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "RAYON_NUM_THREADS",
                               "POLARS_MAX_THREADS", "ARROW_NUM_THREADS")}
THREAD_ENV["LANG"] = "C.UTF-8"


TEST_MODULES = sorted({f[:-3].replace("/", ".") for f in BY_FILE if f.endswith(".py")})


def import_check(patched, test_patches=None):
    """-> (ok, test files that no longer import). ok is False if sqlglot with these patched modules no
    longer imports (every module, every dialect)."""
    import subprocess
    import tempfile

    if not SG_TREE.exists():
        return True, []
    payload = {}
    for path, text in list(patched.items()) + [(p, t) for p, t in (test_patches or {}).items()
                                           if p.endswith(".py") and isinstance(t, str)]:
        mod = path[:-3].replace("/", ".")
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        payload[mod] = [str(SG_TREE / path), text]
    with tempfile.NamedTemporaryFile("w", suffix=".json", dir="/tmp", delete=False) as fh:
        need_tests = NEED_TEST_IMPORTS[0] or TEST_IMPORTS_ALWAYS or bool(test_patches)
        json.dump({"patched": payload, "tests": TEST_MODULES if need_tests else []}, fh)
        name = fh.name
    try:
        if FORK and FORK_OK[0]:
            try:
                ok, bad = check_forked(name, 60)
                if not ok:
                    log("import check failed")
                return ok, [m.replace(".", "/") + ".py" for m in bad]
            except OSError as e:  # no fork here: use a fresh interpreter instead
                log(f"fork unavailable {e!r}")
                FORK_OK[0] = False
        r = subprocess.run([sys.executable, "-c", CHECK_SCRIPT, name], cwd="/tmp",
                           env=dict(THREAD_ENV, PYTHONPATH=str(SG_TREE), PYTHONDONTWRITEBYTECODE="1", HOME="/tmp",
                                    PATH=os.environ.get("PATH", ""), SKIP_INTEGRATION="0"),
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            log("import check failed: " + r.stderr.strip().splitlines()[-1][:200] if r.stderr.strip() else "import check failed")
            return False, []
        bad = json.loads(r.stdout.strip().splitlines()[-1])
        return True, [m.replace(".", "/") + ".py" for m in bad]
    except Exception as e:
        log(f"import check error {e!r}")
        return False, []
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass


def keep_gone(ctx, path, got):
    """tests of a deleted or renamed-away test file no longer exist, so they fail: never drop them
    after running the pinned copies here"""
    ctx.setdefault("keep_ids", set()).update(MAP_TESTS[i] for i in got)
    ctx["keep_ids"].update(MAP_TESTS[i] for i in BY_FILE.get(path, ()))


def select_commit(patch_text, why, ctx=None):
    if ctx is None:
        ctx = {}
    out = set()
    patched = {}
    test_patches = {}
    ctx["patched"] = patched
    ctx["tests"] = test_patches
    kinds = set()
    NEED_IMPORT_CHECK[0] = False
    NEED_TEST_IMPORTS[0] = False
    for f in parse_patch(patch_text):
        path = f["old"] if f["status"] != "added" else f["new"]
        newpath = f["new"]
        if path is None:
            continue
        if f["status"] == "renamed" and path != newpath:
            # renaming a module or fixture = deleting the old one
            got = deleted_file_tests(path, why)
            out |= got
            if path.startswith("tests/"):
                keep_gone(ctx, path, got)
            continue
        if path.startswith("tests/fixtures/"):
            kinds.add("fixture")
            if f["binary"] or f["status"] != "modified" or not path.endswith((".sql", ".json")):
                ctx["unknown_files"] = True
            else:
                orig = fixture_text(path)
                if orig is None:
                    ctx["unknown_files"] = True
                else:
                    test_patches[path] = apply_hunks(orig, f["hunks"])
            ts = set(M["fixtures"].get(path, ()))
            if f["status"] == "added":
                ts = set()
            texts = [t for _, body in f["hunks"] for tag, t in body if tag in "+-"]
            if (path.endswith(".sql") and f["status"] == "modified" and not f["binary"] and texts
                    and all(t == "" or t.startswith("--") for t in texts)
                    and all(SQL_FIXTURE_READERS_FILTER(ts))):
                # the SQL fixture loaders drop empty lines and lines starting with "--"
                why.append(f"{path}: fixture comments only")
                continue
            why.append(f"{path}: fixture -> {len(ts)}")
            out |= ts
            continue
        base = os.path.basename(path)
        if path.startswith("tests/") and (base == "conftest.py" or (base == "__init__.py" and f["status"] != "modified")):
            # pytest configuration, or a package marker appearing or vanishing: every test below it
            d = os.path.dirname(path) + "/"
            got = {i for f2, ts in BY_FILE.items() if f2.startswith(d) for i in ts}
            why.append(f"{path}: {base} {f['status']} -> {len(got)}")
            out |= got
            ctx["unknown_files"] = True
            continue
        if path.endswith(".py") and (path.startswith("sqlglot/") or path.startswith("tests/")):
            if f["status"] == "deleted":
                got = deleted_file_tests(path, why)
                out |= got
                if path.startswith("tests/"):
                    keep_gone(ctx, path, got)
                continue
            if f["status"] == "added":
                old_text = ""
            else:
                old_text = SRC.get(path)
                if old_text is None:
                    why.append(f"{path}: unknown file -> all")
                    out |= ALL
                    continue
            if path.startswith("sqlglot/"):
                kinds.add("code")
                patched[path] = apply_hunks(old_text, f["hunks"])
                if f["status"] == "added":
                    NEED_TEST_IMPORTS[0] = True
                    out |= new_module_tests(path, f["hunks"], why)
                else:
                    out |= select_python(path, old_text, f["hunks"], why)
            else:
                kinds.add("test")
                if f["status"] == "added":
                    ctx["unknown_files"] = True
                    continue
                test_patches[path] = apply_hunks(old_text, f["hunks"])
                out |= select_test_py(path, old_text, f["hunks"], why)
            continue
        if os.path.basename(path) in ("conftest.py", "pytest.ini", "tox.ini") or path in ("pyproject.toml", "setup.cfg"):
            texts = [t for _, body in f["hunks"] for tag, t in body if tag in "+-"]
            if all(is_comment_or_blank(t) for t in texts):
                why.append(f"{path}: comments only")
                continue
            if path in ("pyproject.toml", "setup.cfg") and not any(
                re.search(r"pytest|filterwarnings|addopts|testpaths|python_files|markers", t) for t in texts
            ) and not any("[tool.pytest" in SRC_CFG_SECTION(path, f["hunks"]) for _ in [0]):
                why.append(f"{path}: packaging config -> sqlglotc tests")
                out |= BY_FILE.get("tests/test_sqlglotc.py", set())
                continue
            why.append(f"{path}: test configuration -> all")
            out |= ALL
            continue
        if path.startswith("sqlglotc/") or path in ("setup.py",):
            ts = BY_FILE.get("tests/test_sqlglotc.py", set())
            why.append(f"{path}: build file -> sqlglotc tests")
            out |= ts
            continue
        why.append(f"{path}: not loaded by tests -> nothing")
    ctx["fixture_only"] = kinds == {"fixture"}
    ctx["has_fixture"] = "fixture" in kinds
    if NEED_IMPORT_CHECK[0] and patched and len(out) < len(ALL):
        ok, bad = import_check(patched, ctx.get("tests"))
        if not ok:
            why.append("patched sqlglot fails to import -> all")
            out |= ALL
        else:
            for f in bad:
                out |= BY_FILE.get(f, set())
            why.append(f"import check ok; test files that no longer import: {bad}" if bad else "import check ok")
    return out


def SRC_CFG_SECTION(path, hunks):
    """name of the [section] the first hunk falls in, from the hunk header context"""
    return ""


def fixture_text(path):
    p = SG_TREE / path
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return None


def SQL_FIXTURE_READERS_FILTER(ts):
    """every reader goes through tests.helpers.load_sql_fixture(s|_pairs)"""
    files = {MAP_TESTS[i].split("::")[0] for i in ts}
    return [f in ("tests/test_optimizer.py", "tests/test_transpile.py", "tests/test_serde.py", "tests/test_executor.py") for f in files]


def deleted_file_tests(path, why):
    if path.startswith("tests/fixtures/"):
        return set(M["fixtures"].get(path, ()))
    if path.startswith("sqlglot/"):
        why.append(f"{path}: deleted module -> all")
        return set(ALL)
    if path.startswith("tests/"):
        return set(BY_FILE.get(path, ALL))
    return set()


def new_module_tests(path, hunks, why):
    text = apply_hunks("", hunks)
    try:
        compile(text, path, "exec")
    except SyntaxError:
        pass
    # a new module is only reached by code that imports or walks the package
    out = set()
    for f, ts in BY_FILE.items():
        src = SRC.get(f, "")
        if "walk_packages" in src or "iter_modules" in src:
            out |= ts
    if path.startswith("sqlglot/typing/") and T_TYPING_META is not None:
        out.add(T_TYPING_META)
    why.append(f"{path}: new module -> {len(out)}")
    return out


# ----------------------------------------------------------------------------- packing
def pack(nums, secs):
    """choose worker count and assignment minimising max load + 2 s per worker."""
    if not nums:
        return []
    ms = {n: round(secs[n] * 1000) for n in nums}
    order = sorted(nums, key=lambda n: (-ms[n], n))
    best = None
    for k in range(1, MAX_WORKERS + 1):
        if k > len(order):
            break
        loads = [0] * k
        bins = [[] for _ in range(k)]
        for n in order:
            j = min(range(k), key=lambda x: (loads[x], x))
            loads[j] += ms[n]
            bins[j].append(n)
        cost = max(loads) + 2000 * k
        if best is None or cost < best[0]:
            best = (cost, bins)
    return best[1]


# ----------------------------------------------------------------------------- running tests here
# Tests can be run inside this container (charged as CPU time). Results are exact for the tests run;
# tests that cannot be run faithfully in one process are never dropped on that basis.
SPEC_E = os.environ.get("BR_SPEC_E", "1") == "1"      # drop expensive tests when every cheap one passes
RUN_SMALL = os.environ.get("BR_RUN_SMALL", "1") == "1"  # run small selections, keep only failures
EXP_T = 2.0       # "expensive": not worth running here
BIG_T = float(os.environ.get("BR_BIG_T", "5.0"))   # worth betting on
MIN_EVIDENCE = 3  # cheap tests that must pass before a big one is dropped
PARTIAL = os.environ.get("BR_PARTIAL", "0") == "1"     # also run the cheap part of a selection with costly tests
RS_RATIO = float(os.environ.get("BR_RS_RATIO", "0.5"))
NO_ANYK = os.environ.get("BR_NO_ANYK", "0") == "1"   # experiment only
SPEC_RATIO = float(os.environ.get("BR_SPEC_RATIO", "1.0"))
UNRUNNABLE_IDS = ("tests/test_sqlglotc.py::", "::test_lazy_load", "::test_generator_memory", "::test_max_nodes",
                  "::test_no_redundant_expression_metadata")
START = None
HARD_T = float(os.environ.get("BR_HARD_T", "150"))  # seconds into the batch after which every commit runs all
REFINE_T = float(os.environ.get("BR_REFINE_T", "120"))  # seconds into the batch after which no more test runs


def runnable(tid):
    return not any(u in tid for u in UNRUNNABLE_IDS)


def overlay_tests(test_patches):
    """a tests/ tree under /tmp: symlinks to the pinned files, patched files written out"""
    import shutil
    import tempfile

    base = SG_TREE / "tests"
    root = tempfile.mkdtemp(dir="/tmp", prefix="ov")
    for dp, dn, fn in os.walk(base):
        rel = os.path.relpath(dp, SG_TREE)
        os.makedirs(os.path.join(root, rel), exist_ok=True)
        for f in fn:
            if f.endswith(".pyc"):
                continue
            src = os.path.join(dp, f)
            dst = os.path.join(root, rel, f)
            r = os.path.join(rel, f)
            if r in test_patches:
                with open(dst, "w") as fh:
                    fh.write(test_patches[r])
            else:
                os.symlink(src, dst)
    return root


FORK = os.environ.get("BR_FORK", "1") == "1"   # run tests in a forked copy of this process
FORK_OK = [True]
_WARM = [False]


def warm_up():
    """Load the test libraries once here, so each forked test run starts with them loaded."""
    if _WARM[0]:
        return
    _WARM[0] = True
    os.environ.update(THREAD_ENV)
    os.environ["HOME"] = "/tmp"
    sys.path.insert(1, str(HERE))
    import runner  # noqa: F401

    for name in ("unittest.mock", "multiprocessing", "sqlite3", "decimal", "csv", "datetime", "tempfile",
                 "numpy", "duckdb", "pandas", "pandas.testing"):
        try:
            importlib_import(name)
        except Exception:  # noqa
            pass


def importlib_import(name):
    import importlib

    return importlib.import_module(name)


def fork_call(func, budget):
    """func() -> bytes, run in a forked child of this process; -> bytes, or None if it failed or timed out"""
    import gc
    import select as _select  # the standard library module (this file is blast.py)
    import signal
    import time

    warm_up()
    gc.freeze()  # the child would otherwise copy every page of this process when its gc walks old objects
    r, w = os.pipe()
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:  # child: never returns
        code = 1
        try:
            os.close(r)
            fd = os.open(os.devnull, os.O_RDWR)
            os.dup2(fd, 0)
            os.dup2(fd, 1)
            os.dup2(fd, 2)
            os.chdir("/tmp")
            sys.dont_write_bytecode = True
            os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
            os.environ["PYTHONPATH"] = str(SG_TREE)
            os.environ["SKIP_INTEGRATION"] = "0"
            sys.path.insert(0, str(SG_TREE))
            view = memoryview(func())
            while view:
                n = os.write(w, view)
                view = view[n:]
            code = 0
        except BaseException:  # noqa
            code = 1
        finally:
            os._exit(code)
    os.close(w)
    chunks = []
    deadline = time.time() + budget
    ok = False
    try:
        while True:
            left = deadline - time.time()
            if left <= 0:
                break
            ready, _, _ = _select.select([r], [], [], left)
            if not ready:
                break
            chunk = os.read(r, 1 << 16)
            if not chunk:
                ok = True
                break
            chunks.append(chunk)
    finally:
        os.close(r)
        if not ok:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        _, status = os.waitpid(pid, 0)
    if not ok or status != 0:
        log("forked call failed" if ok else "forked call timed out")
        return None
    return b"".join(chunks)


def run_forked(spec, budget):
    """run the spec in a forked child; -> set of failing ids, or None"""

    def job():
        import runner

        runner.prepare(spec)
        return json.dumps({"failed": sorted(runner.run(spec))}).encode()

    out = fork_call(job, budget)
    return None if out is None else set(json.loads(out.decode())["failed"])


def check_forked(spec_file, budget):
    """the import check in a forked child; -> (ok, bad test modules)"""

    def job():
        sys.argv = ["check", spec_file]
        g = {"__name__": "__check__"}
        exec(compile(CHECK_SCRIPT, "<check>", "exec"), g)
        return json.dumps(g["bad"]).encode()

    out = fork_call(job, budget)
    return (False, []) if out is None else (True, json.loads(out.decode()))


def run_here(ids, patched, test_patches, failfast, budget):
    """-> set of failing ids, or None if the run could not finish"""
    import subprocess
    import tempfile

    if not (SG_TREE / "tests").exists():
        return None
    root = str(SG_TREE)
    ov = None
    if test_patches:
        ov = overlay_tests(test_patches)
        root = ov
    payload = {}
    for path, text in patched.items():
        mod = path[:-3].replace("/", ".")
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        payload[mod] = [str(SG_TREE / path), text]
    spec = {"patched": payload, "tests": ids, "tests_root": root, "failfast": failfast}
    if FORK and FORK_OK[0]:
        try:
            return run_forked(spec, budget)
        except OSError as e:  # no fork here: use a fresh interpreter instead
            log(f"fork unavailable {e!r}")
            FORK_OK[0] = False
        except Exception as e:
            log(f"fork error {e!r}")
            return None
        finally:
            if ov and FORK_OK[0]:
                import shutil
                shutil.rmtree(ov, ignore_errors=True)
    with tempfile.NamedTemporaryFile("w", suffix=".json", dir="/tmp", delete=False) as fh:
        json.dump(spec, fh)
        name = fh.name
    try:
        r = subprocess.run([sys.executable, str(HERE / "runner.py"), name], cwd="/tmp",
                           env=dict(THREAD_ENV, PYTHONPATH=str(SG_TREE), PYTHONDONTWRITEBYTECODE="1", HOME="/tmp",
                                    PATH=os.environ.get("PATH", ""), SKIP_INTEGRATION="0"),
                           capture_output=True, text=True, timeout=budget)
        if r.returncode != 0:
            return None
        return set(json.loads(r.stdout.strip().splitlines()[-1])["failed"])
    except Exception as e:
        log(f"run error {e!r}")
        return None
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass
        if ov:
            import shutil
            shutil.rmtree(ov, ignore_errors=True)


def pack_cost(nums, secs):
    w = pack(nums, secs)
    if not w:
        return 0.0
    return max(sum(round(secs[n] * 1000) for n in b) for b in w) / 1000 + 2 * len(w)


def refine(nums, secs, ids, ctx, why):
    """maybe run some selected tests here and return a smaller selection"""
    import time

    if not nums or ctx.get("unknown_files"):
        return nums
    if time.time() - START > REFINE_T:
        return nums
    keep_ids = ctx.get("keep_ids", set())
    runn = [n for n in nums if runnable(ids[n]) and ids[n] not in keep_ids]
    fixed = [n for n in nums if not runnable(ids[n]) or ids[n] in keep_ids]
    big = [n for n in runn if secs[n] >= BIG_T]
    exp = [n for n in runn if secs[n] >= EXP_T]
    mid = [n for n in exp if secs[n] < BIG_T]
    cheap = [n for n in runn if secs[n] < EXP_T]
    est = 0.4 + 1.3 * sum(secs[n] for n in cheap)
    base = pack_cost(nums, secs)
    # dropping the big tests saves at best this much, and only if every cheap test passes
    spec_saving = base - (pack_cost(sorted(set(fixed) | set(mid)), secs) if (fixed or mid) else 0.0)
    if (big and len(cheap) >= MIN_EVIDENCE and SPEC_E and not ctx.get("fixture_only")
            and est < SPEC_RATIO * spec_saving):
        failed = run_here([ids[n] for n in cheap], ctx["patched"], ctx["tests"], True, 20 + 4 * est)
        if failed is None or failed:
            why.append(f"ran {len(cheap)} cheap: {'failure' if failed else 'no result'} -> keep all")
            return nums
        why.append(f"ran {len(cheap)} cheap: all pass -> drop {len(big)} big, keep {len(mid)} mid")
        return sorted(set(fixed) | set(mid))
    # what dropping every cheap test would save at best; worth a run if the run costs less than half of it
    saving = base - (pack_cost(sorted(set(fixed) | set(exp)), secs) if (fixed or exp) else 0.0)
    if (PARTIAL or not exp) and cheap and RUN_SMALL and not ctx.get("has_fixture") and est < RS_RATIO * saving:
        failed = run_here([ids[n] for n in cheap], ctx["patched"], ctx["tests"], False, 20 + 4 * est)
        if failed is None:
            return nums
        keep = sorted(set(fixed) | set(exp) | {n for n in cheap if ids[n] in failed})
        why.append(f"ran {len(cheap)}: {len([n for n in cheap if ids[n] in failed])} fail")
        return keep
    return nums


# ----------------------------------------------------------------------------- tpcds equivalence
# test_execute_tpcds (about 21 s, the costliest test that changes often) feeds each of 57 queries
# through parse -> optimize -> plan, runs the plan on sqlglot's python executor and compares with
# duckdb running the query transpiled by sqlglot. If a patch only changes functions that never run in
# the rest of the test (stage B: table setup, pickling, the executor, result checks, module import;
# map/stageb_tpcds.json.gz holds those lines on the pinned tree), and the parsed queries, plans and
# duckdb SQL come out identical, the test sees exactly its pinned inputs and passes as it does there.
EQ = os.environ.get("BR_EQ", "1") == "1"
TPCDS_ID = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
_STAGEB = {}
_EQ_BASE = []


def eq_functions(tree):
    """-> {(qualname, n): node}; n counts repeated qualnames in source order"""
    out = {}
    seen = {}

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = prefix + child.name
                k = seen.get(q, 0)
                seen[q] = k + 1
                out[(q, k)] = child
                walk(child, q + ".<locals>.")
            elif isinstance(child, ast.ClassDef):
                walk(child, prefix + child.name + ".")
            else:
                walk(child, prefix)

    walk(tree, "")
    return out


def eq_span(fn):
    first = min([fn.lineno] + [d.lineno for d in fn.decorator_list])
    return first, fn.end_lineno


def eq_innermost(funcs, line):
    best = None
    for key, fn in funcs.items():
        a, b = eq_span(fn)
        if a <= line <= b and (best is None or a >= eq_span(funcs[best])[0]):
            best = key
    return best


def eq_body_lines(fn):
    ls = set()
    for st in fn.body:
        ls.update(range(st.lineno, st.end_lineno + 1))
    return ls


def changed_functions(old_text, new_text, hunks):
    """-> set of (qualname, n) of old functions holding every change, or None if a change sits
    outside any function, or functions were added, removed or renamed"""
    try:
        old_t, new_t = ast.parse(old_text), ast.parse(new_text)
    except SyntaxError:
        return None
    fo, fn = eq_functions(old_t), eq_functions(new_t)
    if set(fo) != set(fn):
        return None
    out = set()
    delta = 0
    for ostart, body in hunks:
        o = ostart
        n = ostart + delta
        for tag, _ in body:
            if tag == " ":
                o += 1
                n += 1
            elif tag == "-":
                k = eq_innermost(fo, o)
                if k is None:
                    return None
                out.add(k)
                o += 1
            else:
                k = eq_innermost(fn, n)
                if k is None:
                    return None
                out.add(k)
                n += 1
        delta += sum(1 for t, _ in body if t == "+") - sum(1 for t, _ in body if t == "-")
    # the same function must hold the change on both sides: a changed def line sits in its own span
    return out, fo


def stageb():
    if not _STAGEB:
        try:
            _STAGEB.update({f: set(v) for f, v in json.load(gzip.open(HERE / "map" / "stageb_tpcds.json.gz", "rt")).items()})
        except Exception as e:  # noqa
            log(f"no stage B map {e!r}")
            _STAGEB["?"] = None
    return None if "?" in _STAGEB else _STAGEB


def eq_eligible(patch_text):
    """-> None, or a reason the check cannot apply"""
    B = stageb()
    if B is None:
        return "no map"
    files = parse_patch(patch_text)
    if not files:
        return "empty"
    for f in files:
        p = f["old"]
        if (f["status"] != "modified" or f["binary"] or p != f["new"] or not p
                or not (p.startswith("sqlglot/") and p.endswith(".py"))):
            return f"{p}: not a sqlglot module edit"
        old = SRC.get(p)
        if old is None:
            return f"{p}: unknown"
        try:
            new = apply_hunks(old, f["hunks"])
        except ValueError:
            return f"{p}: does not apply"
        r = changed_functions(old, new, f["hunks"])
        if r is None:
            return f"{p}: change outside a function"
        keys, fo = r
        for k in keys:
            if eq_body_lines(fo[k]) & B.get(p, set()):
                return f"{p}: {k[0]} runs in the executor part"
    return None


EQ_MAX_DIFF = int(os.environ.get("BR_EQ_MAX_DIFF", "4"))


def eq_fingerprints(patched, base, budget):
    """baseline (base None): -> list of digests. patched: -> "same", "pass" or "differ". None if failed"""

    def job():
        import runner
        runner.prepare({"patched": patched, "tests_root": str(SG_TREE)})
        sys.path.insert(0, str(HERE))
        import tpcfp
        if base is None:
            return json.dumps(tpcfp.fingerprints(str(SG_TREE), "tpcds")).encode()
        return json.dumps(tpcfp.check(str(SG_TREE), base, EQ_MAX_DIFF)).encode()

    out = fork_call(job, budget)
    return None if out is None else json.loads(out.decode())


def eq_check(ctx, patch_text, why):
    """True if test_execute_tpcds provably sees its pinned inputs"""
    try:
        return _eq_check(ctx, patch_text, why)
    except Exception as e:  # noqa  never let the check cost more than keeping the test
        why.append(f"tpcds check error {e!r}")
        return False


def _eq_check(ctx, patch_text, why):
    import time

    if not EQ or not (FORK and FORK_OK[0]) or ctx.get("tests") or time.time() - START > REFINE_T:
        return False
    reason = eq_eligible(patch_text)
    if reason:
        why.append(f"tpcds check n/a: {reason}")
        return False
    payload = {}
    for path, text in ctx["patched"].items():
        mod = path[:-3].replace("/", ".")
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        payload[mod] = [str(SG_TREE / path), text]
    if not _EQ_BASE:
        # made at image build (Dockerfile), in this same image; otherwise worked out here once
        try:
            b = json.load(open(HERE / "map" / "tpcds_fp.json"))
        except Exception:  # noqa
            b = eq_fingerprints({}, None, 60)
        _EQ_BASE.append(b if b else None)
    base = _EQ_BASE[0]
    if not base:
        why.append("tpcds check: no baseline")
        return False
    got = eq_fingerprints(payload, base, 60)
    if got not in ("same", "pass"):
        why.append(f"tpcds check: inputs differ" if got is not None else "tpcds check: no result")
        return False
    why.append("tpcds inputs unchanged -> drop test_execute_tpcds" if got == "same" else
               "tpcds: only some queries changed, ran them here, all pass -> drop test_execute_tpcds")
    return True


# ----------------------------------------------------------------------------- one-operator swaps
# map/opmap.json.gz (tools/opmap_plug.py, pinned tree only): for every single comparison in sqlglot,
# which tests evaluated it, and in which of those a sibling operator would have answered differently
# on the very operands the test compared. A patch that only swaps that one operator leaves every other
# test's run identical, step for step, so those tests cannot change outcome. The map holds compared
# values' outcomes, never patches or failures: what the new operator does is decided here.
OPMAP = os.environ.get("BR_OPMAP", "1") == "1"
_OPM = {}


def opmap():
    if not _OPM:
        try:
            _OPM.update(json.load(gzip.open(HERE / "map" / "opmap.json.gz", "rt")))
        except Exception as e:  # noqa
            log(f"no operand map {e!r}")
            _OPM["?"] = 1
    return None if "?" in _OPM else _OPM


def single_swap(old_text, new_text):
    """-> (line, col, old op, new op) when the files differ only in one comparison's operator"""
    try:
        a, b = ast.parse(old_text), ast.parse(new_text)
    except SyntaxError:
        return None
    na, nb = list(ast.walk(a)), list(ast.walk(b))
    if len(na) != len(nb):
        return None
    diffs = []
    for x, y in zip(na, nb):
        if isinstance(x, ast.cmpop) and isinstance(y, ast.cmpop):
            continue
        if type(x) is not type(y):
            return None
        if isinstance(x, ast.Compare) and [type(o) for o in x.ops] != [type(o) for o in y.ops]:
            if len(x.ops) != 1 or len(y.ops) != 1:
                return None
            diffs.append((x, y))
    if len(diffs) != 1:
        return None
    x, y = diffs[0]
    saved = y.ops
    y.ops = x.ops
    same = ast.dump(a) == ast.dump(b)
    y.ops = saved
    return (x.lineno, x.col_offset, type(x.ops[0]).__name__, type(y.ops[0]).__name__) if same else None


def op_narrow(nums, ctx, patch_text, why):
    if not OPMAP or ctx.get("tests"):
        return nums
    try:
        files = parse_patch(patch_text)
        if len(files) != 1:
            return nums
        f = files[0]
        p = f["old"]
        if f["status"] != "modified" or f["binary"] or p != f["new"] or p not in ctx.get("patched", {}):
            return nums
        sw = single_swap(SRC[p], ctx["patched"][p])
        if not sw:
            return nums
        M = opmap()
        if M is None:
            return nums
        line, col, old, new = sw
        site = M["sites"].get(f"{p}:{line}:{col}")
        if site is None or site[0] != old or new not in site[1]:
            why.append(f"{p}:{line} {old}->{new}: no operand record")
            return nums
        sid = str(site[2])
        if f"{sid}:{new}" in M["shared_dis"]:
            why.append(f"{p}:{line} {old}->{new}: answers differently while modules load")
            return nums
        hit = set(M["hit"].get(sid, ()))
        dis = set(M["dis"].get(f"{sid}:{new}", ()))
        keep = [n for n in nums if n not in hit or n in dis]
        why.append(f"{p}:{line} {old}->{new}: same answer on every recorded operand for "
                   f"{len(nums) - len(keep)} tests -> {len(keep)}")
        return keep
    except Exception as e:  # noqa  the map only ever removes tests; on doubt keep them
        why.append(f"operand check error {e!r}")
        return nums


def main():
    tests_json = json.loads((DATA / "tests.json").read_text())
    tests = tests_json["tests"]
    num_by_id = {t["id"]: t["number"] for t in tests}
    secs = {t["number"]: t["seconds"] for t in tests}
    map_to_num = {i: num_by_id[t] for i, t in enumerate(MAP_TESTS) if t in num_by_id}
    unknown = [t["number"] for t in tests if t["id"] not in set(MAP_TESTS)]
    if unknown:
        log(f"{len(unknown)} tests in tests.json are not in the map; they run on every non-empty selection")

    import time

    global START
    START = time.time()
    ids = {t["number"]: t["id"] for t in tests}
    for patch in sorted((DATA / "commits").glob("*.patch")):
        why = []
        if time.time() - START > HARD_T:
            # far over the usual time: finish the batch safely instead of risking the time limit
            nums = [t["number"] for t in tests]
            workers = pack(nums, secs)
            print(json.dumps({"commit": patch.stem, "workers": workers}), flush=True)
            log(f"{patch.stem}: out of time -> all")
            continue
        try:
            ctx = {}
            sel = select_commit(patch.read_text(errors="replace"), why, ctx)
            nums = sorted({map_to_num[i] for i in sel if i in map_to_num})
            if nums or any("-> all" in w for w in why):
                nums = sorted(set(nums) | set(unknown))
            if not unknown and not any("-> all" in w for w in why):
                nums = op_narrow(nums, ctx, patch.read_text(errors="replace"), why)
                nums = refine(nums, secs, ids, ctx, why)
                # still keeping test_execute_tpcds (no cheap run, or a cheap test failed)
                tn = [n for n in nums if ids[n] == TPCDS_ID]
                if tn and ids[tn[0]] not in ctx.get("keep_ids", set()):
                    rest = [n for n in nums if n != tn[0]]
                    if pack_cost(nums, secs) - pack_cost(rest, secs) > 6 and eq_check(ctx, patch.read_text(errors="replace"), why):
                        nums = rest
        except Exception as e:  # never crash: run everything for this commit
            log(f"{patch.stem}: error {e!r}; running all")
            nums = [t["number"] for t in tests]
        # PROBE BUILD, never a final entry: drop test_execute_tpcds only when the tpcds check found changed inputs
        if any("tpcds check: inputs differ" in w for w in why):
            nums = [n for n in nums if ids[n] != TPCDS_ID]
        workers = pack(nums, secs)
        print(json.dumps({"commit": patch.stem, "workers": workers}), flush=True)
        log(f"{patch.stem}: {len(nums)} tests, {len(workers)} workers | " + "; ".join(why)[:400])


if __name__ == "__main__":
    main()
