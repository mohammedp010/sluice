"""Benign side of the tool-lint eval: tool and parameter descriptions from real MCP servers.

    python bench/tool_corpus.py <checkout of modelcontextprotocol/servers> <commit sha>

Reads the reference servers' source as text and never runs or imports it: Python through `ast`,
TypeScript through a small string-literal scanner. Takes tool descriptions (`Tool(description=)`,
`registerTool(..., {description})`) and parameter descriptions (`Field(description=)`,
zod `.describe()`), joining `"a" + "b"` and resolving module-level string constants.
Interpolations it can't resolve stay as `{name}`.

Writes bench/data/tool_descriptions_benign.jsonl, one string per line. Feeds
bench/tool_lint_eval.py.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

OUT = Path(__file__).parent / "data" / "tool_descriptions_benign.jsonl"
SERVERS = ("everything", "fetch", "filesystem", "git", "memory", "sequentialthinking", "time")


# --- Python -------------------------------------------------------------------------------

def _py_consts(tree: ast.Module) -> dict[str, str]:
    """Module constants and Enum-style class attributes that are plain strings."""
    out: dict[str, str] = {}
    for node in tree.body:
        targets = [node] if isinstance(node, ast.Assign) else []
        if isinstance(node, ast.ClassDef):
            targets = [n for n in node.body if isinstance(n, ast.Assign)]
        for a in targets:
            if len(a.targets) == 1 and isinstance(a.targets[0], ast.Name):
                s = _py_str(a.value, out)
                if s is not None:
                    prefix = f"{node.name}." if isinstance(node, ast.ClassDef) else ""
                    out[prefix + a.targets[0].id] = s
    return out


def _py_str(node: ast.AST, consts: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if isinstance(node, ast.Attribute):  # GitTools.STATUS.value
        base = node.value if node.attr == "value" else node
        if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
            return consts.get(f"{base.value.id}.{base.attr}")
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = _py_str(node.left, consts), _py_str(node.right, consts)
        return a + b if a is not None and b is not None else None
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append(str(v.value))
            else:
                s = _py_str(v.value, consts)
                parts.append(s if s is not None else "{" + ast.unparse(v.value) + "}")
        return "".join(parts)
    return None


def python_strings(src: str) -> list[dict]:
    tree = ast.parse(src)
    consts = _py_consts(tree)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(
            node.func, "id", "")
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        if "description" not in kw or fn not in ("Tool", "tool", "Field"):
            continue  # Tool(...), @server.tool(...), pydantic Field(...)
        text = _py_str(kw["description"], consts)
        if text is None:
            continue
        tool = fn != "Field"
        name = _py_str(kw["name"], consts) if tool and "name" in kw else None
        out.append({"kind": "tool" if tool else "param", "tool": name, "text": text})
    return out


# --- TypeScript ---------------------------------------------------------------------------

def _ts_literal(src: str, i: int) -> tuple[str, int] | None:
    """The string literal starting at src[i] (after whitespace), and the index after it."""
    while i < len(src) and src[i].isspace():
        i += 1
    if i >= len(src) or src[i] not in "\"'`":
        return None
    q, i, buf = src[i], i + 1, []
    while i < len(src) and src[i] != q:
        if src[i] == "\\":
            buf.append({"n": "\n", "t": "\t"}.get(src[i + 1], src[i + 1]))
            i += 2
        elif q == "`" and src.startswith("${", i):
            end = src.index("}", i)
            buf.append("{" + src[i + 2:end] + "}")
            i = end + 1
        else:
            buf.append(src[i])
            i += 1
    return "".join(buf), i + 1


def _ts_concat(src: str, i: int) -> str | None:
    """'a' + 'b' + `c` starting at i; None if it isn't a literal."""
    first = _ts_literal(src, i)
    if first is None:
        return None
    text, i = first
    while True:
        m = re.match(r"\s*\+", src[i:])
        nxt = _ts_literal(src, i + m.end()) if m else None
        if nxt is None:
            return text
        text, i = text + nxt[0], nxt[1]


def typescript_strings(src: str) -> list[dict]:
    names = sorted([(m.start(), lit[0]) for m in re.finditer(
        r"registerTool\(\s*|const name\s*=\s*", src)
        if (lit := _ts_literal(src, m.end()))], key=lambda x: x[0])
    out, described = [], set()
    for m in re.finditer(r"\bdescription\s*:|\.describe\(", src):
        text = _ts_concat(src, m.end())
        if text is None:
            continue
        before = [(pos, n) for pos, n in names if pos < m.start()]
        # The first `description:` after a tool's name is the tool's. Later ones belong to its
        # schemas, elicitation forms or content: strings a client sees, counted as "param".
        tool = not m.group(0).startswith(".") and before and before[-1][0] not in described
        if tool:
            described.add(before[-1][0])
        out.append({"kind": "tool" if tool else "param",
                    "tool": before[-1][1] if before else None, "text": text})
    return out


def main(checkout: str, sha: str) -> None:
    root = Path(checkout)
    rows = []
    for server in SERVERS:
        for f in sorted((root / "src" / server).rglob("*")):
            if f.suffix not in (".py", ".ts") or re.search(r"test|__tests__|spec", str(f)):
                continue
            src = f.read_text()
            found = python_strings(src) if f.suffix == ".py" else typescript_strings(src)
            for r in found:
                rows.append({"server": server, "file": str(f.relative_to(root)), **r,
                             "source": f"modelcontextprotocol/servers@{sha[:12]}"})
    seen, unique = set(), []
    for r in rows:  # the same string declared twice (e.g. fetch's two `url` params) counts once
        k = (r["server"], r["kind"], r["text"])
        if k not in seen:
            seen.add(k)
            unique.append(r)
    OUT.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in unique))
    by = {}
    for r in unique:
        by.setdefault(r["server"], [0, 0])[r["kind"] == "param"] += 1
    print(f"{len(unique)} strings -> {OUT}")
    for s, (t, p) in sorted(by.items()):
        print(f"  {s:<20} tools {t:>3}  params {p:>3}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
