"""Run one competitive v1.3 scenario with the three parts joined over HTTP.

A: the engine runs as its own process (`Game-engine/app.py`, temporary DB by default).
B: each pool member's player runtime (`--player package.module:factory`; default is
   C's scripted stand-in `ScriptedPlayerRuntime`, no LLM).
C: the GM is whichever pool member the engine makes GM (`CompetitiveGMWorker`).

    python -m gm_coordinator.run_scenario --seed 7 --progress
    python -m gm_coordinator.run_scenario --player agent_reasoning_v13:factory --out run.json
"""
import argparse
import hashlib
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .agent_runtime import AgentWorker, load_factory
from .engine_v1 import EngineV1Client

ROOT = Path(__file__).resolve().parent
DEFAULT_ENGINE = ROOT.parent / "Game-engine"


def git_head(path):
    try:
        out = subprocess.run(["git", "-C", str(path), "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, encoding="utf-8", timeout=10)
    except OSError:
        return "unavailable"
    return out.stdout.strip() if out.returncode == 0 else "unavailable"


@contextmanager
def engine_process(engine_dir, data):
    """Start A's engine server on a free port; yield (base_url, operator_token)."""
    env = os.environ | {"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
    secret = subprocess.run([sys.executable, "-B", "integration_cli.py", "--data", str(data), "credentials"],
                            cwd=engine_dir, env=env, capture_output=True, text=True, encoding="utf-8",
                            check=True, timeout=30)
    token = json.loads(secret.stdout)["operator_token"]
    proc = subprocess.Popen([sys.executable, "-B", "app.py", "--port", "0", "--no-browser", "--data", str(data)],
                            cwd=engine_dir, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, encoding="utf-8")
    lines = queue.Queue()
    threading.Thread(target=lambda: [lines.put(line) for line in proc.stdout], daemon=True).start()
    try:
        line = lines.get(timeout=30)
        url = next((x for x in line.split() if x.startswith("http://")), None)
        if url is None:
            raise RuntimeError(f"engine did not announce its address: {line!r}")
        yield url.rstrip("/"), token
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        proc.stdout.close()


def run(client, operator_token, seed=7, rules=None, player_factory=None, provenance=None,
        time_limit=3600, progress=False):
    """Bootstrap the pool, create one game and step every pool member until the GM has reported."""
    operator = client.bind(operator_token=operator_token)
    pool = operator.call("pool/bootstrap", pool_id="society")  # reuses the persistent pool if the DB has one
    # [FIX-C2] The engine (and through it every player) learns the wall-clock budget.
    rules = dict(rules or {}, time_limit_seconds=int(time_limit))
    created = operator.call("games/create", pool_id="society", seed=seed, rules=rules,
                            provenance=provenance or {})
    game_id = created["game_id"]
    workers = {a["agent_id"]: AgentWorker(client.bind(**operator.call("pool/credentials", agent_id=a["agent_id"])),
                                          player_factory)
               for a in pool["agents"]}
    started, last_cycle = time.monotonic(), None
    timeout_requested = None
    while True:
        for worker in workers.values():
            if timeout_requested is None and time.monotonic() - started >= time_limit and operator.call("games/observe", game_id=game_id)["status"] != "awaiting_setup":
                operator.call("games/operator", game_id=game_id, operation="TIMEOUT")
                timeout_requested = time.monotonic()
            if timeout_requested is None:
                worker.step()
            else:
                # No new player decisions after timeout; allow GM publication/report.
                worker.gm.step()
        state = operator.call("games/observe", game_id=game_id)
        if state["ended"] and state["gm_report"] is not None:
            break
        if progress and state["cycle_id"] != last_cycle and (state["cycle_id"] == 1 or state["cycle_id"] % 10 == 0):
            print(f"cycle {state['cycle_id']} · active {len(state['active_agents'])}", file=sys.stderr, flush=True)
        last_cycle = state["cycle_id"]
        if timeout_requested is not None and time.monotonic() - timeout_requested > 60:
            raise RuntimeError("official timeout recorded, but GM finalization failed within 60s")
    gm_worker = next(w.gm for w in workers.values() if w.gm.gm is not None and w.gm.gm.reported)
    record = gm_worker.gm.report()
    players = [p for w in workers.values() for p in w.runtimes]
    # [FIX-C3] Game-end hook. Players never observe the ended game themselves (after a
    # time_out only the GM is stepped), so the runner tells each runtime the game is over.
    # B uses it to read its official feedback and store what it learned for the next game.
    end_hooks = []
    for p in players:
        hook = getattr(p, "on_game_end", None)
        if callable(hook):
            try:
                end_hooks.append(hook(state) or "ok")
            except Exception as exc:  # learning must never break the official record
                end_hooks.append(f"failed: {type(exc).__name__}: {exc}"[:200])
                print(f"on_game_end failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    verdicts = [v for a in state["announcements"] if a["kind"] == "deduction_result" for v in a["verdicts"]]
    return {"game_id": game_id, "seed": seed, "resolution": state["resolution"], "cycles": state["cycle_id"],
            "players": len(state["role_assignments"]), "winners": [v for v in verdicts if v["winner"]],
            "deaths": [v["agent_id"] for v in verdicts if not v["winner"]],
            "gm": {"controller_id": gm_worker.gm.controller_id, "term": gm_worker.gm.term,
                   "failovers": state["failovers"]},
            "gm_report": state["gm_report"], "engine_metrics": state["metrics"],
            "scenario": [line["text"] for line in record["scenario"]],
            "player_runtime": sorted({type(p).__name__ for p in players}) or None,
            "player_fallbacks": sum(getattr(p, "fallbacks", 0) for p in players),
            # [FIX-C5] model-level stand-ins inside the player (B counts them, see FIX-B9)
            "model_calls": sum(getattr(p, "model_calls", 0) for p in players),
            "model_fallbacks": sum(getattr(p, "model_fallbacks", 0) for p in players),
            "live_llm": any(getattr(p, "live_llm", False) for p in players),
            "end_hooks": end_hooks,
            "elapsed_seconds": round(time.monotonic() - started, 1)}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--engine", default=str(DEFAULT_ENGINE), help="A's Game-engine checkout")
    parser.add_argument("--player", help="B player factory as package.module:callable (default: scripted stand-in)")
    parser.add_argument("--player-num", type=int, choices=range(3, 13), default=12, help="playable participants (3..12); GM excluded")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--deadline", type=int, help="override deadline_cycles (default 120)")
    parser.add_argument("--data", help="persistent SQLite path; default is a temporary database")
    parser.add_argument("--out", help="write the run summary JSON here")
    parser.add_argument("--time-limit", type=int, default=3600)
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args()
    from .role_policy import REQUESTED_PLAYERS
    REQUESTED_PLAYERS.set(args.player_num)
    engine_dir = Path(args.engine).resolve()
    if not (engine_dir / "competitive.py").is_file():
        parser.error(f"A engine not found at {engine_dir}")
    rules = {"deadline_cycles": args.deadline} if args.deadline else {}
    factory = load_factory(args.player)
    sources = [engine_dir / p for p in ("integration.py", "competitive.py", "competitive_rules.py", "competitive_catalog.json")]
    provenance = {"engine_commit": git_head(engine_dir), "gm_commit": git_head(ROOT),
                  "engine_tree_hash": hashlib.sha256(b"".join(p.read_bytes() for p in sources)).hexdigest()[:16],
                  "player_num": str(args.player_num),
                  "player": args.player or "gm_coordinator.agent_runtime:ScriptedPlayerRuntime",
                  "command": " ".join(["run_scenario"] + sys.argv[1:])[:500]}
    with tempfile.TemporaryDirectory() as temporary:
        # [FIX-C4] Resolve now: the engine runs with cwd=Game-engine, so a relative path
        # used to create the DB under Game-engine/ instead of where the user asked.
        data = Path(args.data).resolve() if args.data else Path(temporary) / "scenario.sqlite3"
        data.parent.mkdir(parents=True, exist_ok=True)
        with engine_process(engine_dir, data) as (url, token):
            result = run(EngineV1Client(url, timeout=30), token, args.seed, rules, factory, provenance,
                         args.time_limit, args.progress)
    result["provenance"] = provenance
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
