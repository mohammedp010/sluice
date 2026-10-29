"""Secret egress guard (ADR 0012): secrets in tool arguments, tool results and chat prompts are
found, logged as type and position only, and optionally blocked or redacted.

Fake secrets are built at runtime, so no secret-shaped string sits in the repo."""
import json
import random
import string

import pytest
from conftest import ROUTE, client_for, make_app
from mcp.shared.exceptions import McpError
from test_mcp import SERVER, sdk_session

from sluice.config import GuardConfig, Settings
from sluice.guard.secrets import redact, redact_json, scan, scan_json

R = random.Random(7)


def rnd(n, alphabet=string.ascii_letters + string.digits):
    return "".join(R.choice(alphabet) for _ in range(n))


SECRETS = {
    "openai_key": lambda: "sk-proj-" + rnd(40),
    "anthropic_key": lambda: "sk-ant-api03-" + rnd(60),
    "sluice_key": lambda: "sk-sluice-" + rnd(43),
    "aws_access_key": lambda: "AKIA" + rnd(16, string.ascii_uppercase + string.digits),
    "github_token": lambda: "ghp_" + rnd(36),
    "slack_token": lambda: "xoxb-" + rnd(12, string.digits) + "-" + rnd(24),
    "npm_token": lambda: "npm_" + rnd(36),
    "pem_private_key": lambda: "-----BEGIN " + "OPENSSH PRIVATE KEY-----\n" + rnd(64),
    "jwt": lambda: "eyJ" + rnd(20) + ".eyJ" + rnd(30) + "." + rnd(43),
    "url_password": lambda: f"postgres://app:{rnd(12)}@db.internal:5432/orders",
    "high_entropy": lambda: "the signing secret is " + rnd(40),
}
BENIGN = [
    "Refund order KO-48213 for $42.10, reason: damaged in transit.",
    "id 123e4567-e89b-12d3-a456-426614174000",  # UUID
    "fixed in 1a89cf5e2b7d4c3a9f8e6d5c4b3a2918f7e6d5c4",  # git SHA
    "def get_current_time_in_timezone(tz: str) -> str: return tz",
    "The token budget is 20000 tokens per minute for this key.",
    "sha256=" + "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "Use the internationalization_and_localization_helpers module.",
]


@pytest.mark.parametrize("kind", SECRETS)
def test_each_format_is_found_with_its_position(kind):
    secret = SECRETS[kind]()
    text = f"please use {secret} for this"
    found = scan(text)
    assert [f.type for f in found] == [kind]
    f = found[0]
    assert secret.endswith(text[f.start:f.end]) or text[f.start:f.end] in secret


def test_benign_text_is_quiet():
    for text in BENIGN:
        assert scan(text) == [], text


def test_env_style_assignment_and_same_line_hint():
    # found by bench/egress_eval.py's first run: "=" joined the name to the value, hiding the
    # hint; and a hint on the previous line made a git SHA look like a secret
    secret = rnd(40)
    assert [f.type for f in scan(f"PASSWORD={secret}")] == ["high_entropy"]
    assert scan("Keys expire, bind to networks\n1a89cf5e2b7d4c3a9f8e6d5c4b3a2918f7e6d5c4") == []


def test_findings_never_carry_the_value_and_redact_replaces_it():
    secret = SECRETS["openai_key"]()
    found = scan(f"key={secret}")
    assert secret not in json.dumps([f.brief() for f in found])
    assert redact(f"key={secret}!", found) == "key=[REDACTED:openai_key]!"


def test_json_paths_cover_nesting_and_keys():
    s = SECRETS["github_token"]()
    found = scan_json({"to": ["a@b.c"], "body": {"lines": ["hi", f"tok {s}"]}, s: 1})
    assert sorted(f.path for f in found) == ["body.lines[1]", f"{s}{{key}}"]
    assert s not in json.dumps(redact_json({"body": [f"x {s}"]}))


