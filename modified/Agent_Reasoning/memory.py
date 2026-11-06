"""[FIX-B1] Cross-game experience of one persistent pool agent.

Why this file exists: in experiment-30 every scenario started with a fresh LLMAgent
and nothing in the prompt said what this agent had done before, so 30 games were 30
first games. The engine already keeps the official record per persistent agent_id
(participation history, competencies, strategy_state); B never read it.

Two stores, both keyed by the persistent pool agent_id. That id is only stable when
all scenarios run against the same pool DB (run_batch.py shares it by default now).

  engine (official)  pool/learning/get|update: participation_history, competencies,
                     strategy_state. Only the generalized TACTICS enum and numeric
                     weights are accepted there, by design (no case secrets).
  local JSON (B)     per-game statistics and the agent's own lessons in natural
                     language, which the engine deliberately does not store.

Timing: the engine refuses LLM calls once a game has ended, so
  on_game_end  -> record(): statistics + official feedback + rule-based lessons (no LLM)
  next game    -> pending_reflection() / complete_reflection(): the agent, active again,
                  refines those lessons with one LLM call through the normal gateway.
"""
import json
import os
from pathlib import Path

TACTICS = ("verify_before_accuse", "ask_before_duplicate_search", "share_when_stalled",
           "parallelize_independent_tasks", "prefer_unexplored_rooms", "request_before_escalation")
TACTIC_TEXT = {
    "verify_before_accuse": "고발 전에 남은 조합 수와 근거를 다시 확인한다",
    "ask_before_duplicate_search": "이미 뒤진 곳을 또 뒤지기 전에 다른 인물에게 묻는다",
    "share_when_stalled": "단서가 몇 사이클째 늘지 않으면 질문·교섭으로 돌파한다",
    "parallelize_independent_tasks": "서로 독립적인 조사를 한 사이클에 나눠 진행한다",
    "prefer_unexplored_rooms": "아직 다 뒤지지 않은 방을 우선 탐색한다",
    "request_before_escalation": "심문·협박 전에 일반 질문을 먼저 시도한다",
}
MAX_LESSONS = 6
RECENT_GAMES = 5


def default_dir():
    return Path(os.environ.get("GAME_MEMORY_DIR") or Path.cwd() / "runs" / "agent-memory")


def rule_lessons(stats):
    """Deterministic lessons and engine tactics from one game's statistics.
    Used immediately at game end, and as the fallback when no LLM is available."""
    lessons, tactics, risk = [], [], 0
    if stats.get("wasted_explores", 0) >= 2:
        lessons.append(f"성공했는데 아무것도 못 얻은 탐색이 {stats['wasted_explores']}번 있었다. "
                       "그 방은 내게 남은 단서가 없으니 room_notes를 보고 다른 방으로 옮긴다.")
        tactics.append("prefer_unexplored_rooms")
    if stats.get("died"):
        lessons.append(f"남은 조합이 {stats.get('combinations_at_accuse')}개일 때 고발해 사망했다. "
                       "고발 직전 성공 확률(1/남은 조합)을 점수표와 비교한다.")
        tactics.append("verify_before_accuse")
        risk -= 1
    elif stats.get("resolution") in ("time_out", "deadline") and not stats.get("accused"):
        lessons.append(f"{stats.get('cycles_played')}사이클 만에 {stats['resolution']}로 끝났고 한 번도 "
                       "고발하지 않았다. time_budget.estimated_remaining_cycles를 보고 마지막 기회를 놓치지 않는다.")
        risk += 1
    if stats.get("stalled_cycles", 0) >= 3:
        lessons.append(f"단서가 {stats['stalled_cycles']}사이클 연속 늘지 않았다. 탐색만 반복하지 말고 "
                       "질문 대상을 바꾼다.")
        tactics.append("share_when_stalled")
    if not lessons:
        lessons.append("특별한 실패 패턴 없음. 단서 수집 속도를 더 높일 방법을 찾는다.")
    return lessons, tactics, risk


