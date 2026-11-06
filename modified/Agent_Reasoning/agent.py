"""LLM reasoning agent for competitive-v1.3. One observation in, one action out.

The engine owns rules, dice and disclosure; this file only chooses. Payload
shapes follow Game-engine/competitive.py `Rules.action` exactly, since a wrong
field is rejected as `invalid_input` and wastes the phase.

A belief_mode switch exists for the H2 comparison.
  True  -> structured belief (remaining candidates + entropy) goes into the prompt
  False -> only raw clue text goes in; the model judges on its own

An unheld ID in evidence_used is recorded rather than rejected: that is exactly
what H2 measures (hallucinated evidence).
"""
import json
import re
import time
from pathlib import Path

from schema import BeliefState
from llm import GatewayError

SCHEMA_VERSION = "b-0.4"
LOG_PATH = Path("decisions.jsonl")

CATEGORIES = ("suspect", "weapon", "place")
SIZES = {"suspect": 12, "weapon": 8, "place": 8}
APPROACHES = ("question", "interrogate", "threaten", "negotiate")
STAT_BY_APPROACH = {"explore": "observe", "question": "speech", "negotiate": "speech",
                    "interrogate": "pressure", "threaten": "pressure"}
FALLBACK = {"movement": ("stay", {}), "interaction": ("pass", {}), "deduction": ("pass", {})}
DECIDE_BUDGET = 4096
ANSWER_BUDGET = 2048
REFLECT_BUDGET = 2048
RECENT_ACTIONS = 8
# [FIX-B5] GAME_SPEC v1.3 B7: MBTI goes into the prompt as tendencies, not orders.
MBTI_TENDENCY = {
    "E": "E: 교류가 잦아 관계를 빨리 쌓지만, 질문 쌍 공개로 관심사가 드러난다",
    "I": "I: 노출이 적지만 관계 형성이 느리다",
    "S": "S: 직접 확인한 단서로 신중하게 추리한다(안전, 느림)",
    "N": "N: 추론으로 빨리 도전한다(빠름, 사망 위험)",
    "T": "T: 손익과 조건으로 판단한다",
    "F": "F: 관계로 판단한다",
    "J": "J: 약속을 지키고 예측 가능하다",
    "P": "P: 유연하지만 신뢰를 잃기 쉽다",
}
TACTIC_NAMES = ("verify_before_accuse", "ask_before_duplicate_search", "share_when_stalled",
                "parallelize_independent_tasks", "prefer_unexplored_rooms", "request_before_escalation")