def test_first_matching_glob_wins():
    rules = {"mail__*": "block", "*": "audit"}
    assert GuardConfig.mode(rules, "mail__send") == "block"
    assert GuardConfig.mode(rules, "demo__echo") == "audit"
    assert GuardConfig.mode({"files__*": "block"}, "demo__echo") == "off"


def mcp_guarded(**guard):
    # the watch is off: R5 would suspend the key on a block (tests/test_watch.py covers that)
    settings = Settings(providers={"mock": {"type": "mock"}}, routes={"m": ROUTE},
                        mcp={"servers": {"demo": {"command": SERVER}}}, guard=guard,
                        watch={"enabled": False})
    return make_app(settings, key_kw={"allowed_tools": ["demo__*"]})


async def test_audit_mode_lets_the_call_through_and_records_type_and_position():
    app, raw = mcp_guarded()
    secret = SECRETS["aws_access_key"]()
    async with sdk_session(app, raw) as s:
        r = await s.call_tool("demo__echo", {"text": f"creds: {secret}"})
    assert r.content[0].text == f"creds: {secret}"  # audit only: nothing changed
    row = app.state.mcp.audit.rows()[0]
    assert row["outcome"] == "ok" and secret not in json.dumps(row)
    egress = json.loads(row["egress"])
    assert {(e["where"], e["type"], e["path"]) for e in egress} == {
        ("tool_args", "aws_access_key", "text"),
        ("tool_results", "aws_access_key", "content[0].text"),
        ("tool_results", "aws_access_key", "structuredContent.result")}


async def test_block_mode_refuses_before_the_server_sees_it():
    app, raw = mcp_guarded(tool_args={"demo__echo": "block", "*": "audit"})
    secret = SECRETS["openai_key"]()
    up, forwarded = app.state.mcp.up, []
    real = up.call

    async def spy(name, args):
        forwarded.append(name)
        return await real(name, args)

    up.call = spy
    async with sdk_session(app, raw) as s:
        with pytest.raises(McpError) as e:
            await s.call_tool("demo__echo", {"text": secret})
        assert (await s.call_tool("demo__add", {"a": 1, "b": 2})).content[0].text == "3"
    assert "openai_key" in e.value.error.message and secret not in e.value.error.message
    rows = app.state.mcp.audit.rows()
    assert [r["outcome"] for r in rows] == ["ok", "egress_blocked"]
    assert forwarded == ["demo__add"]  # the blocked call never reached the server


async def test_redact_mode_scrubs_results_before_they_reach_the_model():
    app, raw = mcp_guarded(tool_results={"*": "redact"})
    secret = SECRETS["github_token"]()
    async with sdk_session(app, raw) as s:
        r = await s.call_tool("demo__echo", {"text": f"found {secret} in .env"})
    assert r.content[0].text == "found [REDACTED:github_token] in .env"
    assert secret not in json.dumps(r.structuredContent)


def chat_app(mode):
    settings = Settings(providers={"mock": {"type": "mock"}}, routes={"m": ROUTE},
                        guard={"chat": mode})
    return make_app(settings)


async def test_chat_prompt_audit_and_block():
    secret = SECRETS["anthropic_key"]()
    body = {"model": "m", "messages": [{"role": "user", "content": f"debug: {secret}"}],
            "max_tokens": 5}
    app, raw = chat_app("audit")
    async with client_for(app, raw) as c:
        assert (await c.post("/v1/chat/completions", json=body)).status_code == 200
    ev = [e for s in app.state.spans.get_finished_spans() for e in s.events
          if e.name == "sluice.guard.findings"]
    assert ev and secret not in json.dumps([dict(e.attributes) for e in ev], default=str)

    app, raw = chat_app("block")
    async with client_for(app, raw) as c:
        r = await c.post("/v1/chat/completions", json=body)
    assert r.status_code == 400 and r.json()["error"]["code"] == "secret_in_prompt"
    assert secret not in r.text
    assert not [s for s in app.state.spans.get_finished_spans() if s.name.startswith("chat mock")]
