"""agent/schema.py

BeliefState only. The action vocabulary lives in the engine contract and the
decision rows are written by agent.py's _log, so a second copy here would only
drift out of step.

Ruleset v1.3 has two clue shapes, and that is what this file is built around.

  definite  rules out exactly one id in one category, on its own
  indirect  is a midnight fact (who was in which room, where a weapon was,
            who was with whom). On its own it rules out nothing. It only bites
            when combined with other facts.

So belief is no longer "apply one clue, drop one candidate". It is a closure
over everything I hold, recomputed from scratch. The closure here is a port of
the engine's competitive_rules.infer(), deliberately kept rule-for-rule
identical: a belief that eliminates more than the engine can prove is a
hallucination, and a belief that eliminates less wastes clues.

evidence[cat][id] keeps the support set -- which clue IDs together killed that
candidate. H2 compares the evidence the model cites against what actually moved
the belief, so that trail has to exist, and with indirect clues the trail is a
set rather than a single ID.

narrated and possession packets carry no checkable fact (the engine strips the
original), so they are counted but never asserted.
"""
import copy
import json
import math

SCHEMA_VERSION = "b-0.4"
CATEGORIES = ("suspect", "weapon", "place")
# The engine sends pools under either key depending on the view, so accept both.
ALIASES = {"suspect": ("suspect", "suspects"), "weapon": ("weapon", "weapons"),
           "place": ("place", "places")}
OPAQUE_KINDS = ("narrated", "possession")