class LLMAgent:
    def __init__(self, agent_id, client=None, belief_mode=True, catalog=None, log_path=LOG_PATH):
        self.agent_id = agent_id
        self.client = client
        self.belief_mode = belief_mode
        self.catalog = catalog or {}
        self.log_path = Path(log_path)

        self.initialized = False
        self.profile = {}
        self.belief = None
        self.known_info = []        # clues I hold, as the engine sent them
        self.applied_seen = []      # clue ids already folded into belief
        self.disclosed = []         # clue ids I have handed to someone
        self.candidates = {}
        self.relationships = {}
        self.sub_missions = []
        self.cycle = 0
        self.phase = None
        self.turn = 0
        self.inactive = False
        self._tokens = 0            # local tally if the client has no counter
        self._last_roll = None
        # [FIX-B1] cross-game experience block, set by the runtime before the first decision
        self.experience = None
        # [FIX-B4] within-game memory the prompt never had
        self.action_log = []        # my own past actions and their outcomes
        self.room_notes = {}        # room -> {"explores", "found", "exhausted"}
        self.clues_by_cycle = {}    # cycle -> number of clues held (stall detection)
        self.accused = False
        self.combinations_at_accuse = None
        self.last_observation = {}
        self.model_fallbacks = {}   # [FIX-B9] kind -> count of rule-based stand-ins for the model
        self.model_calls = 0

    # ---------- token accounting (deduction rank: fewest wins) ----------

    @property
    def output_tokens(self):
        totals = getattr(self.client, "usage_totals", None)
        if callable(totals):
            return int(totals().get("output") or 0)
        return self._tokens

    def _count(self, usage):
        if isinstance(usage, dict):
            self._tokens += int(usage.get("output") or 0)

    # ---------- stage 1: state sync ----------

    def sync(self, observation):
        me = observation.get("self") or {}

        if not self.initialized:
            self.profile = dict(me)
            self.initialized = True
        else:
            if me.get("personality") != self.profile.get("personality"):
                self._log({"event": "profile_changed",
                           "before": self.profile.get("personality"),
                           "after": me.get("personality")})
            self.profile = dict(me)

        if observation.get("candidates"):
            self.candidates = observation["candidates"]
        if self.belief_mode and self.belief is None and self.candidates:
            self.belief = BeliefState(self.candidates, agent_id=self.agent_id)

        self.relationships = observation.get("relationships") or {}
        self.sub_missions = observation.get("sub_missions") or []
        self.cycle = observation.get("cycle_id", self.cycle)
        self.phase = observation.get("phase")

        mode = me.get("mode", "active")
        if mode != "active" and not self.inactive:
            self.inactive = True
            self._log({"event": "mode_changed", "mode": mode})
        elif mode == "active":
            self.inactive = False

        self.known_info = observation.get("information") or []
        if self.belief_mode and self.belief is not None:
            held = {self._item_id(i) for i in self.known_info}
            for item in self.known_info + (observation.get("shared_information") or []):
                key = self._item_id(item)
                if key in self.applied_seen:
                    continue
                self.belief.apply(item, source="own" if key in held else "heard")
                self.applied_seen.append(key)
                self._log({"event": "clue_seen", "cycle": self.cycle, "clue_id": key,
                           "kind": item.get("kind"),
                           "source": "own" if key in held else "shared"})
        self.clues_by_cycle[self.cycle] = len(self.known_info)
        self.last_observation = observation
        self.turn += 1

    # ---------- stage 2: prompts ----------

    def _system_prompt(self):
        return (
            "너는 살인 미스터리 경쟁 추리 게임의 플레이어다. JSON 객체 하나만 출력한다.\n\n"
            "출력 형식:\n"
            "{\n"
            '  "evidence_used": [근거로 삼은 단서 ID 배열],\n'
            '  "current_hypothesis": {"suspect": 0~11, "weapon": 0~7, "place": 0~7, '
            '"confidence": 0.0~1.0},\n'
            '  "action": {"type": 행동, "payload": {...}},\n'
            '  "consideration": "관측 근거와 선택 이유를 2~4문장으로", "utterance": "GM에게 선언할 행동을 캐릭터 말투로 한 문장"\n'
            "}\n\n"
            "action.type은 반드시 allowed_actions 중 하나여야 한다.\n"
            "payload 형식(필드를 추가하면 거부된다):\n"
            '  move={"room": 0~7}  stay={}  prepare={}  pass={}\n'
            '  explore={} 또는 {"target_id": 같은 방 시체의 인물ID}\n'
            '  question={"target_id": 같은 방 생존 인물ID, "approach": '
            '"question"|"interrogate"|"threaten"|"negotiate", '
            '"hypothesis": {"suspect": ID, "weapon": ID, "place": ID}}\n'
            '  accuse={"suspect": ID, "weapon": ID, "place": ID}\n\n'
            "판정 규칙:\n"
            "- 주사위 2개를 굴린다. 직전에 prepare 했으면 3개.\n"
            "- 각 주사위가 기준선 이상이면 성공 1. 기준선 = 8 − 스탯, 최소 2 최대 6.\n"
            "- 쓰이는 스탯: explore는 observe, question·negotiate는 speech, "
            "interrogate·threaten은 pressure.\n"
            "- explore 획득: 성공 0개→0, 1개→1, 2개→1, 3개→2.\n"
            "- question 공개량: 0→없음, 1→모호 1, 2→확실 1 또는 모호 2, 3→확실 1 + 모호 2.\n"
            "- 상대와의 관계가 70 이상이면 question·negotiate 기준선 −1, 30 이하면 +1.\n\n"
            "접근 선택의 대가:\n"
            "- question: 성공 시 상대의 나에 대한 관계 +5, 실패 시 −5.\n"
            "- negotiate: question과 같고 교섭 제안을 겸한다.\n"
            "- interrogate: 즉시 관계 −5. 실패하면 다음 사이클 끝까지 그 인물에게 질문할 수 없다.\n"
            "- threaten: 즉시 관계 −10. 실패하면 내 단서 하나가 상대에게 노출된다.\n\n"
            "질문은 hypothesis에 적은 세 후보에 대해서만 답을 끌어낸다. "
            "이미 좁혀진 항목보다 아직 넓게 열린 항목을 물어라.\n"
            # [FIX-B2] was: "accuse는 맞히면 승리하고 틀리면 즉사한다. 확신이 없으면 단서를 더 모아라."
            # That line plus remaining_cycles=106 made passing the only sensible move.
            "accuse는 맞히면 승리하고 틀리면 즉사한다. 판단 재료는 셋이다: "
            "accuse_math.success_if_random(남은 조합 중 하나를 고를 때의 성공 확률), "
            "scoring(실제 점수표), time_budget.estimated_remaining_cycles(시간 제한까지 반영한 실제 남은 사이클). "
            "남은 사이클이 0이 되면 정답자 없이 전원 패배한다. 남은 기회와 점수 기대값을 비교해 고발 여부를 정하라.\n"
            # [FIX-B8] the midnight rule was never stated, so the model discarded indirect clues
            "자정 규칙: 범인과 흉기는 자정에 범행 장소에 있었다. 따라서 어떤 방이 범행 장소가 아니라고 밝혀지면, "
            "자정에 그 방에 있던 인물과 흉기도 정답이 아니다. belief는 이 규칙을 이미 계산해 반영한다. "
            "belief.indirect_leverage는 장소 하나가 제외될 때 함께 제외될 후보 목록이다. unlocks가 큰 장소를 "
            "질문 hypothesis의 place로 넣으면 간접 단서가 실제 제외로 이어질 가능성이 커진다.\n"
            # [FIX-B3]
            "me.thresholds는 엔진이 스킬·현재 방·관계를 반영해 계산한 실제 기준선이다. "
            "by_target은 같은 방 인물별·접근 방식별 기준선이다. 기준선이 낮을수록 성공하기 쉽다.\n"
            # [FIX-B4]
            "recent_actions는 내가 직전에 한 행동과 결과다. room_notes에서 exhausted=true인 방은 "
            "주사위에 성공해도 아무것도 얻지 못한 방이므로 내게 남은 단서가 없다.\n"
            # [FIX-B5]
            "me.tendencies는 너의 MBTI 경향이다. 지시가 아니라 경향이며, 실제 선택은 목표에 따라 네가 정한다.\n"
            # [FIX-B1]
            "experience는 같은 너(같은 풀 에이전트)가 이전 게임들에서 겪은 결과와 스스로 정리한 교훈이다. "
            "사건의 정답과 인물 배치는 매 게임 바뀌므로, 사실이 아니라 전략에 반영하라.\n"
            "선택 이유에는 근거 ID, 대안과의 비교, 불확실성을 간결히 명시하라. 내부 사고 전체를 작성할 필요는 없다.\n"
            "evidence_used에는 실제로 보유한 단서 ID만 적는다."
        )

    def _thresholds(self, observation, stats):
        """[FIX-B3] Prefer the engine's effective thresholds (skills, room, relation).
        Older engines do not send them; then fall back to stats only and say so."""
        engine = observation.get("thresholds")
        if isinstance(engine, dict):
            return dict(engine, source="engine")
        return {**{a: min(6, max(2, 8 - (stats.get(s) or 0))) for a, s in STAT_BY_APPROACH.items()},
                "source": "stats_only (skills/room/relation not applied)"}

    def _accuse_math(self):
        if self.belief is None:
            return None
        counts = {c: len(self.belief.remaining(c)) for c in CATEGORIES}
        combos = counts["suspect"] * counts["weapon"] * counts["place"]
        return {"combinations_left": combos,
                "success_if_random": round(1.0 / combos, 4) if combos else None}

    def _missions(self):
        """[FIX-B6] Sub-missions arrived as bare IDs ("M20"); attach name and condition."""
        catalog = {m.get("id"): m for m in (self.catalog.get("missions") or []) if isinstance(m, dict)}
        out = []
        for m in self.sub_missions:
            info = catalog.get(m.get("mission_id"), {})
            out.append(dict(m, name=info.get("name"), condition=info.get("condition")))
        return out

    def _tendencies(self, mbti):
        return [MBTI_TENDENCY[x] for x in (mbti or "") if x in MBTI_TENDENCY]

    def _user_prompt(self, observation):
        me = observation.get("self") or {}
        stats = me.get("stats") or {}
        state = {
            "cycle": observation.get("cycle_id"),
            "remaining_cycles": observation.get("remaining_cycles"),
            "time_budget": observation.get("time_budget"),      # [FIX-B2]
            "scoring": observation.get("scoring"),              # [FIX-B2]
            "accuse_math": self._accuse_math(),                 # [FIX-B2]
            "phase": observation.get("phase"),
            "allowed_actions": observation.get("allowed_actions") or [],
            "me": {
                "character_id": me.get("character_id"),
                "name": me.get("name"),
                "job": (me.get("job") or {}).get("name"),
                "mbti": me.get("mbti"),
                "tendencies": self._tendencies(me.get("mbti")),   # [FIX-B5]
                "room": me.get("room"),
                "prepared": me.get("prepared"),
                "stats": stats,
                "skill": me.get("skill"),
                "thresholds": self._thresholds(observation, stats),   # [FIX-B3]
            },
            "recent_actions": self.action_log[-RECENT_ACTIONS:],    # [FIX-B4]
            "room_notes": {str(k): v for k, v in sorted(self.room_notes.items())},
            "experience": self.experience,                          # [FIX-B1]
            "occupants": [{"id": x.get("id"), "name": x.get("name"),
                           "mode": x.get("mode"), "job": x.get("job")}
                          for x in observation.get("occupants") or []],
            "positions": [{"id": x.get("id"), "room": x.get("room"), "mode": x.get("mode")}
                          for x in observation.get("positions") or []],
            "relationships": self.relationships,
            "sub_missions": self._missions(),                       # [FIX-B6]
            "my_information": [self._readable(i) for i in self.known_info],
            "heard_statements": (observation.get("statements") or [])[-8:],
            "already_disclosed": self.disclosed,
            "announcements": observation.get("announcements") or [],
            "question_pairs": (observation.get("question_pairs") or [])[-10:],
        }
        if self.belief_mode and self.belief is not None:
            state["belief"] = self.belief.summary()
        else:
            state["candidates"] = {k: [c.get("name") for c in (self.candidates.get(k) or [])]
                                   for k in CATEGORIES}
        if self._last_roll:
            state["last_roll"] = self._last_roll
        return json.dumps(state, ensure_ascii=False)

    # ---------- stage 3: decision ----------

    def _request_complete(self, system, user, budget):
        # Retry the whole structured decision, never execute or splice a fragment.
        for attempt in range(1, 4):
            self._log({"event":"generation_attempt", "attempt":attempt, "budget":budget})
            try:
                result = self.client.request(system, user, budget=budget)
                self.model_calls = getattr(self, "model_calls", 0) + 1  # [FIX-B9]
                self._log({"event":"generation_accepted", "attempt":attempt, "budget":budget})
                return result
            except Exception as exc:
                code = getattr(exc, "code", None)
                body = getattr(exc, "body", {})
                if isinstance(body, dict): code = body.get("code", code)
                cut = code in ("truncated", "llm_truncated")
                self._log({"event":"generation_failed", "attempt":attempt, "budget":budget,
                           "code":code, "truncated":cut, "reason":str(exc)})
                if cut and attempt < 3 and budget < 16384:
                    budget = min(16384, budget * 2)
                    continue
                if isinstance(exc, GatewayError): raise
                raise GatewayError(str(exc), "truncated" if cut else "http") from exc

    def decide(self, observation):
        self.last_utterance = None
        allowed = observation.get("allowed_actions") or []
        phase = observation.get("phase") or "interaction"
        if not allowed:
            return FALLBACK.get(phase, ("pass", {}))
        if self.client is None:
            return self._rule_action(observation, allowed)

        started = time.time()
        budget, attempt, last = DECIDE_BUDGET, 0, ""
        while attempt < 2:
            attempt += 1
            try:
                text, usage = self._request_complete(self._system_prompt(),
                                                  self._user_prompt(observation), budget=budget)
                self._count(usage)
            except GatewayError as exc:
                code = getattr(exc, "code", "http")
                self._log({"event": "llm_error", "cycle": self.cycle, "phase": phase,
                           "code": code, "reason": str(exc)[:200]})
                # Gateway faults are excluded from the H2 denominator.
                return self._fallback(observation, allowed, "gateway", attempt, started)
            parsed = self._parse(text)
            last = text[:300]
            if parsed is None:
                continue
            action = (parsed.get("action") or {}).get("type")
            payload = (parsed.get("action") or {}).get("payload") or {}
            ok, reason = self._validate(action, payload, observation)
            if not ok:
                self._log({"event": "invalid_output", "cycle": self.cycle, "phase": phase,
                           "action": action, "reason": reason, "attempt": attempt})
                continue
            payload = self._finalize(action, payload)
            self.last_utterance = parsed.get("utterance")
            self._record(parsed, action, payload, observation, attempt, started, usage)
            return action, payload

        self._log({"event": "invalid_output", "cycle": self.cycle, "phase": phase,
                   "reason": "unparseable", "raw": last})
        return self._fallback(observation, allowed, "invalid_output", attempt, started)

    def _finalize(self, action, payload):
        if action == "accuse":
            # The engine ranks correct deductions by this number, fewest first.
            payload["output_tokens"] = self.output_tokens
            self.accused = True
            self.combinations_at_accuse = (self._accuse_math() or {}).get("combinations_left")
        return payload

    def _record(self, parsed, action, payload, observation, attempt, started, usage):
        held = {self._item_id(i) for i in self.known_info}
        used = [x for x in (parsed.get("evidence_used") or []) if isinstance(x, str)]
        self._log({"event": "decision", "cycle": self.cycle, "phase": observation.get("phase"),
                   "action": action, "payload": payload,
                   "evidence_used": used,
                   "ungrounded_evidence": [x for x in used if x not in held],
                   "hypothesis": parsed.get("current_hypothesis"),
                   "attempt": attempt, "fallback": None,
                   "belief_mode": self.belief_mode, "usage": usage,
                   "elapsed": round(time.time() - started, 3)})
        if parsed.get("consideration"):
            self._log({"event": "consideration", "cycle": self.cycle,
                       "context": "action", "text": str(parsed["consideration"])})

    def _fallback(self, observation, allowed, kind, attempt, started):
        # [FIX-B9] Count it. The runtime's `fallbacks` never saw these, so a game in which
        # every model call failed still reported player_fallbacks = 0.
        counts = self.__dict__.setdefault("model_fallbacks", {})
        counts[kind] = counts.get(kind, 0) + 1
        action, payload = self._rule_action(observation, allowed)
        self._log({"event": "decision", "cycle": self.cycle, "phase": observation.get("phase"),
                   "action": action, "payload": payload, "evidence_used": [],
                   "ungrounded_evidence": [], "hypothesis": None,
                   "attempt": attempt, "fallback": kind,
                   "belief_mode": self.belief_mode, "usage": None,
                   "elapsed": round(time.time() - started, 3)})
        return action, payload

    def _rule_action(self, observation, allowed):
        """No-LLM policy: never waste a phase, never gamble on accuse."""
        me = observation.get("self") or {}
        if "explore" in allowed:
            corpse = next((x for x in observation.get("occupants") or []
                           if x.get("mode") == "corpse"), None)
            return "explore", ({"target_id": corpse["id"]} if corpse else {})
        if "question" in allowed:
            living = [x for x in observation.get("occupants") or []
                      if x.get("mode") != "corpse"]
            if living:
                return "question", {"target_id": living[0]["id"], "approach": "question",
                                    "hypothesis": self._best_guess()}
            return "prepare", {}
        if "move" in allowed:
            return "move", {"room": ((me.get("room") or 0) + 1) % 8}
        return FALLBACK.get(observation.get("phase") or "interaction", ("pass", {}))

    def _best_guess(self):
        if self.belief is not None:
            remaining = self._remaining_all()
            return {k: (remaining.get(k) or [0])[0] for k in CATEGORIES}
        return {k: 0 for k in CATEGORIES}

    def _remaining_all(self):
        """[FIX-B7] BeliefState.remaining(category) needs a category; three call sites
        called it with none and would raise TypeError if reached."""
        return {c: self.belief.remaining(c) for c in CATEGORIES}

    def _validate(self, action, payload, observation):
        allowed = observation.get("allowed_actions") or []
        if action not in allowed:
            return False, f"{action}은 이번 단계에 허용되지 않는다"
        if not isinstance(payload, dict):
            return False, "payload는 객체여야 한다"
        occupants = {x.get("id"): x for x in observation.get("occupants") or []}

        if action == "move":
            if set(payload) != {"room"} or not self._in_range(payload.get("room"), 8):
                return False, "move는 room 0~7 하나만 받는다"
        elif action in ("stay", "pass", "prepare"):
            if payload:
                return False, f"{action}에는 payload가 없다"
        elif action == "explore":
            if not set(payload) <= {"target_id"}:
                return False, "explore는 target_id 외의 필드를 받지 않는다"
            if "target_id" in payload:
                target = occupants.get(payload["target_id"])
                if target is None or target.get("mode") != "corpse":
                    return False, "explore의 target_id는 같은 방의 시체여야 한다"
        elif action == "question":
            if not set(payload) <= {"target_id", "approach", "hypothesis"}:
                return False, "question은 target_id/approach/hypothesis만 받는다"
            target = occupants.get(payload.get("target_id"))
            if target is None or target.get("mode") == "corpse":
                return False, "question 대상은 같은 방의 살아있는 인물이어야 한다"
            if payload.get("approach") not in APPROACHES:
                return False, "approach는 question/interrogate/threaten/negotiate 중 하나다"
            hypothesis = payload.get("hypothesis")
            if not isinstance(hypothesis, dict) or set(hypothesis) != set(CATEGORIES):
                return False, "hypothesis는 suspect/weapon/place 세 값이 모두 필요하다"
            for key in CATEGORIES:
                if not self._in_range(hypothesis[key], SIZES[key]):
                    return False, f"hypothesis.{key} 값이 범위를 벗어났다"
        elif action == "accuse":
            if not set(payload) <= set(CATEGORIES) | {"confidence", "elapsed_seconds"}:
                return False, "accuse는 suspect/weapon/place만 지정한다"
            for key in CATEGORIES:
                if not self._in_range(payload.get(key), SIZES[key]):
                    return False, f"{key} 값이 후보 범위를 벗어났다"
        else:
            return False, f"알 수 없는 행동 {action}"
        return True, ""

    # ---------- stage 4: answering another player's question ----------

    def answer_question(self, question):
        """Pick the form of disclosure. The engine already fixed the amount, and
        checks it for exact equality, so neither more nor less is accepted."""
        options = question.get("response_options") or {}
        successes = question.get("successes", 0)
        if successes <= 0:
            return []
        if self.client is None:
            return self._required(options, successes)

        prompt = json.dumps({
            "asker_id": question.get("asker_id"),
            "approach": question.get("approach"),
            "asker_hypothesis": question.get("hypothesis"),
            "successes": successes,
            "relationship_to_asker": self.relationships.get(str(question.get("asker_character"))),
            "my_information": [self._readable(i) for i in self.known_info],
            "already_disclosed": self.disclosed,
            "response_options": options,
        }, ensure_ascii=False)
        system = (
            "너는 질문을 받은 인물이다. 공개량은 엔진이 성공 수로 이미 정했고, "
            "너는 무엇을 어떤 형태로 말할지만 고른다. JSON 하나만 출력한다.\n\n"
            '{"statements": [...], "consideration": "관측 근거와 선택 이유를 2~4문장으로", "utterance": "GM에게 선언할 행동을 캐릭터 말투로 한 문장"}\n\n'
            "성공 수별 형태(정확히 맞아야 한다. 더 줘도 거부된다): "
            "1 → 모호 1개. 2 → 확실 1개 또는 모호 2개. 3 → 확실 1개 + 모호 2개.\n"
            'statements 항목: {"kind":"definite","clue_id":ID} / '
            '{"kind":"ambiguous","category":...,"candidate":...} / '
            '{"kind":"indirect","clue_id":ID,"text":...}\n'
            "indirect의 text는 반드시 그 단서의 narrative_options 중 하나를 그대로 쓴다. "
            "직접 지어낸 문장은 거부된다.\n"
            "response_options 밖의 것은 절대 말하지 않는다. 거짓말은 엔진이 차단한다.\n"
            "성공 2일 때 확실 1개와 모호 2개 중 고를 수 있다. 관계가 나쁜 상대나 "
            "나와 같은 가설을 쫓는 상대에게는 가치가 낮은 쪽을 내줘라."
        )
        try:
            text, usage = self._request_complete(system, prompt, budget=ANSWER_BUDGET)
            self._count(usage)
        except GatewayError as exc:
            self._log({"event": "llm_error", "cycle": self.cycle, "context": "question",
                       "code": getattr(exc, "code", "http"), "reason": str(exc)[:200]})
            counts = self.__dict__.setdefault("model_fallbacks", {})  # [FIX-B9]
            counts["answer_gateway"] = counts.get("answer_gateway", 0) + 1
            return self._required(options, successes)

        parsed = self._parse(text)
        statements = (parsed or {}).get("statements")
        if not isinstance(statements, list):
            statements = self._required(options, successes)
        for s in statements:
            if isinstance(s, dict) and s.get("clue_id") and s["clue_id"] not in self.disclosed:
                self.disclosed.append(s["clue_id"])
        self._log({"event": "question_response", "cycle": self.cycle,
                   "asker_id": question.get("asker_id"), "approach": question.get("approach"),
                   "successes": successes, "statements": statements,
                   "held": len(self.known_info), "usage": usage})
        if parsed and parsed.get("consideration"):
            self._log({"event": "consideration", "cycle": self.cycle,
                       "context": "question", "text": str(parsed["consideration"])})
        return statements

    @staticmethod
    def _required(options, successes):
        """Mirror of the engine's disclosure tier, used when the model is unavailable."""
        definite = [{"kind": "definite", "clue_id": c} for c in (options.get("definite") or [])[:1]]
        vague = [{"kind": "ambiguous", "category": x["category"], "candidate": x["candidate"]}
                 for x in options.get("ambiguous") or []]
        vague += [{"kind": "indirect", "clue_id": x["clue_id"], "text": x["narrative_options"][0]}
                  for x in options.get("indirect") or []]
        if successes <= 0:
            return []
        if successes == 1:
            return vague[:1] or definite
        if successes == 2:
            return definite or vague[:2]
        return definite + vague[:2]

    # ---------- stage 5: result intake ----------

    def ingest_result(self, result, payload=None):
        gained = []
        known = {self._item_id(i) for i in self.known_info}
        for item in result.get("acquired_information") or []:
            key = self._item_id(item)
            if key not in known:
                self.known_info.append(item)
                known.add(key)
                gained.append(key)
            if self.belief_mode and self.belief is not None and key not in self.applied_seen:
                self.belief.apply(item, source="own")
                self.applied_seen.append(key)
        self._last_roll = result.get("roll")
        self._remember(result, payload or {}, gained)
        self._log({"event": "result", "cycle": self.cycle,
                   "action": result.get("action"), "outcome": result.get("outcome"),
                   "roll": result.get("roll"), "acquired": gained,
                   "held": len(result.get("held_information_ids") or self.known_info)})

    def _remember(self, result, payload, gained):
        """[FIX-B4] Keep my own action history and what each room still holds for me.
        An explore that rolled successes but returned nothing means the room has no clue
        left that I do not already hold (clues are depleted per player)."""
        action = result.get("action")
        if action in ("move", "stay", "pass"):
            return
        roll = result.get("roll") or {}
        entry = {"cycle": result.get("cycle_id", self.cycle), "action": action,
                 "room": result.get("room"), "outcome": result.get("outcome"),
                 "successes": roll.get("successes"), "new_clues": len(gained)}
        if action == "question":
            entry.update(target=payload.get("target_id"), approach=payload.get("approach"))
        self.action_log.append(entry)
        if action == "explore" and result.get("room") is not None:
            note = self.room_notes.setdefault(result["room"], {"explores": 0, "found": 0, "exhausted": False})
            note["explores"] += 1
            note["found"] += len(gained)
            if (roll.get("successes") or 0) > 0 and not result.get("acquired_information"):
                note["exhausted"] = True

    # ---------- [FIX-B1] game end and reflection ----------

    def game_stats(self, resolution):
        """Deterministic summary of this game, stored for the next one (no LLM)."""
        explores = [a for a in self.action_log if a["action"] == "explore"]
        questions = [a for a in self.action_log if a["action"] == "question"]
        math_ = self._accuse_math() or {}
        stalled = best = 0
        previous = None
        for cycle in sorted(self.clues_by_cycle):
            held = self.clues_by_cycle[cycle]
            stalled = stalled + 1 if previous is not None and held <= previous else 0
            best, previous = max(best, stalled), held
        me = (self.last_observation.get("self") or {})
        return {"resolution": resolution, "cycles_played": self.cycle,
                "character_id": me.get("character_id"), "job": (me.get("job") or {}).get("name"),
                "mbti": me.get("mbti"),
                "accused": self.accused, "died": me.get("mode") == "corpse" or None,
                "combinations_at_accuse": self.combinations_at_accuse,
                "combinations_left": math_.get("combinations_left"),
                "clues_held": len(self.known_info),
                "explores": len(explores),
                "wasted_explores": sum(1 for a in explores if (a["successes"] or 0) > 0 and a["new_clues"] == 0),
                "questions": len(questions),
                "question_clues": sum(a["new_clues"] for a in questions),
                "stalled_cycles": best}

    def reflect(self, entry):
        """One LLM call at the start of the next game (the engine refuses calls after a
        game has ended). Returns (lessons, tactics, risk) or None to keep rule lessons."""
        if self.client is None or not entry:
            return None
        system = ("너는 방금 끝난 살인 미스터리 게임을 돌아보는 플레이어다. JSON 하나만 출력한다.\n"
                  '{"lessons": [다음 게임에 쓸 일반화된 교훈 1~3개, 각 한 문장], '
                  '"tactics": [아래 목록에서 0~3개], "risk": -1|0|1}\n'
                  f"tactics 목록: {', '.join(TACTIC_NAMES)}\n"
                  "risk: 다음 게임에서 고발을 더 과감히(+1), 그대로(0), 더 신중히(-1).\n"
                  "사건 정답·인물 이름·방 번호처럼 이번 사건에만 해당하는 사실은 쓰지 마라. "
                  "다음 게임은 다른 사건이고 다른 캐릭터를 맡을 수 있다.")
        user = json.dumps({"this_game": entry.get("stats"), "official_feedback": entry.get("feedback"),
                           "rule_based_lessons": entry.get("lessons")}, ensure_ascii=False)
        try:
            text, usage = self._request_complete(system, user, budget=REFLECT_BUDGET)
            self._count(usage)
        except Exception as exc:
            self._log({"event": "reflection_failed", "reason": str(exc)[:200]})
            return None
        parsed = self._parse(text) or {}
        lessons = [str(x)[:300] for x in parsed.get("lessons") or [] if str(x).strip()][:3]
        tactics = [t for t in parsed.get("tactics") or [] if t in TACTIC_NAMES][:3]
        risk = parsed.get("risk") if parsed.get("risk") in (-1, 0, 1) else 0
        self._log({"event": "reflection", "game_id": entry.get("game_id"), "lessons": lessons,
                   "tactics": tactics, "risk": risk})
        return (lessons, tactics, risk) if lessons else None

    # ---------- stage 6: negotiation (not an action; no phase cost) ----------

    def propose_trade(self, observation):
        """Return a trades/propose body, or None to skip this phase."""
        held = [i for i in self.known_info if i.get("kind") in ("definite", "indirect")]
        living = [x for x in observation.get("occupants") or [] if x.get("mode") != "corpse"]
        if not held or not living or self.belief is None:
            return None
        remaining = self._remaining_all()  # [FIX-B7]
        # Ask about whichever category is still widest open; offer the narrowest.
        want = max(remaining, key=lambda k: len(remaining[k]))
        if len(remaining[want]) <= 1:
            return None
        spare = min(held, key=lambda i: len(remaining.get(i.get("category"), [])))
        target = max(living, key=lambda x: self.relationships.get(str(x["id"]), 50))
        self._log({"event": "trade_proposed", "cycle": self.cycle,
                   "target_id": target["id"], "offered": spare["id"], "requested": want})
        return {"target_id": target["id"], "offer_ids": [spare["id"]],
                "requested_category": want}

    def respond_to_trade(self, trade):
        """accept / refuse / counter. A broken settled trade costs -20 relationship,
        so only accept when something ownable actually matches the request."""
        want = trade.get("requested_category")
        mine = [i for i in self.known_info
                if i.get("kind") in ("definite", "indirect")
                and i.get("category", "indirect") == want]
        if not mine:
            return {"decision": "refuse"}
        decision = {"decision": "accept", "offer_ids": [mine[0]["id"]]}
        if self.belief is not None:
            remaining = self._remaining_all()  # [FIX-B7]
            incoming = (trade.get("offer_categories") or {}).get(trade.get("proposer"))
            # Counter if they want my scarcest category and offer nothing scarcer.
            if incoming and len(remaining.get(want, [])) < len(remaining.get(incoming, [])):
                decision = {"decision": "counter", "offer_ids": [mine[0]["id"]],
                            "offer_category": want}
        self._log({"event": "trade_response", "cycle": self.cycle,
                   "trade_id": trade.get("trade_id"), "decision": decision["decision"]})
        return decision

    # ---------- helpers ----------

    @staticmethod
    def _in_range(value, size):
        return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < size

    @staticmethod
    def _item_id(item):
        return item.get("id") if isinstance(item, dict) else item

    @staticmethod
    def _readable(item):
        if not isinstance(item, dict):
            return {"id": item}
        keep = ("id", "kind", "type", "category", "candidate", "text", "excludes",   # [FIX-B4] keep type
                "character", "weapon", "room", "other")
        return {k: item[k] for k in keep if k in item}

    @staticmethod
    def _parse(text):
        if not isinstance(text, str):
            return None
        body = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
        start, end = body.find("{"), body.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(body[start:end + 1])
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _log(self, record):
        try:
            from research_trace import emit
            emit("agent_journal", record=record)
        except ImportError:
            pass
        record.update({"agent_id": self.agent_id,
                       "schema_version": SCHEMA_VERSION,
                       "ts": round(time.time(), 3)})
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
