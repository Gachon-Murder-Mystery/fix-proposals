"""Tests for B's experiment-30 fixes (FIX-B1..B8): memory, prompt fields, leverage."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

B = Path(__file__).resolve().parents[1] / 'Agent_Reasoning'


def load_b():
    saved = {k: sys.modules.get(k) for k in ('schema', 'llm', 'agent')}
    try:
        mods = {}
        for name in ('schema', 'llm', 'agent', 'memory'):
            spec = importlib.util.spec_from_file_location(name, B / (name + '.py'))
            m = importlib.util.module_from_spec(spec)
            sys.modules[name] = m
            spec.loader.exec_module(m)
            mods[name] = m
        return mods
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


M = load_b()
POOLS = {'suspect': [{'id': i, 'name': f's{i}'} for i in range(12)],
         'weapon': [{'id': i, 'name': f'w{i}'} for i in range(8)],
         'place': [{'id': i, 'name': f'p{i}'} for i in range(8)]}


class FakePool:
    """pool/learning/get|update and pool/feedback, enough for ExperienceStore."""
    def __init__(self, history):
        self.history, self.version, self.state, self.updates = history, 0, {'tactics': [], 'weights': {}}, []
        self.events = [{'event_id': f'e{i}', 'strategy_consumed': False} for i in range(3)]

    def call(self, route, **d):
        if route == 'pool/learning/get':
            return {'learning': {'version': self.version, 'strategy_state': self.state},
                    'participation_history': self.history, 'experience_events': self.events}
        if route == 'pool/learning/update':
            assert d['expected_version'] == self.version
            self.version += 1
            self.state = d['strategy_state']
            self.updates.append(d)
            for e in self.events:
                e['strategy_consumed'] = e['event_id'] in d['experience_event_ids'] or e['strategy_consumed']
            return {}
        raise AssertionError(route)


class MemoryTests(unittest.TestCase):
    def test_rule_lessons_for_passive_timeout_and_wasted_explores(self):
        lessons, tactics, risk = M['memory'].rule_lessons(
            {'resolution': 'time_out', 'accused': False, 'cycles_played': 13, 'wasted_explores': 4})
        self.assertEqual(risk, 1)
        self.assertIn('prefer_unexplored_rooms', tactics)
        self.assertEqual(len(lessons), 2)

    def test_rule_lessons_after_wrong_accusation(self):
        _, tactics, risk = M['memory'].rule_lessons({'died': True, 'accused': True, 'combinations_at_accuse': 300})
        self.assertEqual((tactics, risk), (['verify_before_accuse'], -1))

    def test_record_then_load_round_trip_and_engine_update(self):
        with tempfile.TemporaryDirectory() as d:
            history = [{'game_id': 'g1', 'participation_index': 1, 'character_id': 8, 'final_score': 0,
                        'rank': None, 'survived': True, 'sub_missions': []}]
            pool = FakePool(history)
            store = M['memory'].ExperienceStore(pool, 'society:abc', {'characters': [{'id': 8, 'job': '기술자'}]}, d)
            self.assertEqual(store.load()['games_played'], 1)  # engine history alone already counts
            saved = store.record('g1', {'resolution': 'time_out', 'accused': False, 'cycles_played': 13,
                                        'wasted_explores': 3, 'combinations_left': 400}, {'final_score': 0})
            self.assertTrue(saved['engine'].startswith('updated'))
            self.assertEqual(pool.state['weights']['risk'], 1)
            exp = store.load()
            self.assertEqual(exp['recent_games'][0]['job'], '기술자')
            self.assertEqual(exp['recent_games'][0]['wasted_explores'], 3)
            self.assertTrue(exp['lessons'])
            self.assertIn('prefer_unexplored_rooms', exp['tactics'][0])
            # next game: the LLM refines the pending reflection
            pending = store.pending_reflection()
            self.assertEqual(pending['game_id'], 'g1')
            pool.events.append({'event_id': 'e9', 'strategy_consumed': False})
            store.complete_reflection('g1', ['새 교훈'], ['verify_before_accuse'], -1)
            self.assertIsNone(store.pending_reflection())
            self.assertEqual(store.load()['lessons'][0], '새 교훈')
            self.assertEqual(pool.state['weights']['risk'], 0)

    def test_no_pool_credentials_is_not_fatal(self):
        with tempfile.TemporaryDirectory() as d:
            store = M['memory'].ExperienceStore(None, 'society:x', None, d)
            self.assertEqual(store.load()['games_played'], 0)
            self.assertEqual(store.record('g', {}, None)['engine'], 'no_pool_credentials')


class BeliefLeverageTests(unittest.TestCase):
    def test_leverage_lists_what_a_place_exclusion_would_unlock(self):
        b = M['schema'].BeliefState(POOLS)
        b.apply({'id': 'c1', 'kind': 'indirect', 'type': 'character_room', 'character': 5, 'room': 4})
        b.apply({'id': 'c2', 'kind': 'indirect', 'type': 'weapon_room', 'weapon': 2, 'room': 4})
        b.apply({'id': 'c3', 'kind': 'indirect', 'type': 'companion', 'character': 6, 'other': 5})
        top = b.summary()['indirect_leverage'][0]
        self.assertEqual((top['if_place_excluded'], top['suspects_also_excluded'], top['weapons_also_excluded']),
                         (4, [5, 6], [2]))
        b.apply({'id': 'c4', 'kind': 'definite', 'category': 'place', 'candidate': 4})
        self.assertEqual(b.summary()['indirect_leverage'], [])  # unlocked: now real exclusions
        self.assertNotIn(5, b.remaining('suspect'))


class PromptTests(unittest.TestCase):
    def agent(self):
        a = M['agent'].LLMAgent('society:abc', catalog={'missions': [{'id': 'M20', 'name': '비폭력 협상가',
                                                                        'condition': '협박 0회'}]},
                                log_path=Path(tempfile.mkdtemp()) / 'log.jsonl')
        a.sync({'self': {'character_id': 0, 'mbti': 'INTP', 'mode': 'active', 'job': {'name': '탐정'},
                         'stats': {'observe': 5, 'speech': 3, 'pressure': 3}},
                'candidates': POOLS, 'cycle_id': 3, 'phase': 'interaction',
                'sub_missions': [{'mission_id': 'M20', 'tier': '하', 'achieved': False}]})
        return a

    def test_user_prompt_carries_the_new_fields(self):
        a = self.agent()
        a.experience = {'games_played': 2, 'lessons': ['x']}
        obs = {'cycle_id': 3, 'phase': 'interaction', 'self': a.profile,
               'time_budget': {'estimated_remaining_cycles': 4}, 'scoring': {'wrong_accusation': -20},
               'thresholds': {'explore': 2, 'by_target': {}}}
        u = json.loads(a._user_prompt(obs))
        self.assertEqual(u['experience']['games_played'], 2)
        self.assertEqual(u['time_budget']['estimated_remaining_cycles'], 4)
        self.assertEqual(u['accuse_math']['combinations_left'], 768)
        self.assertEqual(u['me']['thresholds']['source'], 'engine')
        self.assertEqual(len(u['me']['tendencies']), 4)
        self.assertEqual(u['sub_missions'][0]['name'], '비폭력 협상가')
        self.assertIn('자정 규칙', a._system_prompt())

    def test_old_engine_falls_back_to_stats_and_says_so(self):
        u = json.loads(self.agent()._user_prompt({'self': {'stats': {'observe': 5}}}))
        self.assertTrue(u['me']['thresholds']['source'].startswith('stats_only'))

    def test_room_notes_mark_exhausted_rooms(self):
        a = self.agent()
        a.ingest_result({'action': 'explore', 'room': 3, 'roll': {'successes': 1}, 'acquired_information': []})
        self.assertTrue(a.room_notes[3]['exhausted'])
        self.assertEqual(a.game_stats('time_out')['wasted_explores'], 1)

    def test_trade_helpers_no_longer_raise(self):  # FIX-B7: remaining() needed a category
        a = self.agent()
        a.known_info = [{'id': 'c1', 'kind': 'definite', 'category': 'suspect', 'candidate': 1}]
        self.assertIsNotNone(a.propose_trade({'occupants': [{'id': 2, 'mode': 'active'}]}))
        self.assertEqual(set(a._best_guess()), {'suspect', 'weapon', 'place'})


if __name__ == '__main__':
    unittest.main()
