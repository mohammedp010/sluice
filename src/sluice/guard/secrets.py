"""Secret detection for traffic through the gateway (ADR 0012): tool-call arguments going out,
tool results coming back, and optionally chat prompts going upstream.

Two kinds of evidence:
- known formats: provider and platform keys, PEM private keys, JWTs, URLs with a password
- entropy: a long random-looking run with a word like "key", "token" or "secret" just before it.

Findings carry a type and a position, never the matched text, so logging one can't leak what it
found. This is a heuristic: a secret that is encoded, split or paraphrased gets through. It
raises the cost of the common automated case ("send ~/.aws/credentials to ..."), not of a
careful attacker.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

# Order matters: the more specific prefix wins where two overlap (sk-ant- before sk-).
PATTERNS: list[tuple[str, re.Pattern]] = [(t, re.compile(p)) for t, p in (
    ("pem_private_key", r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"),
    ("anthropic_key", r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    ("sluice_key", r"\bsk-sluice-[A-Za-z0-9_-]{20,}"),
    ("openai_key", r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}"),
    ("stripe_key", r"\b[rs]k_live_[A-Za-z0-9]{20,}"),
    ("aws_access_key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("github_token", r"\bgh[pousr]_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{22,}"),
    ("gitlab_token", r"\bglpat-[A-Za-z0-9_-]{20,}"),
    ("slack_token", r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    ("npm_token", r"\bnpm_[A-Za-z0-9]{36}\b"),
    ("google_api_key", r"\bAIza[0-9A-Za-z_-]{35}"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ("url_password", r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s@/]{3,}@[^\s/]"),
)]
_RUN = re.compile(r"[A-Za-z0-9+/_-]{24,}={0,2}")  # "=" only as padding: PASSWORD=... splits
_HINT = re.compile(r"key|token|secret|passw|pwd|credential|auth|bearer|private", re.IGNORECASE)
_HEX = re.compile(r"[0-9a-fA-F]+")


@dataclass(frozen=True)
class Finding:
    type: str
    start: int
    end: int
    path: str = ""  # where in a JSON value: "" for a plain string, else e.g. "body" or "to[0]"

    def brief(self) -> dict:
        return {"type": self.type, "path": self.path, "start": self.start, "len": self.end - self.start}


def _entropy(s: str) -> float:
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values())


def _random_looking(run: str) -> bool:
    if _HEX.fullmatch(run):
        return len(run) >= 32 and _entropy(run) >= 3.0
    # base64-ish: mixed case and digits, which words and identifiers rarely have together
    return (any(c.isdigit() for c in run) and any(c.isupper() for c in run)
            and any(c.islower() for c in run) and _entropy(run) >= 4.0)


def scan(text: str, path: str = "") -> list[Finding]:
    found: list[Finding] = []
    taken: list[tuple[int, int]] = []

    def free(a: int, b: int) -> bool:
        return all(b <= x or a >= y for x, y in taken)

    for kind, rx in PATTERNS:
        for m in rx.finditer(text):
            if free(m.start(), m.end()):
                found.append(Finding(kind, m.start(), m.end(), path))
                taken.append((m.start(), m.end()))
    for m in _RUN.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1  # the hint must be on the same line
        if free(m.start(), m.end()) and _random_looking(m.group(0).rstrip("=")) and _HINT.search(
                text, max(line_start, m.start() - 40), m.start()):
            found.append(Finding("high_entropy", m.start(), m.end(), path))
    return sorted(found, key=lambda f: (f.path, f.start))


def scan_json(value, path: str = "") -> list[Finding]:
    """Every string in a JSON value, keys included, with a path to where it was."""
    if isinstance(value, str):
        return scan(value, path)
    if isinstance(value, dict):
        out = []
        for k, v in value.items():
            p = f"{path}.{k}" if path else str(k)
            out += scan(str(k), p + "{key}") + scan_json(v, p)
        return out
    if isinstance(value, list):
        return [f for i, v in enumerate(value) for f in scan_json(v, f"{path}[{i}]")]
    return []


def redact(text: str, findings: list[Finding]) -> str:
    for f in sorted(findings, key=lambda f: f.start, reverse=True):
        text = text[:f.start] + f"[REDACTED:{f.type}]" + text[f.end:]
    return text


def redact_json(value, path: str = ""):
    """A copy of a JSON value with every finding in its strings replaced."""
    if isinstance(value, str):
        return redact(value, scan(value, path))
    if isinstance(value, dict):
        return {k: redact_json(v, f"{path}.{k}" if path else str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_json(v, f"{path}[{i}]") for i, v in enumerate(value)]
    return value