class BeliefState:
    def __init__(self, candidates, agent_id=None):
        self.schema_version = SCHEMA_VERSION
        self.agent_id = agent_id
        entries = {c: self._entries(candidates, c) for c in CATEGORIES}
        self.pools = {c: [self._ident(i) for i in entries[c]] for c in CATEGORIES}
        self.names = {c: {self._ident(i): self._name(i) for i in entries[c]}
                      for c in CATEGORIES}
        self.eliminated = {c: set() for c in CATEGORIES}
        self.evidence = {c: {} for c in CATEGORIES}   # id -> [clue_id, ...]
        self.facts = {}             # clue_id -> parsed fact
        self.opaque = set()         # narrated/possession ids, held but unusable
        self.known_clues = set()    # clues I hold myself
        self.heard_clues = set()    # clues that reached me through sharing

    @staticmethod
    def _entries(candidates, category):
        for key in ALIASES[category]:
            value = (candidates or {}).get(key)
            if value:
                return value
        return []

    @staticmethod
    def _ident(item):
        return item.get("id") if isinstance(item, dict) else item

    @staticmethod
    def _name(item):
        return item.get("name") if isinstance(item, dict) else str(item)

    # ---------- update ----------

    @staticmethod
    def _parse(clue):
        """Engine clue -> fact, or None when there is nothing checkable in it."""
        kind = clue.get("kind")
        if kind == "definite":
            category = clue.get("category")
            # v1.3 names the excluded candidate `candidate`; `excluded_id` is the
            # old field name and is still accepted so legacy fixtures keep working.
            candidate = clue.get("candidate", clue.get("excluded_id"))
            if category in CATEGORIES and isinstance(candidate, int):
                return {"t": "definite", "category": category, "candidate": candidate}
            return None
        if kind != "indirect":
            return None
        shape = clue.get("type")
        if shape == "character_room" and isinstance(clue.get("room"), int):
            return {"t": "character_room", "character": clue.get("character"),
                    "room": clue["room"]}
        if shape == "weapon_room" and isinstance(clue.get("room"), int):
            return {"t": "weapon_room", "weapon": clue.get("weapon"), "room": clue["room"]}
        if shape == "companion":
            return {"t": "companion", "character": clue.get("character"),
                    "other": clue.get("other")}
        return None

    def apply(self, clue, source="own"):
        """Fold in one clue and reclose. Returns whether the belief moved."""
        if not isinstance(clue, dict):
            return False
        clue_id = clue.get("id")
        if clue_id is not None:
            (self.known_clues if source == "own" else self.heard_clues).add(clue_id)
        if clue_id is None:
            return False

        fact = self._parse(clue)
        if fact is None:
            if clue.get("kind") in OPAQUE_KINDS:
                self.opaque.add(clue_id)
            return False
        # A narrated packet can later arrive in full, so a real fact always wins.
        self.opaque.discard(clue_id)
        if self.facts.get(clue_id) == fact:
            return False        # repeats stay harmless
        self.facts[clue_id] = fact
        return self.infer()

    def infer(self):
        """Recompute the closure over every fact held. Returns whether it moved."""
        excluded = {c: {} for c in CATEGORIES}      # id -> support set
        positions = {"suspect": {}, "weapon": {}}   # id -> (room, support set)
        companions = []

        for clue_id, fact in self.facts.items():
            shape = fact["t"]
            if shape == "definite":
                if fact["candidate"] in self.pools[fact["category"]]:
                    excluded[fact["category"]][fact["candidate"]] = {clue_id}
            elif shape == "character_room":
                positions["suspect"][fact["character"]] = (fact["room"], {clue_id})
            elif shape == "weapon_room":
                positions["weapon"][fact["weapon"]] = (fact["room"], {clue_id})
            elif shape == "companion":
                companions.append((fact["character"], fact["other"], {clue_id}))

        rooms = self.pools["place"]
        changed = True
        while changed:
            changed = False

            # Someone seen with a located person was in that same room.
            for a, b, support in companions:
                for x, y in ((a, b), (b, a)):
                    if x not in positions["suspect"] and y in positions["suspect"]:
                        room, basis = positions["suspect"][y]
                        positions["suspect"][x] = (room, support | basis)
                        changed = True

            scene = [i for i in rooms if i not in excluded["place"]]
            for cat in ("suspect", "weapon"):
                for i, (room, support) in positions[cat].items():
                    if i not in self.pools[cat]:
                        continue
                    basis = excluded["place"].get(room)
                    if basis is None and len(scene) == 1 and room != scene[0]:
                        # Only one room can be the scene, and this is not it.
                        basis = set().union(*excluded["place"].values())
                    if basis is not None and i not in excluded[cat]:
                        excluded[cat][i] = basis | support
                        changed = True

                # The one survivor must be the culprit/weapon, so the scene is
                # wherever it was at midnight, and every other room is out.
                alive = [i for i in self.pools[cat] if i not in excluded[cat]]
                if len(alive) == 1 and alive[0] in positions[cat] and excluded[cat]:
                    room, support = positions[cat][alive[0]]
                    support = support | set().union(*excluded[cat].values())
                    for i in rooms:
                        if i != room and i not in excluded["place"]:
                            excluded["place"][i] = support
                            changed = True

        moved = False
        for category in CATEGORIES:
            killed = set(excluded[category])
            if killed != self.eliminated[category]:
                moved = True
            self.eliminated[category] = killed
            self.evidence[category] = {i: sorted(s) for i, s in excluded[category].items()}
        return moved

    # ---------- query ----------

    def remaining(self, category):
        return [i for i in self.pools[category] if i not in self.eliminated[category]]

    def entropy(self, category):
        n = len(self.remaining(category))
        return math.log2(n) if n else 0.0   # uniform, so entropy is log2(count)

    def total_entropy(self):
        return sum(self.entropy(c) for c in CATEGORIES)

    def settled(self, category):
        alive = self.remaining(category)
        return alive[0] if len(alive) == 1 else None

    def evidence_for(self, category, ident):
        return list(self.evidence[category].get(ident, []))

    def summary(self):
        """Goes straight into the prompt, so keep it small and readable."""
        out = {}
        for category in CATEGORIES:
            alive = self.remaining(category)
            share = round(1.0 / len(alive), 4) if alive else 0.0
            out[category] = {
                "remaining": [{"id": i, "name": self.names[category].get(i), "p": share}
                              for i in alive],
                "remaining_count": len(alive),
                "excluded_count": len(self.eliminated[category]),
                "entropy": round(self.entropy(category), 3),
                "settled": self.settled(category),
            }
        shapes = [f["t"] for f in self.facts.values()]
        out["total_entropy"] = round(self.total_entropy(), 3)
        # Indirect facts that have not bitten yet are the reason to keep asking,
        # so the model is told how many it is sitting on.
        out["midnight_facts"] = {
            "character_rooms": shapes.count("character_room"),
            "weapon_rooms": shapes.count("weapon_room"),
            "companions": shapes.count("companion"),
        }
        out["definite_count"] = shapes.count("definite")
        out["unusable_count"] = len(self.opaque)
        out["indirect_leverage"] = self.indirect_leverage()
        return out

    def indirect_leverage(self):
        """[FIX-B8] Turn held midnight facts into an inquiry compass.

        infer() can only use a midnight position once that room is excluded as the scene
        (the culprit and the weapon were in the scene at midnight). In experiment-30 few
        places were ever excluded, so indirect clues almost never bit. This lists, for each
        open place, which open suspects/weapons would fall *if* that place were excluded,
        so the model can aim its question hypothesis at the place that unlocks the most.
        Pure read-out of facts already held: it eliminates nothing by itself."""
        located = {"suspect": {}, "weapon": {}}
        for fact in self.facts.values():
            if fact["t"] == "character_room":
                located["suspect"][fact["character"]] = fact["room"]
            elif fact["t"] == "weapon_room":
                located["weapon"][fact["weapon"]] = fact["room"]
        for a, b in [(f["character"], f["other"]) for f in self.facts.values() if f["t"] == "companion"]:
            for x, y in ((a, b), (b, a)):
                if x not in located["suspect"] and y in located["suspect"]:
                    located["suspect"][x] = located["suspect"][y]
        result = []
        for place in self.remaining("place"):
            drops = {cat: sorted(i for i, room in located[cat].items()
                                 if room == place and i in self.remaining(cat))
                     for cat in ("suspect", "weapon")}
            if drops["suspect"] or drops["weapon"]:
                result.append({"if_place_excluded": place,
                               "suspects_also_excluded": drops["suspect"],
                               "weapons_also_excluded": drops["weapon"],
                               "unlocks": len(drops["suspect"]) + len(drops["weapon"])})
        return sorted(result, key=lambda x: -x["unlocks"])

    # ---------- serialisation (for C's takeover / forced termination) ----------

    def to_dict(self):
        return {
            "schema_version": self.schema_version,
            "agent_id": self.agent_id,
            "pools": copy.deepcopy(self.pools),
            "names": {c: {str(k): v for k, v in d.items()} for c, d in self.names.items()},
            "eliminated": {c: sorted(s) for c, s in self.eliminated.items()},
            "evidence": {c: {str(k): list(v) for k, v in d.items()}
                         for c, d in self.evidence.items()},
            "facts": copy.deepcopy(self.facts),
            "opaque": sorted(self.opaque),
            "known_clues": sorted(self.known_clues),
            "heard_clues": sorted(self.heard_clues),
        }

    @classmethod
    def from_dict(cls, d):
        b = cls({}, d.get("agent_id"))
        b.schema_version = d.get("schema_version", SCHEMA_VERSION)
        b.pools = {c: list(v) for c, v in d["pools"].items()}
        b.names = {c: {int(k): v for k, v in sub.items()} for c, sub in d["names"].items()}
        b.eliminated = {c: set(v) for c, v in d["eliminated"].items()}
        b.evidence = {c: {int(k): list(v) for k, v in sub.items()}
                      for c, sub in d["evidence"].items()}
        b.facts = dict(d.get("facts", {}))
        b.opaque = set(d.get("opaque", []))
        b.known_clues = set(d.get("known_clues", []))
        b.heard_clues = set(d.get("heard_clues", []))
        return b

    def to_json(self):
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, s):
        return cls.from_dict(json.loads(s))


