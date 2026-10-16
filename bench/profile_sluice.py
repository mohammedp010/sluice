"""Profile Sluice under load: where does a request's CPU time go?

    python bench/profile_sluice.py --tag before     # results/profile-<tag>{.txt,-flat.txt,.html}
The .html is an interactive call tree (~40 MB, gitignored); open it in a browser.

Sluice runs in this process under pyinstrument (a sampling profiler: it doesn't slow every
call down the way cProfile does). The load generator runs as a separate process so its CPU
doesn't show up here. Same policy as bench/compare.py: auth, limits, ledger on; tracing export
off.

The plan names py-spy; on macOS it needs root, so this uses pyinstrument instead. With sudo:
    sudo py-spy record -o flame.svg -- python -m uvicorn sluice.api.app:app_factory --factory
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import uvicorn
from pyinstrument import Profiler

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
from compare import PORTS, SLUICE_POLICY, port_free, start, stop, wait_ready

OUT = ROOT / "results"


async def serve_and_profile(app, key: str, rps: int, duration: int, prof: Profiler) -> None:
    server = uvicorn.Server(uvicorn.Config(app, port=PORTS["sluice"], log_level="warning",
                                           access_log=False))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    load = await asyncio.create_subprocess_exec(
        sys.executable, str(ROOT / "loadgen.py"),
        f"http://127.0.0.1:{PORTS['sluice']}/v1/chat/completions",
        "--rps", str(rps), "--duration", str(duration), "--warmup", "2", "--key", key,
        stdout=subprocess.PIPE)
    prof.start()
    out, _ = await load.communicate()
    prof.stop()
    print(out.decode())
    server.should_exit = True
    await task


def main(tag: str, rps: int, duration: int):
    if not port_free(PORTS["healthy"]) or not port_free(PORTS["sluice"]):
        sys.exit("benchmark ports busy")
    tmp = Path(tempfile.mkdtemp(prefix="sluice-prof-"))
    mock = start([sys.executable, str(ROOT / "mock_upstream.py"), "--port",
                  str(PORTS["healthy"])], tmp / "mock.log")
    try:
        wait_ready(f"http://127.0.0.1:{PORTS['healthy']}/v1/models")
        (tmp / "sluice.yaml").write_text(SLUICE_POLICY.format(db=tmp / "sluice.db", **PORTS))
        os.environ["SLUICE_POLICY"] = str(tmp / "sluice.yaml")
        sys.path.insert(0, str(ROOT.parent / "src"))
        from sluice.api.app import create_app
        from sluice.auth.keys import KeyStore

        key, _ = KeyStore(str(tmp / "sluice.db")).create("bench", rpm=10**9, tpm=10**12)
        prof = Profiler(interval=0.0005, async_mode="disabled")  # sample the whole loop thread
        import uvloop

        uvloop.run(serve_and_profile(create_app(), key, rps, duration, prof))
    finally:
        stop(mock)
    OUT.mkdir(exist_ok=True)
    (OUT / f"profile-{tag}.txt").write_text(prof.output_text(unicode=True, color=False,
                                                             show_all=False))
    (OUT / f"profile-{tag}.html").write_text(prof.output_html())
    # flat: self time per function, i.e. where the CPU actually goes
    (OUT / f"profile-{tag}-flat.txt").write_text(prof.output_text(unicode=True, color=False,
                                                                  flat=True))
    print(prof.output_text(unicode=True, color=False, show_all=False)[:6000])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="run")
    ap.add_argument("--rps", type=int, default=200)
    ap.add_argument("--duration", type=int, default=20)
    a = ap.parse_args()
    main(a.tag, a.rps, a.duration)
