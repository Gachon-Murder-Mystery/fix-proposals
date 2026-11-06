"""One persistent pool agent: player or GM, whichever role the engine gives it now.

`AgentWorker` polls `pool/session`. As a player it runs a player runtime; when the
engine promotes it to GM it drops the player runtime entirely (spec B8, no memory
carried) and runs `CompetitiveGMWorker`.

The player runtime is B's slot. Contract (V13_ALIGNMENT_20261026.md section 4):

    factory(role_client, session, catalog) -> object with step() -> str

`step()` must be non-blocking: observe, answer pending questions from
`response_options`, submit at most one `agent/action` for the current phase and
send `agent/heartbeat` well inside the 40 s lease. The engine rolls the dice for
the submitted action; the GM continues the scenario from the result.

`ScriptedPlayerRuntime` is the reference implementation with C's scripted policy
(no LLM). It is the stand-in until B's runtime is plugged in.
"""
import hashlib
import importlib
import json
import uuid
from pathlib import Path

from .competitive_gm import CompetitiveGMWorker
from .competitive_player import CompetitivePlayer

_PLAYER = json.loads(Path(__file__).with_name("competitive_settings.json").read_text(encoding="utf-8"))["player_runtime"]
REJECTED = ("disclosure_required", "untruthful", "invalid_input")


def _code(exc):
    return getattr(exc, "code", None)


def legal_statements(answer, question):
    """Keep only statements inside the engine's current response domain (narration from its grammar)."""
    options, result = question["response_options"], []
    for s in answer.get("statements", []):
        if s.get("kind") == "definite" and s.get("clue_id") in options["definite"]:
            result.append({"kind": "definite", "clue_id": s["clue_id"]})
        elif s.get("kind") == "ambiguous" and any(x["category"] == s.get("category") and x["candidate"] == s.get("candidate")
                                                 for x in options["ambiguous"]):
            result.append({"kind": "ambiguous", "category": s["category"], "candidate": s["candidate"]})
        elif s.get("kind") == "indirect":
            option = next((x for x in options["indirect"] if x["clue_id"] == s.get("clue_id")), None)
            if option:
                text = s.get("narration", s.get("text"))
                result.append({"kind": "indirect", "clue_id": s["clue_id"],
                               "text": text if text in option["narrative_options"] else option["narrative_options"][0]})
    return result


def required_statements(question):
    """Smallest answer that meets the success-tier disclosure (spec A6) from the engine's options."""
    options, n = question["response_options"], question["successes"]
    definite = [{"kind": "definite", "clue_id": clue} for clue in options["definite"][:1]]
    vague = [{"kind": "ambiguous", "category": x["category"], "candidate": x["candidate"]} for x in options["ambiguous"]]
    vague += [{"kind": "indirect", "clue_id": x["clue_id"], "text": x["narrative_options"][0]} for x in options["indirect"]]
    if n <= 0:
        return []
    if n == 1:
        return vague[:1] or definite
    if n == 2:
        return definite or vague[:2]
    return definite + vague[:2]