# ---------- self-test ----------

if __name__ == "__main__":
    pools = {"suspect": [{"id": i, "name": f"s{i}"} for i in range(12)],
             "weapon": [{"id": i, "name": f"w{i}"} for i in range(8)],
             "place": [{"id": i, "name": f"p{i}"} for i in range(8)]}
    b = BeliefState(pools, agent_id="player_1")
    before = b.total_entropy()

    # A definite clue rules out one candidate by itself.
    assert b.apply({"id": "c1", "kind": "definite", "category": "suspect", "candidate": 3})
    assert 3 not in b.remaining("suspect")
    assert b.evidence_for("suspect", 3) == ["c1"]
    assert not b.apply({"id": "c1", "kind": "definite", "category": "suspect", "candidate": 3})

    # An indirect clue alone proves nothing.
    assert not b.apply({"id": "c2", "kind": "indirect", "type": "character_room",
                        "character": 5, "room": 4})
    assert 5 in b.remaining("suspect")

    # Combined with a ruled-out room it does: suspect 5 was not at the scene.
    assert b.apply({"id": "c3", "kind": "definite", "category": "place", "candidate": 4})
    assert 5 not in b.remaining("suspect")
    assert b.evidence_for("suspect", 5) == ["c2", "c3"]

    # Companions inherit the room, so suspect 6 falls out of the same two clues.
    assert b.apply({"id": "c4", "kind": "indirect", "type": "companion",
                    "character": 5, "other": 6})
    assert 6 not in b.remaining("suspect")
    assert b.evidence_for("suspect", 6) == ["c2", "c3", "c4"]

    # Narrated text is held but never asserted.
    assert not b.apply({"id": "c5", "kind": "narrated", "text": "누군가 있었어요."},
                       source="heard")
    assert "c5" in b.heard_clues and "c5" in b.opaque

    assert BeliefState.from_json(b.to_json()).to_json() == b.to_json()
    assert json.dumps(b.summary(), ensure_ascii=False)   # must stay serialisable

    print("entropy:", round(before, 3), "->", round(b.total_entropy(), 3))
    print("settled:", {c: b.settled(c) for c in CATEGORIES})
    print("schema self-test passed")