class ExperienceStore:
    def __init__(self, pool_client, agent_id, catalog=None, directory=None):
        self.pool = pool_client
        self.agent_id = agent_id
        self.jobs = {c["id"]: c.get("job") or c.get("name")
                     for c in (catalog or {}).get("characters", []) if isinstance(c, dict)}
        self.path = Path(directory or default_dir()) / (agent_id.replace(":", "_") + ".json")

    # ---------- local file ----------
    def _local(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("games"), list):
                return data
        except (OSError, ValueError):
            pass
        return {"agent_id": self.agent_id, "games": []}

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        temporary.replace(self.path)

    # ---------- engine ----------
    def _official(self):
        if self.pool is None:
            return {}
        try:
            return self.pool.call("pool/learning/get")
        except Exception as exc:  # experience is an aid; never block a game on it
            return {"error": f"{type(exc).__name__}: {exc}"[:200]}

    # ---------- read: what the prompt shows ----------
    def load(self):
        """Compact experience block for the prompt. Empty games -> first game."""
        official = self._official()
        local = self._local()
        by_game = {g["game_id"]: g for g in local["games"]}
        history = official.get("participation_history") or []
        recent = []
        for h in history[-RECENT_GAMES:]:
            mine = by_game.get(h.get("game_id"), {})
            s = mine.get("stats", {})
            recent.append({
                "game_no": h.get("participation_index"),
                "job": self.jobs.get(h.get("character_id"), h.get("character_id")),
                "resolution": s.get("resolution"),
                "score": h.get("final_score"),
                "won": bool(h.get("rank")),
                "survived": h.get("survived"),
                "cycles_played": s.get("cycles_played"),
                "accused": s.get("accused"),
                "combinations_left_at_end": s.get("combinations_left"),
                "wasted_explores": s.get("wasted_explores"),
                "missions_achieved": sum(1 for m in h.get("sub_missions") or [] if m.get("achieved")),
            })
        lessons = list(dict.fromkeys(x for g in reversed(local["games"])   # newest first, no repeats
                                     for x in g.get("lessons", [])))[:MAX_LESSONS]
        strategy = ((official.get("learning") or {}).get("strategy_state") or {})
        scores = [h.get("final_score") or 0 for h in history]
        return {
            "games_played": len(history),
            "summary": (f"지난 {len(history)}게임: 승리 {sum(1 for h in history if h.get('rank'))}, "
                        f"오답 사망 {sum(1 for h in history if not h.get('survived'))}, "
                        f"평균 점수 {round(sum(scores) / len(scores), 1)}") if history else "첫 게임",
            "recent_games": recent,
            "lessons": lessons,
            "tactics": [f"{t}: {TACTIC_TEXT.get(t, t)}" for t in strategy.get("tactics", [])],
            "risk_weight": (strategy.get("weights") or {}).get("risk", 0),
            "source_error": official.get("error"),
        }

    # ---------- write: game end (no LLM) ----------
    def record(self, game_id, stats, feedback):
        lessons, tactics, risk = rule_lessons(stats)
        data = self._local()
        data["games"] = [g for g in data["games"] if g.get("game_id") != game_id]
        data["games"].append({"game_id": game_id, "stats": stats, "feedback": feedback,
                              "lessons": lessons, "lesson_source": "rule", "reflected": False})
        self._save(data)
        pushed = self.push_strategy(tactics, risk)
        return {"lessons": lessons, "tactics": tactics, "risk": risk, "engine": pushed}

    # ---------- write: next game, LLM refinement ----------
    def pending_reflection(self):
        games = self._local()["games"]
        return next((g for g in reversed(games) if not g.get("reflected")), None)

    def complete_reflection(self, game_id, lessons, tactics=(), risk=0):
        data = self._local()
        for g in data["games"]:
            if g.get("game_id") == game_id:
                if lessons:
                    g.update(lessons=list(lessons)[:3], lesson_source="llm")
                g["reflected"] = True
        self._save(data)
        if not lessons:  # no LLM result: rule lessons stay, already pushed at game end
            return "kept rule lessons"
        return self.push_strategy([t for t in tactics if t in TACTICS], risk)

    def push_strategy(self, tactics, risk_delta):
        """Fold tactics/risk into the engine's official strategy_state."""
        if self.pool is None:
            return "no_pool_credentials"
        try:
            got = self.pool.call("pool/learning/get")
            learning = got["learning"]
            ids = [e["event_id"] for e in got.get("experience_events", []) if not e.get("strategy_consumed")]
            if not ids:
                return "no_unconsumed_feedback"
            state = learning.get("strategy_state") or {"tactics": [], "weights": {}}
            merged = [t for t in TACTICS if t in set(state.get("tactics", [])) | set(tactics)]
            weights = dict(state.get("weights") or {})
            weights["risk"] = max(-10, min(10, weights.get("risk", 0) + risk_delta))
            version = learning["version"]
            for start in range(0, len(ids), 100):  # the engine accepts at most 100 ids per update
                self.pool.call("pool/learning/update", expected_version=version,
                               experience_event_ids=ids[start:start + 100],
                               strategy_state={"tactics": merged, "weights": weights})
                version += 1
            return f"updated to v{version}"
        except Exception as exc:
            return f"failed: {type(exc).__name__}: {exc}"[:200]
