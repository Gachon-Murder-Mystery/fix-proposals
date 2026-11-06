"""Tests for the experiment-30 fixes in competitive.py (FIX-A1..A3).

Reuses CompetitiveTests' fixture (3 players: characters 0 탐정, 1 배우, 2 사업가)
without re-running its tests.
"""
import unittest

from competitive import WRONG_ACCUSE_SCORE
from competitive_rules import REWARDS
from integration import ContractError
import test_competitive as base  # module import: do not re-collect its tests


class FixTests(unittest.TestCase):
    _base = base.CompetitiveTests
    setUp = _base.setUp
    call, pc, state, mutate, picks, action, advance, interaction = (
        _base.call, _base.pc, _base.state, _base.mutate, _base.picks, _base.action, _base.advance, _base.interaction)

    def observe(self, index):
        return self.call('agent/observe', self.pc(self.ids[index]))

    # FIX-A1 ---------------------------------------------------------------
    def test_time_budget_off_by_default_follows_deadline(self):
        budget = self.observe(0)['time_budget']
        self.assertIsNone(budget['time_limit_seconds'])
        self.assertEqual(budget['estimated_remaining_cycles'], self.observe(0)['remaining_cycles'])
        self.assertEqual(budget['binding_limit'], 'deadline_cycles')

    def test_time_budget_reports_the_wall_clock_as_binding(self):
        # 2 cycles finished in 40 s -> 20 s/cycle; 60 s left -> current cycle + 3 more = 4
        self.mutate(lambda g: (g['rules'].update(time_limit_seconds=100), g.update(cycle_id=3)))
        self.now += 40
        budget = self.observe(0)['time_budget']
        self.assertEqual((budget['elapsed_seconds'], budget['average_cycle_seconds'], budget['remaining_seconds']),
                         (40.0, 20.0, 60.0))
        self.assertEqual(budget['estimated_remaining_cycles'], 4)
        self.assertEqual(budget['binding_limit'], 'time_limit_seconds')
        self.assertGreater(self.observe(0)['remaining_cycles'], 100)  # what players were told before

    # FIX-A2 ---------------------------------------------------------------
    def test_thresholds_include_skills_and_match_the_roll(self):
        self.interaction()  # everyone in room 0, interaction phase
        detective, actor, merchant = (self.observe(i)['thresholds'] for i in range(3))
        self.assertEqual(detective['explore'], 2)                  # observe 5 -> 3, 현장 감식 -1
        self.assertEqual(actor['by_target']['0']['question'], 2)   # speech 5 -> 3, 감정 연기 -1
        self.assertEqual(merchant['by_target']['0']['negotiate'], 3)  # speech 4 -> 4, 거래의 달인 -1
        self.assertEqual(merchant['by_target']['0']['question'], 4)
        result, _ = self.action(self.ids[0], 'explore')
        self.assertEqual(result['roll']['threshold'], detective['explore'])

    # FIX-A3 ---------------------------------------------------------------
    def finish_by_timeout(self):
        self.call('games/operator', self.op, game_id='case', operation='TIMEOUT')
        self.call('gm/command', self.gm, command_id='t-announce', operation='ANNOUNCE', ref='time_out', kind='time_out')
        self.call('gm/command', self.gm, command_id='t-finish', operation='FINISH')
        return self.state()['final_feedback']

    @staticmethod
    def base(feedback):
        return feedback['final_score'] - sum(REWARDS[m['tier']] for m in feedback['sub_missions'] if m['achieved'])

    def test_unsolved_score_defaults_to_v13_zero(self):
        self.assertEqual({self.base(f) for f in self.finish_by_timeout().values()}, {0})

    def test_unsolved_score_option_applies_to_survivors(self):
        self.mutate(lambda g: g['rules'].update(unsolved_score=WRONG_ACCUSE_SCORE))
        self.assertEqual(self.observe(0)['scoring']['unsolved_survivor'], WRONG_ACCUSE_SCORE)
        self.assertEqual({self.base(f) for f in self.finish_by_timeout().values()}, {WRONG_ACCUSE_SCORE})

    def test_rule_values_are_validated(self):
        for rules in ({'unsolved_score': 5}, {'time_limit_seconds': -1}):
            with self.assertRaises(ContractError) as exc:
                self.call('games/create', self.op, game_id='bad', seed=1, rules=rules)
            self.assertEqual(exc.exception.code, 'invalid_input')


if __name__ == '__main__':
    unittest.main()
