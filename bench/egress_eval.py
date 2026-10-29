"""C3's secret detector (ADR 0012): recall on fake secrets, false positives on real text, speed.

    python bench/egress_eval.py

Positives are generated here, never stored: every known format, each placed in several kinds of
surrounding text (prose, JSON, env file, shell, log line). An "evasion" set (base64-encoded,
split, spaced, reversed) shows what the detector is not meant to catch.

Negatives are real text from this repository: every .py/.md/.ts/.tsx/.yaml file, the git log's
commit SHAs, random UUIDs and sha256 digests, and the walkthrough screenshots as base64 (the
shape image tool results take). A finding in them is a false positive. One file is left out:
tests/test_egress_guard.py, which holds secret-shaped fixture text on purpose (a PEM header, a
URL with a password). Writes bench/results/egress-eval.{md,json}.
"""
from __future__ import annotations

import base64
import hashlib
import json
import random
import string
import subprocess
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path

from sluice.guard.secrets import scan

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).parent / "results"
R = random.Random(2026)
A = string.ascii_letters + string.digits


def rnd(n, alphabet=A):
    return "".join(R.choice(alphabet) for _ in range(n))


FORMATS = {
    "openai_key": lambda: "sk-proj-" + rnd(R.randint(40, 120), A + "-_"),
    "anthropic_key": lambda: "sk-ant-api03-" + rnd(93, A + "-_"),
    "sluice_key": lambda: "sk-sluice-" + rnd(43, A + "-_"),
    "stripe_key": lambda: "sk_live_" + rnd(24),
    "aws_access_key": lambda: R.choice(["AKIA", "ASIA"]) + rnd(16, string.ascii_uppercase + string.digits),
    "github_token": lambda: R.choice(["ghp_", "gho_", "ghs_"]) + rnd(36),
    "gitlab_token": lambda: "glpat-" + rnd(20, A + "-_"),
    "slack_token": lambda: "xoxb-" + rnd(12, string.digits) + "-" + rnd(13, string.digits) + "-" + rnd(24),
    "npm_token": lambda: "npm_" + rnd(36),
    "google_api_key": lambda: "AIza" + rnd(35, A + "-_"),
    "pem_private_key": lambda: f"-----BEGIN {R.choice(['RSA ', 'EC ', 'OPENSSH ', ''])}PRIVATE KEY-----\n" + rnd(64),
    "jwt": lambda: "eyJ" + rnd(30, A + "-_") + ".eyJ" + rnd(60, A + "-_") + "." + rnd(43, A + "-_"),
    "url_password": lambda: f"{R.choice(['postgres', 'mysql', 'redis', 'amqp'])}://svc:{rnd(16)}@db.internal:5432/x",
    "high_entropy": lambda: rnd(R.choice([32, 40, 64])),
}
HINTED = {  # a generic random secret only counts with a hint nearby, so give it one
    "high_entropy": ["api_secret = {s}", "Authorization: Bearer {s}", "the token is {s}",
                     '"client_secret": "{s}"', "PASSWORD={s}"],
}
CONTEXTS = ["Here is the key you asked for: {s}. Thanks!", '{{"config": {{"value": "{s}"}}}}',
            "export VALUE={s}", "curl -H 'X-Api: {s}' https://api.example.com", "2026-10-29 INFO loaded {s}",
            "{s}"]