class ScriptedPlayerRuntime:
    """Reference player runtime: engine communication around C's scripted CompetitivePlayer."""

    live_llm = False

    def __init__(self, role, session, catalog):
        self.role, self.catalog = role, catalog
        self.seed = int(hashlib.sha256(f"player:{session['game_id']}:{session['agent_id']}".encode()).hexdigest()[:8], 16)
        self.policy, self.applied = None, set()
        self.fallbacks, self.idle = 0, 0

    def _update(self, obs):
        me = obs["self"]
        if self.policy is None:
            sheet = next(c for c in self.catalog["characters"] if c["id"] == me["character_id"])
            self.policy = CompetitivePlayer(me["id"], sheet, me["mbti"], self.seed, missions=obs["sub_missions"],
                                            names={c["id"]: c["name"] for c in self.catalog["characters"]},
                                            places={p["id"]: p["name"] for p in self.catalog["places"]})
        for info in obs["information"]:
            marker = (info["id"], info["kind"], info.get("text"))
            if marker not in self.applied:
                self.policy.ingest({"clues": [info]})
                self.applied.add(marker)
        # A narrated clue can later arrive in full; answer only with clues held in full.
        self.policy.held = [x for x in obs["information"] if x["kind"] in ("definite", "indirect")]
        simple = {"room": me["room"], "cycle": obs["cycle_id"],
                  "relationship": {int(k): v for k, v in obs["relationships"].items()},
                  "achieved_missions": [m["mission_id"] for m in obs["sub_missions"] if m["achieved"]],
                  "here": [{"id": c["id"], "kind": "player" if c["mode"] == "active" else c["mode"]} for c in obs["occupants"]]}
        self.policy.observe(simple)
        return simple

    def _answer(self):
        for question in self.role.call("questions/inbox")["questions"]:
            answer = self.policy.respond(question | {"asker_id": question["asker_character"]})
            try:
                self.role.call("questions/respond", question_id=question["question_id"], response_id=uuid.uuid4().hex,
                               statements=legal_statements(answer, question))
            except Exception as exc:
                if _code(exc) == "expired":
                    # The engine owns timeout settlement; never retry a closed question.
                    continue
                if _code(exc) not in REJECTED:
                    raise
                self.fallbacks += 1  # The policy's form did not meet the tier; disclose the required minimum.
                try:
                    self.role.call("questions/respond", question_id=question["question_id"], response_id=uuid.uuid4().hex,
                                   statements=required_statements(question))
                except Exception as retry_exc:
                    if _code(retry_exc) != "expired":
                        raise


    def _choose(self, obs, simple):
        if obs["phase"] == "movement":
            choice = self.policy.move(simple)
            return choice["action"], ({"room": choice["room"]} if choice["action"] == "move" else {})
        if obs["phase"] == "interaction":
            choice = self.policy.interact(simple)
            if choice["action"] == "explore":
                target = choice.get("target", "room")
                return "explore", ({} if target == "room" else {"target_id": int(target.split(":")[1])})
            if choice["action"] == "question":
                return "question", {k: choice[k] for k in ("target_id", "approach", "hypothesis")}
            return choice["action"], {}
        decision = self.policy.deduce()
        if decision["decision"] == "challenge":
            return "accuse", decision["accusation"] | {"output_tokens": decision["reasoning_tokens"]}
        return "pass", {}

    def step(self):
        obs = self.role.call("agent/observe")
        if obs["ended"] or obs["self"]["mode"] != "active":
            return "inactive"
        simple = self._update(obs)
        self._answer()
        if not obs["remaining_actions"]:
            self.idle += 1
            if self.idle % _PLAYER["heartbeat_every_idle_steps"] == 0:
                self.role.call("agent/heartbeat")
            return "waiting"
        self.idle = 0
        action, payload = self._choose(obs, simple)
        request = {k: obs[k] for k in ("game_id", "cycle_id", "phase", "phase_epoch")}
        request.update(agent_id=obs["self"]["id"], action_id=uuid.uuid4().hex, action=action, payload=payload)
        result = self.role.call("agent/action", request=request)
        if action == "explore" and result["execution_status"] == "completed":
            # The engine rolled; the result carries the dice for this player's own belief update.
            self.policy.ingest({"action": "explore", "target": "corpse" if payload else "room",
                                "clues": result["acquired_information"], "successes": result["roll"]["successes"]})
        return action


def load_factory(spec):
    """`package.module:callable` -> player factory (B's runtime); None -> scripted stand-in."""
    if not spec:
        return ScriptedPlayerRuntime
    module, _, name = spec.partition(":")
    return getattr(importlib.import_module(module), name)


class AgentWorker:
    """Engine-assigned role of one pool member, stepped by a runner."""

    def __init__(self, identity, player_factory=None):
        self.identity = identity
        self.factory = player_factory or ScriptedPlayerRuntime
        self.catalog = identity.call("catalog")
        self.gm = CompetitiveGMWorker(identity)
        self.player, self.context = None, None
        self.runtimes = []  # Every player runtime this member had, for the run summary.

    def step(self):
        if self.gm.gm is not None and not self.gm.gm.reported:
            return "gm:" + self.gm.step()  # A finished term still owes its REPORT.
        session = self.identity.call("pool/session")
        if session["role"] == "gm":
            self.player, self.context = None, None  # B8: no player context carried into the GM term.
            return "gm:" + self.gm.step()
        if session["role"] != "player":
            self.player, self.context = None, None
            return session["role"]
        if self.context != session["context_key"]:
            # [FIX-C1] The player runtime belongs to this persistent pool member, so it gets the
            # member's own pool credential to read/write its cross-game learning
            # (pool/learning/get|update, pool/feedback). Game secrets stay behind role tokens.
            session = dict(session, pool_credentials=dict(self.identity.credentials))
            self.player = self.factory(self.identity.bind(**session["credentials"]), session, self.catalog)
            self.context = session["context_key"]
            self.runtimes.append(self.player)
        return "player:" + self.player.step()
