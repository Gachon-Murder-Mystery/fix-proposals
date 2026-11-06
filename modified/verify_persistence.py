"""[FIX-I4] Check a batch for the three properties experiment-30 lacked.

  P1 same agents   every game draws its players from ONE persistent pool (same ids, MBTI fixed)
  P2 distinct      within a game each worker is a different agent: own id, character, MBTI
  P3 experience    an agent's first prompt in game k reports exactly the games it played
                   before k, with lessons; a newcomer's says it is its first game

    python verify_persistence.py runs/<batch-dir>

Needs prompts on disk: --fake-llm writes runs/<game>/fake-calls.jsonl, --codex writes
codex-calls.jsonl. For --llm runs P3 is read from the engine's llm_records instead.
Exit code 0 only if every check passes.
"""
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def prompts(game_id, db):
    """(user JSON dict) for every decision prompt of one game, in call order."""
    out = []
    for name in ("fake-calls.jsonl", "codex-calls.jsonl"):
        path = ROOT / "runs" / game_id / name
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    out.append(json.loads(json.loads(line)["user"]))
                except (ValueError, KeyError, TypeError):
                    pass
    if not out:  # live API runs: the engine archived every request
        for (state,) in db.execute("SELECT state FROM llm_records WHERE game_id=? AND status='started' ORDER BY sequence", (game_id,)):
            try:
                out.append(json.loads(json.loads(state)["request"]["user"]))
            except (ValueError, KeyError, TypeError):
                pass
    return [p for p in out if isinstance(p, dict) and "me" in p]


def main(batch_dir):
    batch = json.loads((Path(batch_dir) / "batch.json").read_text(encoding="utf-8"))
    done = [a for a in batch["attempts"] if a["status"] == "completed"]
    if not batch.get("pool_db"):
        print("FAIL batch ran with --isolated: every seed had its own pool")
        return 1
    db = sqlite3.connect(batch["pool_db"])
    agents = {aid: json.loads(s) for aid, s in db.execute("SELECT agent_id, state FROM agents")}
    players = {a: v for a, v in agents.items() if v.get("pool_role") == "player"}
    failures = []

    def check(ok, text):
        print(("PASS " if ok else "FAIL ") + text)
        if not ok:
            failures.append(text)

    # P1
    mbtis = [v["fixed_mbti"] for v in players.values()]
    check(len(players) == 16 and len(set(mbtis)) == 16, f"P1 one pool of 16 player agents with 16 distinct MBTI ({len(players)} agents)")
    played_before = Counter()
    for attempt in done:
        gid = attempt["game_id"]
        game = json.loads(db.execute("SELECT state FROM games WHERE id=?", (gid,)).fetchone()[0])
        roster = game["players"]  # agent_id -> player record
        check(set(roster) <= set(players), f"P1 seed {attempt['seed']}: all {len(roster)} players come from that pool")
        # P2
        chars = [p["character_id"] for p in roster.values()]
        char_mbti = [game["characters"][str(c)]["mbti"] for c in chars]
        check(len(set(roster)) == len(roster) and len(set(chars)) == len(chars) and len(set(char_mbti)) == len(char_mbti),
              f"P2 seed {attempt['seed']}: {len(roster)} distinct agents / characters / MBTI")
        for aid, p in roster.items():
            if game["characters"][str(p["character_id"])]["mbti"] != players[aid]["fixed_mbti"]:
                check(False, f"P2 seed {attempt['seed']}: {aid[-8:]} plays with a foreign MBTI")
        # P3: first prompt per character in this game
        first = {}
        for prompt in prompts(gid, db):
            first.setdefault(prompt["me"]["character_id"], prompt)
        by_char = {p["character_id"]: aid for aid, p in roster.items()}
        if not first:
            check(False, f"P3 seed {attempt['seed']}: no prompts found")
        repeat, before = 0, len(failures)
        for cid, prompt in first.items():
            aid = by_char.get(cid)
            exp = prompt.get("experience") or {}
            expected = played_before[aid]
            ok = exp.get("games_played") == expected and (expected == 0 or exp.get("lessons"))
            repeat += expected > 0
            if not ok:
                check(False, f"P3 seed {attempt['seed']}: {aid[-8:]} expected {expected} past games, prompt shows {exp.get('games_played')}")
        check(len(failures) == before and len(first) == len(roster),
              f"P3 seed {attempt['seed']}: every first prompt reports the right past "
              f"({repeat} returning agents, {len(first) - repeat} newcomers, {len(first)}/{len(roster)} prompts seen)")
        summary_path = Path(attempt["summary_file"]) if attempt.get("summary_file") else None
        summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path and summary_path.exists() else {}
        hooks = summary.get("end_hooks", [])
        check(hooks and not any(str(h).startswith("failed") for h in hooks), f"seed {attempt['seed']}: {len(hooks)} game-end hooks ran")
        calls, stand_ins = summary.get("model_calls", 0), summary.get("model_fallbacks", 0)
        check(calls > 0 and stand_ins <= 0.1 * (calls + stand_ins),
              f"seed {attempt['seed']}: the model answered ({calls} calls, {stand_ins} rule-based stand-ins)")
        for aid in roster:
            played_before[aid] += 1
    # engine-side learning and history
    for aid, v in sorted(players.items(), key=lambda x: x[1]["fixed_mbti"]):
        learning = json.loads(db.execute("SELECT state FROM agent_learning WHERE agent_id=?", (aid,)).fetchone()[0])
        if len(v["history"]) != played_before[aid]:
            check(False, f"history of {aid[-8:]} has {len(v['history'])} games, expected {played_before[aid]}")
        if played_before[aid] and learning["version"] == 0:
            check(False, f"{v['fixed_mbti']} {aid[-8:]} played but strategy_state was never updated")
    check(not [f for f in failures if f.startswith("history") or "strategy_state" in f],
          "engine history and strategy_state advanced for every agent that played")
    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
