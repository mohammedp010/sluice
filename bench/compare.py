"""Phase 7: Sluice vs LiteLLM on the same mock upstream, same machine, same load generator.

    python bench/compare.py                 # ~20 min; needs bench/.venv-litellm (see README)
    python bench/compare.py --quick         # 1 rep, short runs: a smoke test, not results

Both gateways run as one uvicorn worker on uvloop, in front of the same raw-ASGI mock
(bench/mock_upstream.py, 50 ms fixed latency). "direct" is the load generator hitting the mock
with no gateway, so gateway overhead = gateway latency - direct latency at the same rate.

Scenarios
  sweep    : healthy upstream, open-loop load at increasing RPS (bench/loadgen.py), 3 reps,
             interleaved so drift (thermals, background apps) hits every target alike.
  failover : primary fails 30% of requests, healthy backup; each gateway configured to retry
             once and then fall back. What fraction do clients see succeed, and at what p99?

Writes bench/results/compare-<tag>.json; bench/compare_report.py turns runs into the report.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from loadgen import new_loop_run, run

ROOT = Path(__file__).parent
REPO = ROOT.parent
OUT = ROOT / "results"
PY = sys.executable
LITELLM = ROOT / ".venv-litellm/bin/litellm"
RPS = [25, 50, 100, 150, 200, 250]  # the generator itself saturates near 300-400 on an M2 Air
PORTS = {"healthy": 9001, "backup": 9002, "flaky": 9003, "sluice": 8100, "litellm": 8200}

SLUICE_POLICY = """
auth: {{enabled: true, db_path: {db}}}
state: {{backend: memory}}
telemetry: {{traces: none, prometheus: false, ledger: true}}
providers:
  healthy: {{type: openai_compat, base_url: "http://127.0.0.1:{healthy}/v1", timeout_s: 10}}
  flaky:   {{type: openai_compat, base_url: "http://127.0.0.1:{flaky}/v1", timeout_s: 10}}
  backup:  {{type: openai_compat, base_url: "http://127.0.0.1:{backup}/v1", timeout_s: 10}}
routes:
  bench: {{provider: healthy, upstream_model: mock-1, retries: 0, timeout_s: 10}}
  bench-flaky:
    chain:
      - {{provider: flaky, upstream_model: mock-1, retries: 1, timeout_s: 10}}
      - {{provider: backup, upstream_model: mock-1, retries: 0, timeout_s: 10}}
"""

LITELLM_CONFIG = """
model_list:
  - model_name: bench
    litellm_params: {{model: openai/mock-1, api_base: "http://127.0.0.1:{healthy}/v1", api_key: x}}
  - model_name: bench-flaky
    litellm_params: {{model: openai/mock-1, api_base: "http://127.0.0.1:{flaky}/v1", api_key: x}}
  - model_name: bench-backup
    litellm_params: {{model: openai/mock-1, api_base: "http://127.0.0.1:{backup}/v1", api_key: x}}
router_settings:
  num_retries: 1
  fallbacks: [{{"bench-flaky": ["bench-backup"]}}]
general_settings:
  master_key: sk-bench