EVASIONS = {
    "base64_encoded": lambda s: base64.b64encode(s.encode()).decode(),
    "split_in_two": lambda s: s[: len(s) // 2] + "\n...\n" + s[len(s) // 2:],
    "spaced": lambda s: " ".join(s),
    "reversed": lambda s: s[::-1],
}


def positives(n_per_format=40):
    out = []
    for kind, make in FORMATS.items():
        for _ in range(n_per_format):
            s = make()
            ctx = R.choice(HINTED.get(kind, CONTEXTS))
            out.append((kind, ctx.format(s=s)))
    return out


def negatives() -> list[tuple[str, str]]:
    docs = []
    for pat in ("src/**/*.py", "tests/**/*.py", "bench/*.py", "demo/*.py", "**/*.md", "UI/src/**/*.ts",
                "UI/src/**/*.tsx", "policies/*.yaml", "demo/*.yaml"):
        for f in sorted(ROOT.glob(pat)):
            if "node_modules" not in f.parts and f.is_file() and f.name != "test_egress_guard.py":
                docs.append((str(f.relative_to(ROOT)), f.read_text(errors="replace")))
    shas = subprocess.run(["git", "log", "--format=%H %s"], cwd=ROOT, capture_output=True, check=True,
                          text=True).stdout
    docs.append(("git log (SHAs + subjects)", shas))
    docs.append(("uuids", "\n".join(f"request {uuid.UUID(int=R.getrandbits(128))}" for _ in range(2000))))
    docs.append(("sha256 digests", "\n".join(
        f"args_sha256={hashlib.sha256(rnd(20).encode()).hexdigest()}" for _ in range(2000))))
    for png in sorted((ROOT / "docs" / "walkthrough").glob("*.png"))[:6]:
        b64 = base64.b64encode(png.read_bytes()).decode()
        docs.append((f"{png.name} as base64", '{"type": "image", "data": "' + b64 + '"}'))
    return docs


def main() -> None:
    pos = positives()
    got = defaultdict(lambda: [0, 0])
    wrong_type = Counter()
    for kind, text in pos:
        found = scan(text)
        got[kind][1] += 1
        if any(f.type == kind for f in found):
            got[kind][0] += 1
        elif found:
            wrong_type[(kind, found[0].type)] += 1
    ev = defaultdict(lambda: [0, 0])
    for name, tf in EVASIONS.items():
        for kind in ("openai_key", "aws_access_key", "github_token"):
            for _ in range(20):
                ev[name][1] += 1
                ev[name][0] += bool(scan("value: " + tf(FORMATS[kind]())))

    neg = negatives()
    fps, total_kb, secs = [], 0.0, 0.0
    for name, text in neg:
        t0 = time.perf_counter()
        found = scan(text)
        secs += time.perf_counter() - t0
        total_kb += len(text.encode()) / 1024
        for f in found:
            line = text.count("\n", 0, f.start) + 1
            fps.append({"source": name, "line": line, "type": f.type, "len": f.end - f.start})
    n_pos = sum(b for _, b in got.values())
    n_hit = sum(a for a, _ in got.values())

    summary = {"positives": n_pos, "found": n_hit,
               "recall_by_type": {k: f"{a}/{b}" for k, (a, b) in got.items()},
               "wrong_type": {f"{a}->{b}": n for (a, b), n in wrong_type.items()},
               "evasions_found": {k: f"{a}/{b}" for k, (a, b) in ev.items()},
               "negative_kb": round(total_kb), "negative_docs": len(neg),
               "false_positives": len(fps), "us_per_kb": round(secs / total_kb * 1e6, 1)}
    (OUT / "egress-eval.json").write_text(json.dumps({"summary": summary, "false_positives": fps},
                                                     indent=1))
    md = [
        "# Secret egress guard: recall, false positives, speed",
        "",
        (f"Generated by `bench/egress_eval.py`. Positives: {n_pos} fake secrets made at run time "
        f"({len(FORMATS)} formats, each in several surrounding texts); none is stored. Negatives: "
        f"{round(total_kb):,} KB of real text from this repository ({len(neg)} documents: source, "
        "docs, UI code, policies, the git log), plus random UUIDs and sha256 digests and six "
        "screenshots as base64."),
        "",
        ("> **Caveats.** The positives come from the same format rules the detector uses, so "
         "known-format recall measures that the rules are wired up, not that they cover every "
         "real key format. The evasion rows are the honest part: an encoded, split or spaced "
         "secret gets through. The negatives are one repository's text; logs and data from "
         "other systems will differ."),
        "",
        "## Result",
        "",
        ("The first run found two detector bugs, both fixed before this one: `=` was allowed "
         "anywhere in a random run, so `PASSWORD=<secret>` swallowed its own hint (high_entropy "
         "28/40), and the hint was looked for across line breaks, so three git SHAs counted as "
         "secrets because the commit subject above them said \"Keys\" (3 false positives). "
         "`tests/test_egress_guard.py` has a regression test for each. The detector has no "
         "tuned threshold; nothing else changed."),
        "",
        f"- **Found {n_hit} of {n_pos} fake secrets ({100 * n_hit / n_pos:.0f}%).**",
        f"- **False positives: {len(fps)} in {round(total_kb):,} KB of real text.**",
        f"- Speed: **{summary['us_per_kb']} µs per KB** (one core, `time.perf_counter`).",
        "",
        "| format | found |",
        "|---|---|",
        *[f"| {k} | {a}/{b} |" for k, (a, b) in got.items()],
        "",
        "| evasion (openai, aws, github keys) | found |",
        "|---|---|",
        *[f"| {k} | {a}/{b} |" for k, (a, b) in ev.items()],
        "",
        "## Every false positive",
        "",
    ]
    if fps:
        md += ["| source | line | type | length |", "|---|---|---|---|",
               *[f"| {f['source']} | {f['line']} | {f['type']} | {f['len']} |" for f in fps]]
    else:
        md.append("None.")
    if wrong_type:
        md += ["", "Found but typed differently: " + ", ".join(
            f"{a} as {b} ({n})" for (a, b), n in wrong_type.items()) + "."]
    (OUT / "egress-eval.md").write_text("\n".join(md) + "\n")
    print("\n".join(md[md.index("## Result"):]))


if __name__ == "__main__":
    main()