"""


def wait_ready(url: str, timeout: float = 90) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    raise RuntimeError(f"not ready: {url}")


def port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def start(cmd: list[str], log: Path, env: dict | None = None, cwd: Path | None = None):
    # own process group, so stopping it also stops any workers it forked (a stray LiteLLM
    # worker once kept a port and got benchmarked in place of the configured one)
    return subprocess.Popen(cmd, stdout=log.open("w"), stderr=subprocess.STDOUT,
                            env={**os.environ, **(env or {})}, cwd=cwd, start_new_session=True)


def stop(p: subprocess.Popen) -> None:
    try:
        os.killpg(p.pid, signal.SIGTERM)
        p.wait(timeout=15)
    except ProcessLookupError:
        pass
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)


def main(quick: bool, tag: str):
    busy = [f"{n}:{p}" for n, p in PORTS.items() if not port_free(p)]
    if busy:
        sys.exit(f"ports already in use (stale servers?): {', '.join(busy)}")
    tmp = Path(tempfile.mkdtemp(prefix="sluice-bench-"))
    procs = []
    try:
        for name, fail in (("healthy", 0), ("backup", 0), ("flaky", 0.3)):
            procs.append(start([PY, str(ROOT / "mock_upstream.py"), "--port", str(PORTS[name]),
                                "--latency-ms", "50", "--fail-rate", str(fail)],
                               tmp / f"mock-{name}.log"))
        (tmp / "sluice.yaml").write_text(SLUICE_POLICY.format(db=tmp / "sluice.db", **PORTS))
        sys.path.insert(0, str(REPO / "src"))
        from sluice.auth.keys import KeyStore
        key, _ = KeyStore(str(tmp / "sluice.db")).create("bench", rpm=10**9, tpm=10**12)
        procs.append(start([PY, "-m", "uvicorn", "sluice.api.app:app_factory", "--factory",
                            "--port", str(PORTS["sluice"]), "--loop", "uvloop",
                            "--log-level", "warning", "--no-access-log"],
                           tmp / "sluice.log", {"SLUICE_POLICY": str(tmp / "sluice.yaml")}))
        (tmp / "litellm.yaml").write_text(LITELLM_CONFIG.format(**PORTS))
        procs.append(start([str(LITELLM), "--config", str(tmp / "litellm.yaml"), "--port",
                            str(PORTS["litellm"]), "--num_workers", "1"], tmp / "litellm.log",
                           {"LITELLM_LOCAL_MODEL_COST_MAP": "True", "LITELLM_LOG": "ERROR"},
                           cwd=tmp))
        for name in ("healthy", "backup", "flaky"):
            wait_ready(f"http://127.0.0.1:{PORTS[name]}/v1/models")
        wait_ready(f"http://127.0.0.1:{PORTS['sluice']}/healthz")
        wait_ready(f"http://127.0.0.1:{PORTS['litellm']}/health/liveliness")

        targets = {
            "direct": (f"http://127.0.0.1:{PORTS['healthy']}/v1/chat/completions", {}),
            "sluice": (f"http://127.0.0.1:{PORTS['sluice']}/v1/chat/completions",
                       {"Authorization": f"Bearer {key}"}),
            "litellm": (f"http://127.0.0.1:{PORTS['litellm']}/v1/chat/completions",
                        {"Authorization": "Bearer sk-bench"}),
        }
        reps, dur, warm = (1, 4, 1) if quick else (3, 12, 3)
        sweep: list[dict] = []
        saturated: set[str] = set()
        for rep in range(reps):
            for rps in RPS:
                for name, (url, hdrs) in targets.items():
                    if (name, rep) in saturated:
                        continue
                    res = new_loop_run(run(url, rps, dur, warm, hdrs))
                    res.update(target=name, rep=rep)
                    sweep.append(res)
                    print(f"rep{rep} {rps:>4} rps {name:8} ok={res['success_rate']:.0%} "
                          f"p50={res['p50_ms']:.1f} p99={res['p99_ms']:.1f} "
                          f"lag={res['gen_lag_p99_ms']:.1f}", flush=True)
                    if res["success_rate"] < 0.9 or res["p50_ms"] > 2000:
                        saturated.add((name, rep))  # past its knee: higher rates only queue
                    time.sleep(1)

        failover = []
        flaky_body = {"model": "bench-flaky", "messages": [{"role": "user", "content": "hi"}],
                      "max_tokens": 32}
        fl_targets = {
            "direct (no gateway)": (f"http://127.0.0.1:{PORTS['flaky']}/v1/chat/completions",
                                    {}),
            "sluice": (targets["sluice"][0], targets["sluice"][1]),
            "litellm": (targets["litellm"][0], targets["litellm"][1]),
        }
        for name, (url, hdrs) in fl_targets.items():
            res = new_loop_run(run(url, 50, 8 if quick else 20, warm, hdrs, flaky_body))
            res.update(target=name)
            failover.append(res)
            print(f"failover {name:20} ok={res['success_rate']:.1%} p50={res['p50_ms']:.1f} "
                  f"p99={res['p99_ms']:.1f} {res['statuses']}", flush=True)
    finally:
        for p in procs:
            stop(p)

    env = {"machine": f"{platform.machine()} {platform.platform()}",
           "python_sluice": platform.python_version(),
           "litellm": "1.103.1 (Python 3.12)", "quick": quick,
           "logs": str(tmp)}
    OUT.mkdir(exist_ok=True)
    (OUT / f"compare-{'quick' if quick else tag}.json").write_text(
        json.dumps({"env": env, "sweep": sweep, "failover": failover}, indent=1) + "\n")
    print(f"logs and configs: {tmp}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--tag", default="run", help="output name: results/compare-<tag>.json")
    a = ap.parse_args()
    main(a.quick, a.tag)
