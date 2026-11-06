"""Authoritative competitive rules v1.3. No normal GM/player decision policy.

Runs inside ExternalEngine's SQLite transaction. Only engine rules choose NPC
responses or mandatory timeout disclosure. Player/GM choices stay external.
"""
import copy
import hashlib
import json
import math
import random
import secrets
import uuid

from integration import VERSION, ContractError, require, integer, identifier, text, fingerprint, token_matches
from competitive_rules import DATA, MBTI, SHEETS, MISSIONS, TIERS, REWARDS, tier, candidates, build_case, infer, narrative_options, mission_complete
from engine import BACKGROUNDS, PERSONALITIES

SPEC = 'competitive-v1.3'
LEGAL = {'movement': ['move', 'stay'], 'interaction': ['explore', 'question', 'prepare', 'pass'],
         'deduction': ['accuse', 'pass']}
RULES = {'gm_lease': 40, 'player_lease': 40, 'question_timeout': 30, 'trade_timeout': 30,
         'deadline_cycles': 120, 'min_players': 3, 'max_players': 12,
         'successor_scope': 'active_then_pool', 'relationship_effects': True,
         # [FIX-A1] Wall-clock budget known to the engine (0 = off). The driver used to
         # enforce it alone, so players were told 106+ cycles remained when the game ended.
         'time_limit_seconds': 0,
         # [FIX-A3] Score of a surviving non-winner when the game ends unsolved
         # (deadline/time_out). 0 keeps v1.3 behaviour; see README for the -20 option.
         'unsolved_score': 0}
WIN_SCORES = [100, 70, 50, 30]
WRONG_ACCUSE_SCORE = -20


def catalog():
    from integration import catalog as legacy
    result = legacy()
    result.update(ruleset=SPEC, mode='competitive', characters=copy.deepcopy(DATA['characters']),
                  mbti=list(MBTI), missions=copy.deepcopy(DATA['missions']), sub_missions=copy.deepcopy(DATA['missions']),
                  default_rules=RULES.copy(), allowed_actions=copy.deepcopy(LEGAL), temporary_rules=[],
                  scoring={'ranks': [100, 70, 50, 30], 'dead': -20, 'survivor': 0, 'missions': REWARDS.copy()},
                  narrative_validation='closed-truth-preserving-grammar; arbitrary unverifiable prose rejected')
    for c in result['characters']:
        c.update(background=BACKGROUNDS[c['id']], voice=PERSONALITIES[c['id']])
    return result


def pool_key(pid): return 'competitive-pool:'+pid


class Rules:
    def __init__(self, engine): self.e = engine
    def tables(self, db):
        db.execute('CREATE TABLE IF NOT EXISTS competitive_records(game_id TEXT, kind TEXT, key TEXT, state TEXT NOT NULL, PRIMARY KEY(game_id,kind,key))')
        db.execute('CREATE TABLE IF NOT EXISTS competitive_events(game_id TEXT, sequence INTEGER, state TEXT NOT NULL, PRIMARY KEY(game_id,sequence))')
    def record(self, db, g, kind, key):
        self.tables(db)
        row = db.execute('SELECT state FROM competitive_records WHERE game_id=? AND kind=? AND key=?', (g['id'],kind,key)).fetchone()
        return json.loads(row[0]) if row else None
    def persist(self, db, g):
        self.tables(db)
        for e in g['events']:
            db.execute('INSERT OR REPLACE INTO competitive_events VALUES (?,?,?)',(g['id'],e['sequence'],json.dumps(e,ensure_ascii=False)))
        g['events'] = []
        for field, kind in (('commands','command'),('trade_receipts','trade_message'),('receipts','action'),('questions','question')):
            for key, value in list(g[field].items()):
                pending = value['result']['execution_status'] == 'pending' if field == 'receipts' else value['status'] == 'pending' if field == 'questions' else False
                if not pending:
                    db.execute('INSERT OR REPLACE INTO competitive_records VALUES (?,?,?,?)', (g['id'],kind,key,json.dumps(value,ensure_ascii=False)))
                    del g[field][key]
        self.e.put(db,'games',g['id'],g,'id')
    def all_events(self, db, g, after=0):
        self.tables(db)
        return [json.loads(x[0]) for x in db.execute('SELECT state FROM competitive_events WHERE game_id=? AND sequence>? ORDER BY sequence',(g['id'],after))]+[x for x in g['events'] if x['sequence']>after]
    def pool(self, db, pid):
        row = db.execute('SELECT value FROM integration_config WHERE key=?', (pool_key(pid),)).fetchone()
        require(row is not None, 'not_found', 'bootstrap the competitive pool first')
        import json
        return json.loads(row[0])
    def save_pool(self, db, pool):
        import json
        db.execute('INSERT OR REPLACE INTO integration_config VALUES (?,?)',
                   (pool_key(pool['pool_id']), json.dumps(pool)))
    def bootstrap(self, db, d):
        self.e.operator(d)
        pid = identifier(d.get('pool_id', 'society'), 'pool_id')
        row = db.execute('SELECT value FROM integration_config WHERE key=?', (pool_key(pid),)).fetchone()
        if row:
            p = self.pool(db, pid)
            if 'model_version' in d:
                require(d['model_version'] == p['model_version'], 'conflict', 'pool model is immutable')
            return {'pool_id': pid, 'current_gm_id': p['current_gm_id'], 'agents': self.cards(db, p)}
        model = text(d.get('model_version', 'gpt-6-luna'), 'model_version', 100)
        gm = identifier(d.get('gm_agent_id', pid+':gm'), 'gm_agent_id')
        ids = [gm]+[pid+':'+uuid.uuid4().hex for _ in MBTI]
        for index, aid in enumerate(ids):
            a = self.e.register(db, d | {'agent_id': aid, 'model_version': model})
            a.update(pool_id=pid, fixed_mbti=None if aid == gm else MBTI[index-1],
                     pool_role='gm' if aid == gm else 'player', history=[], negotiation_reputation={'fulfilled': 0, 'broken': 0})
            self.e.put(db, 'agents', aid, a)
        p = {'pool_id': pid, 'agent_ids': ids, 'current_gm_id': gm, 'model_version': model,
             'random_secret': secrets.token_hex(32)}
        self.save_pool(db, p)
        return {'pool_id': pid, 'current_gm_id': gm, 'agents': self.cards(db, p)}
    def cards(self, db, pool):
        result = []
        for aid in pool['agent_ids']:
            a = self.e.load(db, 'agents', aid)
            l = self.e.load(db, 'agent_learning', aid)
            result.append({k: copy.deepcopy(a[k]) for k in ('agent_id', 'model_version', 'pool_status', 'busy_game_id',
                           'fixed_mbti', 'pool_role', 'history', 'negotiation_reputation')} | {'learning_version': l['version']})
        return result
    def create(self, db, d):
        self.e.operator(d)
        pid = identifier(d.get('pool_id', 'society'), 'pool_id')
        pool = self.pool(db, pid)
        gid = identifier(d.get('game_id', str(uuid.uuid4())), 'game_id')
        require(not db.execute('SELECT 1 FROM games WHERE id=?', (gid,)).fetchone(), 'conflict', 'game_id exists')
        seed = integer(d.get('seed', 0), 'seed', 0, 2**32-1)
        provenance=d.get('provenance',{})
        require(isinstance(provenance,dict) and len(provenance)<=12 and all(isinstance(k,str) and isinstance(v,str) and len(k)<=80 and len(v)<=500 for k,v in provenance.items()),'invalid_input','bounded string provenance required')
        require(isinstance(d.get('rules', {}), dict), 'invalid_input', 'rules must be an object')
        rules = RULES | d.get('rules', {})
        require(set(rules) == set(RULES), 'invalid_input', 'unknown competitive rule')
        for k in ('gm_lease', 'player_lease', 'question_timeout', 'trade_timeout'):
            require(type(rules[k]) in (int, float) and math.isfinite(rules[k]) and 1 <= rules[k] <= 3600,
                    'invalid_input', 'invalid timeout')
        integer(rules['deadline_cycles'], 'deadline_cycles', 1, 1000)
        integer(rules['time_limit_seconds'], 'time_limit_seconds', 0, 7*86400)
        integer(rules['unsolved_score'], 'unsolved_score', WRONG_ACCUSE_SCORE, 0)
        require(rules['min_players'] == 3 and rules['max_players'] == 12 and
                rules['successor_scope'] == 'active_then_pool' and type(rules['relationship_effects']) is bool,
                'invalid_input', 'v1.3 requires 3..12 players and active_then_pool succession')
        gm = pool['current_gm_id']
        self.e.reserve(db, gm, gid)
        truth, timeline, clues = build_case(seed)
        chars = {}
        for cid, sheet in SHEETS.items():
            chars[str(cid)] = copy.deepcopy(sheet) | {'character_id': cid, 'controller': None,
                'mode': 'npc', 'mbti': MBTI[cid], 'goal': None, 'room': sheet['start_room'],
                'prepared': False, 'information': {c['id']: public_clue(c) for c in clues if c['source_room'] == sheet['start_room']}}
        g = {'id': gid, 'kind': 'external', 'mode': 'competitive', 'ruleset': SPEC, 'schema_version': VERSION,
             'pool_id': pid, 'seed': seed, 'rules': rules, 'truth': truth, 'timeline': timeline, 'clues': clues,'provenance':copy.deepcopy(provenance),
             'roll_secret': hashlib.sha256(f'{pool["random_secret"]}:roll:{seed}'.encode()).hexdigest(),
             'roll_counts': {}, 'characters': chars, 'players': {}, 'assignments': [],
             'controller': None, 'term': 0, 'cycle_id': 1, 'phase': 'movement', 'phase_epoch': 1,
             'spent': {}, 'map_version': 0, 'plans': [], 'events': [], 'receipts': {}, 'commands': {},
             'questions': {}, 'question_pairs': [], 'recent_questions': [], 'trades': {}, 'trade_receipts': {},
             'transfer_count': 0, 'relationships': {str(i): {str(j): 50.0 for j in range(12) if i != j} for i in range(12)},
             'activity': [], 'cycle_history': [], 'disclosures': [], 'deductions': {}, 'announcements': {},
             'status': 'awaiting_setup', 'ended': False, 'resolution': None, 'started': False,
             'failovers': 0, 'technical_failure': False, 'created_at': self.e.clock(), 'finished_at': None,
             'observer_token': secrets.token_urlsafe(32), 'gm_report': None}
        self.e.controller(g, gm)
        self.e.event(g, 'gm_selected', {'controller_id': gm, 'term': 1, 'mode': 'fixed-incumbent'})
        self.persist(db, g)
        return self.e.credentials(g) | {'observer_token': g['observer_token'], 'state': self.gm_view(db, g)}
    def ch(self, g, p): return g['characters'][str(p['character_id'])]
    def player(self, g, d, read=False):
        aid = identifier(d.get('agent_id'), 'agent_id')
        p = g['players'].get(aid)
        require(p is not None and token_matches(d.get('token'), p['token']) and
                (p['mode'] == 'active' or (read and p['mode'] == 'corpse')), 'unauthorized', 'own current player credential required')
        if not read:
            require(not g['ended'] and g['status'] == 'running' and g['resolution'] is None, 'not_running', 'game is not accepting actions')
        require(read or self.e.clock() < p['lease_until'], 'expired', 'player lease expired')
        return p
    def mission_candidates(self, db, g, activation):
        pool = self.pool(db, g['pool_id'])
        result = {}
        for item in activation:
            aid, cid = item['agent_id'], integer(item['character_id'], 'character_id', 0, 11)
            require(aid in pool['agent_ids'], 'unauthorized', 'agent outside this pool')
            a = self.e.load(db, 'agents', aid)
            previous = [x['mission_id'] for x in a['history'][-1].get('sub_missions', [])] if a['history'] else []
            require(a['pool_role'] == 'player' and a['fixed_mbti'] in MBTI, 'invalid_input', 'GM cannot become a player')
            result[aid] = candidates(g['seed'], aid, cid, a['fixed_mbti'], previous, len(activation))
        return result
    def preview_characters(self, db, g, activation):
        chars = [{k: copy.deepcopy(v) for k, v in c.items() if k in ('character_id','name','job','job_id','stats','skill','mbti')}
                 | {'controller': None} for c in g['characters'].values()]
        used = set()
        for item in activation:
            a = self.e.load(db, 'agents', item['agent_id'])
            c = chars[item['character_id']]
            c.update(controller=item['agent_id'], mbti=a['fixed_mbti'])
            used.add(a['fixed_mbti'])
        remaining = [x for x in MBTI if x not in used]
        random.Random(f'npc-mbti:{g["seed"]}').shuffle(remaining)
        for c in chars:
            if c['controller'] is None: c['mbti'] = remaining.pop()
        return chars
    def plan(self, db, g, d):
        require(not g['started'] and g['status'] == 'awaiting_setup', 'conflict', 'Role assignment is initial-only')
        require('tasks' not in d, 'invalid_input', 'competitive GM assigns Roles, not action tasks')
        require(type(d.get('map_version')) is int and d['map_version'] == g['map_version'], 'conflict', 'map version changed')
        activation = d.get('activate')
        require(isinstance(activation, list) and 3 <= len(activation) <= 12 and all(isinstance(x, dict) for x in activation),
                'invalid_input', 'assign three to twelve Roles at once')
        require(len({x.get('agent_id') for x in activation}) == len(activation) and
                len({x.get('character_id') for x in activation}) == len(activation), 'invalid_input', 'duplicate agent or character')
        choices = self.mission_candidates(db, g, activation)
        alternatives = d.get('alternatives', [])
        require(isinstance(alternatives, list) and len(alternatives) >= 2 and
                all(isinstance(x, dict) and isinstance(x.get('id'), str) and isinstance(x.get('summary'), str) for x in alternatives) and
                len({x['id'] for x in alternatives}) == len(alternatives) and
                d.get('selected_alternative') in [x['id'] for x in alternatives], 'invalid_input', 'two alternatives and a selection required')
        reason = text(d.get('reason'), 'reason')
        require(bool(reason.strip()), 'invalid_input', 'selection reason required')
        assigned_chars = {x['character_id'] for x in activation}
        assigned_agents = {x['agent_id']: x['character_id'] for x in activation}
        used_mbti = set()
        for item in activation:
            require(set(item) == {'agent_id', 'character_id', 'sub_missions'}, 'invalid_input', 'Role fields are agent_id, character_id, sub_missions')
            aid, cid = item['agent_id'], item['character_id']
            a = self.e.load(db, 'agents', aid)
            self.e.reserve(db, aid, g['id'])
            mbti = a['fixed_mbti']
            used_mbti.add(mbti)
            missions = item['sub_missions']
            require(isinstance(missions, list) and len(missions) == 3 and all(isinstance(x, dict) for x in missions), 'invalid_input', 'three missions required')
            require({x.get('tier') for x in missions} == set(TIERS) and len({x.get('mission_id') for x in missions}) == 3,
                    'invalid_input', 'one distinct mission per tier')
            for mission in missions:
                self.validate_mission(g, aid, cid, mbti, mission, choices[aid], assigned_chars, assigned_agents)
            assignment = {'assignment_id': str(uuid.uuid4()), 'agent_id': aid, 'character_id': cid,
                          'role': 'player', 'job_id': cid, 'status': 'active'}
            g['assignments'].append(assignment)
            g['players'][aid] = assignment | {'mode': 'active', 'token': secrets.token_urlsafe(32),
                'lease_until': self.e.clock()+g['rules']['player_lease'], 'visited': [SHEETS[cid]['start_room']],
                'acquired': [], 'ambiguous_statements': [], 'first_definite': None, 'relationship_below_30': False,
                'missions': [copy.deepcopy(x) | {'achieved': False, 'achieved_cycle': None} for x in missions]}
            c = g['characters'][str(cid)]
            c.update(controller=aid, mode='active', mbti=mbti, goal={'victory': 'solve_first', 'sub_missions': copy.deepcopy(missions)}, information={})
        rng = random.Random(f'npc-mbti:{g["seed"]}')
        remaining = [x for x in MBTI if x not in used_mbti]
        rng.shuffle(remaining)
        for c in g['characters'].values():
            if c['controller'] is None:
                c['mbti'] = remaining.pop()
        g.update(started=True, status='running', map_version=g['map_version']+1)
        g['time_started_at'] = self.e.clock()  # [FIX-A1] origin of the wall-clock budget
        g['plans'].append({'command_id': d['command_id'], 'alternatives': copy.deepcopy(alternatives),
                           'selected_alternative': d['selected_alternative'], 'reason': reason,
                           'map_version': g['map_version'], 'term': g['term']})
        self.e.event(g, 'roles_assigned', {'map_version': g['map_version'], 'player_count': len(activation)})
        return {'map_version': g['map_version'], 'started': True}
    def validate_mission(self, g, aid, cid, mbti, m, choices, assigned_chars, assigned_agents):
        mid, t = m.get('mission_id'), m.get('tier')
        require(t in TIERS and mid in choices[t], 'invalid_input', 'mission not in engine candidate slot')
        params, targets = m.get('params', {}), m.get('targets', {})
        require(isinstance(params, dict) and isinstance(targets, dict), 'invalid_input', 'invalid mission parameters')
        require(tier(mid, cid, mbti, params.get('N')) == t, 'invalid_input', 'wrong effective difficulty')
        require(set(params) <= ({'N'} if MISSIONS[mid].get('quantity') else set()), 'invalid_input', 'unsupported mission parameter')
        kind = MISSIONS[mid]['designate']
        if kind == 'player':
            require(targets.get('player') in assigned_agents and targets['player'] != aid, 'invalid_input', 'target another participant')
            require('character' not in targets or targets['character'] == assigned_agents[targets['player']], 'invalid_input', 'player/character target mismatch')
        elif kind in ('character', 'npc'):
            target = integer(targets.get('character'), 'target_character', 0, 11)
            require(target != cid and (kind != 'npc' or target not in assigned_chars), 'invalid_input', 'invalid character target')
        elif kind == 'place': integer(targets.get('place'), 'place', 0, 7)
        elif kind == 'indirect_clues':
            ids = targets.get('clue_ids', [])
            possible = {c['id'] for c in g['clues'] if c['kind'] == 'indirect'}
            require(isinstance(ids, list) and len(ids) == 3 and len(set(ids)) == 3 and all(x in possible for x in ids), 'invalid_input', 'three distinct indirect IDs required')
        elif kind is None: require(not targets, 'invalid_input', 'mission has no designated target')
        if kind == 'character' and 'player' in targets:
            require(targets['player'] in assigned_agents and assigned_agents[targets['player']] == targets['character'],
                    'invalid_input', 'designated player does not control that character')
        allowed = {'player', 'character', 'target_ei'} if kind in ('player','character') else {'character', 'target_ei'} if kind == 'npc' else {'clue_ids'} if kind == 'indirect_clues' else {'place'} if kind == 'place' else set()
        require(set(targets) <= allowed, 'invalid_input', 'unknown mission target field')
    def visible(self, g):
        return {'schema_version': VERSION, 'ruleset': SPEC, 'game_id': g['id'], 'mode': 'competitive',
            'cycle_id': g['cycle_id'], 'phase': g['phase'], 'phase_epoch': g['phase_epoch'], 'status': g['status'],
            'ended': g['ended'], 'resolution': g['resolution'], 'remaining_cycles': 0 if g['resolution']=='deadline' else max(0, g['rules']['deadline_cycles']-g['cycle_id']+1),
            'positions': [{'id': c['character_id'], 'character_id': c['character_id'], 'room': c['room'],
                           'mode': c['mode'], 'name': c['name'], 'job': c['job']} for c in g['characters'].values()],
            'question_pairs': copy.deepcopy(g['question_pairs']),
            'announcements': [copy.deepcopy(x['data']) for x in g['announcements'].values() if x['published']],
            'time_budget': self.time_budget(g), 'scoring': self.scoring(g)}
    def time_budget(self, g):
        """[FIX-A1] What a player needs to know about the real end of the game.

        remaining_cycles only counts deadline_cycles; with a wall-clock limit the game
        may end far earlier. estimated_remaining_cycles is the smaller of the two, using
        the average duration of the cycles already finished (None before cycle 2)."""
        cycles_left = 0 if g['resolution'] == 'deadline' else max(0, g['rules']['deadline_cycles']-g['cycle_id']+1)
        limit, start = g['rules'].get('time_limit_seconds', 0), g.get('time_started_at')
        result = {'time_limit_seconds': limit or None, 'elapsed_seconds': None, 'remaining_seconds': None,
                  'average_cycle_seconds': None, 'estimated_remaining_cycles': cycles_left,
                  'binding_limit': 'deadline_cycles',
                  'on_expiry': 'no winner; every player loses'}
        if start is None:
            return result
        elapsed = max(0.0, self.e.clock()-start)
        result['elapsed_seconds'] = round(elapsed, 1)
        finished = g['cycle_id']-1
        average = elapsed/finished if finished > 0 else None
        result['average_cycle_seconds'] = round(average, 1) if average else None
        if limit:
            remaining = max(0.0, limit-elapsed)
            result['remaining_seconds'] = round(remaining, 1)
            if average:
                # The current cycle counts as one if it can still finish in time.
                by_time = int(remaining // average) + (1 if remaining > 0 else 0)
                if by_time < cycles_left:
                    result.update(estimated_remaining_cycles=by_time, binding_limit='time_limit_seconds')
        return result
    def scoring(self, g):
        """[FIX-A3] The scoring that finish() applies, so players can reason about it."""
        return {'win_by_rank': list(WIN_SCORES), 'wrong_accusation': WRONG_ACCUSE_SCORE,
                'unsolved_survivor': g['rules'].get('unsolved_score', 0),
                'sub_mission_rewards': dict(REWARDS)}
    def pair(self, q): return {'question_id': q['question_id'], 'asker_id': q['asker_character'], 'target_id': q['target_id']}
    def gm_view(self, db, g):
        pool = self.pool(db, g['pool_id'])
        c = g['controller']
        cards = self.cards(db, pool)
        chars = [{k: copy.deepcopy(c[k]) for k in ('character_id', 'name', 'job_id', 'stats', 'skill', 'room', 'mode', 'mbti', 'controller')}
                 for c in g['characters'].values()]
        result = self.visible(g) | {'control': {'controller_id': c['controller_id'], 'term': c['term']} if c else None,
            'rules': copy.deepcopy(g['rules']), 'pool': cards, 'characters': chars, 'actors': chars,
            'active_agents': [a for a, p in g['players'].items() if p['mode'] == 'active'],
            'spent': copy.deepcopy(g['spent']), 'event_sequence': g.get('event_count',0), 'map_version': g['map_version'],
            'plans': copy.deepcopy(g['plans']), 'failovers': g['failovers'],
            'agent_map': self.agent_map(g),
            'role_assignments': [copy.deepcopy(a) for a in g['assignments'] if a['role'] == 'player'],
            'indirect_clue_ids': [c['id'] for c in g['clues'] if c['kind'] == 'indirect'],
            'questions': [self.pair(q) | {'pending': q['status'] == 'pending', 'successes': q['successes'],
                         'approach': q['approach'],
                         'transferred_ids': q.get('transferred_ids', [])} for q in list(g['questions'].values())+g['recent_questions']],
            'pending_announcements': [{'kind': a['kind'], 'ref': ref} for ref, a in g['announcements'].items() if not a['published']],
            'gm_report': copy.deepcopy(g['gm_report']), 'metrics': self.metrics(g)}
        if not g['started']:
            # Candidates are character-dependent; draw only for proposed Roles to keep inputs small.
            result['mission_candidate_endpoint'] = 'gm/mission-candidates'
        return result
    def player_view(self, g, p):
        c = self.ch(g, p)
        info = list(copy.deepcopy(c['information']).values())
        from integration import catalog as old_catalog
        own = {k: copy.deepcopy(c[k]) for k in ('character_id', 'name', 'stats', 'skill', 'room', 'prepared', 'mbti', 'mode', 'goal')}
        own.update(id=p['agent_id'], assignment_id=p['assignment_id'], job={'id': c['job_id'], 'name': c['job']},
                   personality={'id': c['mbti'], 'name': c['mbti']}, personal_goal={'name': 'solve_first'})
        missions = [{k: copy.deepcopy(x[k]) for k in ('mission_id', 'tier', 'targets', 'params', 'achieved', 'achieved_cycle')}
                    for x in p['missions']]
        for m in missions: m['targets'].pop('target_ei', None)
        own['goal'] = {'victory': 'solve_first', 'sub_missions': copy.deepcopy(missions)}
        allowed = LEGAL[g['phase']] if not g['ended'] and g['resolution'] is None and g['status'] == 'running' and p['mode'] == 'active' and p['agent_id'] not in g['spent'] else []
        # [FIX-A2] Effective thresholds for every option open right now (skills applied).
        here = [x for x in g['characters'].values() if x['character_id'] != c['character_id'] and x['room'] == c['room']]
        thresholds = {'explore': self.threshold(g, c, 'explore', None),
                      'by_target': {str(t['character_id']): {a: self.threshold(g, c, a, t)
                                    for a in ('question', 'negotiate', 'interrogate', 'threaten')}
                                    for t in here if t['mode'] != 'corpse'}}
        if any(t['mode'] == 'corpse' for t in here):
            thresholds['explore_corpse'] = self.threshold(g, c, 'explore', None, corpse=True)
        return self.visible(g) | {'self': own, 'thresholds': thresholds, 'information': info, 'shared_information': [],
            'relationships': copy.deepcopy(g['relationships'][str(c['character_id'])]), 'sub_missions': missions,
            'statements': copy.deepcopy(p['ambiguous_statements']),
            'candidates': old_catalog()['candidates'],
            'occupants': [x for x in self.visible(g)['positions'] if x['id'] != c['character_id'] and x['room'] == c['room']],
            'allowed_actions': list(allowed), 'remaining_actions': int(bool(allowed)), 'lease_until': p['lease_until'],
            'final_feedback': copy.deepcopy(g.get('final_feedback')) if g['ended'] else None}
    def metrics(self, g):
        return {'questions': len(g['question_pairs']), 'transfers': g['transfer_count'],
            'indirect_links': sum(x.get('linked', False) for x in g['disclosures']),
            'fulfilled_trades': sum(x['status'] == 'settled' and all(x['fulfilled'].values()) for x in g['trades'].values()),
            'broken_trades': sum(x['status'] == 'settled' and not all(x['fulfilled'].values()) for x in g['trades'].values()),
            'relationship_updates': g.get('event_counts',{}).get('relationship_changed',0),
            'completed_missions': sum(x['achieved'] for p in g['players'].values() for x in p['missions']), 'gm_failovers': g['failovers']}

    def relationship_graph(self, g, owner=None):
        return {'directed': True, 'range': [0, 100], 'initial': 50, 'scope': 'game',
                'nodes': [{'id': c['character_id'], 'name': c['name']} for c in g['characters'].values()],
                'edges': [{'source': int(a), 'target': int(b), 'weight': weight}
                          for a, edges in g['relationships'].items() if owner is None or int(a) == owner
                          for b, weight in edges.items()]}
    def agent_map(self,g):
        nodes=[{'id':'character:'+str(c['character_id']),'type':'Character','mode':c['mode']} for c in g['characters'].values()]
        nodes += [{'id':'location:'+str(i),'type':'Location'} for i in range(8)]
        edges=[{'source':'character:'+str(c['character_id']),'relation':'locatedAt','target':'location:'+str(c['room'])} for c in g['characters'].values()]
        for a in g['assignments']:
            rid='role:'+a['assignment_id'];aid='agent:'+a['agent_id']
            if not any(n['id']==aid for n in nodes):nodes.append({'id':aid,'type':'Agent'})
            nodes.append({'id':rid,'type':'Role','role':a['role'],'status':a['status']})
            edges.append({'source':aid,'relation':'assignedTo','target':rid})
            if 'character_id' in a:edges.append({'source':rid,'relation':'represents','target':'character:'+str(a['character_id'])})
        return {'version':g['map_version'],'state_sequence':g.get('event_count',0),'nodes':nodes,'edges':edges}

    def rng(self, g, key): return random.Random(f'{g["roll_secret"]}:{key}')
    def threshold(self, g, c, approach, target, corpse=False):
        """[FIX-A2] Single source of truth for the dice threshold (skills, room, relation).
        roll() uses it, and player_view() shows the same numbers to the player."""
        stat = c['stats']['observe' if approach == 'explore' else 'speech' if approach in ('question', 'negotiate') else 'pressure']
        threshold = 8-stat
        cid = c['character_id']
        if approach == 'explore':
            threshold -= 1 if cid == 0 else 2 if (cid == 6 and corpse) or (cid == 7 and c['room'] == 5) or (cid == 8 and c['room'] == 7) or (cid == 9 and c['room'] == 3) else 0
        elif approach == 'question':
            threshold -= int(cid == 1 or (cid == 10 and target['mode'] == 'npc') or
                             (cid == 11 and any(x['actor'] == cid and x.get('target') == target['character_id'] and x['approach'] == 'question' for x in g['activity'])))
        elif approach == 'negotiate': threshold -= int(cid == 2)
        elif approach == 'interrogate': threshold -= int(cid == 4 or (cid == 10 and target['mode'] == 'npc'))
        elif approach == 'threaten': threshold -= int(cid == 5)
        if target and target['character_id'] == 3 and approach in ('interrogate', 'threaten'): threshold += 1
        if target and approach in ('question', 'negotiate') and g['rules']['relationship_effects']:
            relation = g['relationships'][str(cid)][str(target['character_id'])]
            threshold += -1 if relation >= 70 else 1 if relation <= 30 else 0
        return min(6, max(2, threshold))
    def roll(self, g, c, approach, target, key, corpse=False):
        threshold = self.threshold(g, c, approach, target, corpse)
        cid = c['character_id']
        prepared = c['prepared']
        c['prepared'] = False
        counter = g['roll_counts'].get(str(cid), 0)
        g['roll_counts'][str(cid)] = counter+1
        dice = [self.rng(g, f'roll:{cid}:{counter}:die:{i}').randint(1, 6) for i in range(3 if prepared else 2)]
        return {'dice': dice, 'threshold': threshold, 'successes': sum(x >= threshold for x in dice), 'prepared': prepared}
    def relation(self, g, owner, other, amount, reason):
        if owner == other: return
        c = g['characters'][str(owner)]
        delta = amount*(1.5 if c['mbti'][0] == 'E' else .5)
        old = g['relationships'][str(owner)][str(other)]
        new = min(100.0, max(0.0, old+delta))
        g['relationships'][str(owner)][str(other)] = new
        aid = c['controller']
        self.e.event(g, 'relationship_changed', {'owner': owner, 'target': other, 'before': old, 'after': new, 'reason': reason},
                     'private', (aid,) if aid else ())
        for p in g['players'].values():
            if p['character_id'] == other and new <= 30: p['relationship_below_30'] = True
    def award_missions(self, g):
        for aid, p in g['players'].items():
            if p['mode'] != 'active': continue
            for m in p['missions']:
                if not m['achieved'] and mission_complete(g, aid, m):
                    m.update(achieved=True, achieved_cycle=g['cycle_id'])
                    self.e.event(g, 'mission_achieved', {'agent_id': aid, 'mission_id': m['mission_id'],
                                 'reward': REWARDS[m['tier']]}, 'private', (aid,))
    def acquire(self, g, aid, info, source, sender=None):
        p, c = g['players'][aid], self.ch(g, g['players'][aid])
        before = infer(c['information'].values())
        existing = c['information'].get(info['id'])
        if existing and existing.get('kind') in ('definite', 'indirect'):
            if sender is not None:g['disclosures'].append({'clue_id':info['id'],'sender':sender,'recipient':aid,'kind':source,'cycle':g['cycle_id'],'linked':False})
            return False
        if existing and existing.get('kind') == 'narrated' and info.get('kind') == 'narrated':
            versions = existing.get('narrations', [existing['text']])
            if info['text'] in versions:
                if sender is not None:g['disclosures'].append({'clue_id':info['id'],'sender':sender,'recipient':aid,'kind':source,'cycle':g['cycle_id'],'linked':False})
                return False
            info = copy.deepcopy(info) | {'narrations': versions+[info['text']]}
        c['information'][info['id']] = copy.deepcopy(info)
        original = next(x for x in g['clues'] if x['id'] == info['id'])
        p['acquired'].append({'id': info['id'], 'cycle': g['cycle_id'], 'room': original['source_room']})
        if info.get('kind') == 'definite' and p['first_definite'] is None:
            p['first_definite'] = {'id': info['id'], 'cycle': g['cycle_id']}
        # Counterfactual link computation stays internal; narrated text is not silently unmasked.
        counterfactual = list(c['information'].values())+[public_clue(original)]
        after = infer(counterfactual)
        linked = original['kind'] == 'indirect' and any(set(after[k])-set(before[k]) for k in before)
        g['disclosures'].append({'clue_id': info['id'], 'sender': sender, 'recipient': aid,
             'cycle': g['cycle_id'], 'kind': source, 'linked': bool(linked)})
        if original['kind'] == 'indirect':
            self.e.event(g,'indirect_link_measurement',{'clue_id':info['id'],'recipient':aid,'linked':bool(linked),
                         'prior_support':before if linked else None},'research')
        return True
    def action(self, db, g, d):
        p = self.player(g, d, read=True)
        require(p['mode'] == 'active', 'unauthorized', 'dead player cannot act')
        r = d.get('request')
        require(isinstance(r, dict), 'invalid_input', 'action request must be an object')
        require(set(r) <= {'schema_version','game_id','agent_id','action_id','cycle_id','phase','phase_epoch','action','payload','task_id','generation'}, 'invalid_input', 'unknown action request field')
        require(r.get('schema_version', VERSION) == VERSION, 'unsupported_version', 'unknown action schema')
        require(r.get('agent_id') == p['agent_id'] and r.get('game_id') == g['id'], 'unauthorized', 'action identity mismatch')
        action_id = identifier(r.get('action_id'), 'action_id')
        fp = fingerprint(r)
        old = g['receipts'].get(action_id) or self.record(db,g,'action',action_id)
        if old:
            require(old['fingerprint'] == fp, 'conflict', 'action_id reused')
            return copy.deepcopy(old['result'])
        self.player(g, d)
        require(not r.get('task_id') and 'generation' not in r, 'invalid_input', 'competitive actions have no action task')
        require(r.get('phase') == g['phase'] and all(type(r.get(k)) is int and r[k] == g[k] for k in ('cycle_id', 'phase_epoch')),
                'conflict', 'stale phase')
        aid, cid = p['agent_id'], p['character_id']
        require(aid not in g['spent'], 'conflict', 'one action per phase')
        act, payload = r.get('action'), r.get('payload', {})
        require(act in LEGAL[g['phase']] and isinstance(payload, dict), 'invalid_input', 'illegal action')
        c = self.ch(g, p)
        result = {k: r[k] for k in ('game_id', 'agent_id', 'action_id', 'cycle_id', 'phase', 'phase_epoch', 'action')}
        result.update(schema_version=VERSION, ruleset=SPEC, execution_status='completed', consumed=True,
                      outcome='completed', acquired_information=[], roll=None, room=c['room'], actor_mode=p['mode'])
        target, approach, roll = None, None, None
        if act == 'move':
            require(set(payload) == {'room'}, 'invalid_input', 'move requires room')
            c['room'] = integer(payload['room'], 'room', 0, 7)
            p['visited'] = sorted(set(p['visited']) | {c['room']})
            result['room'] = c['room']
        elif act == 'prepare':
            require(not payload, 'invalid_input', 'prepare has no payload')
            c['prepared'] = True
        elif act == 'explore':
            require(set(payload) <= {'target_id'}, 'invalid_input', 'explore takes optional corpse target_id')
            target_id = payload.get('target_id')
            if target_id is not None:
                integer(target_id, 'corpse target', 0, 11)
                target = g['characters'][str(target_id)]
                require(target['mode'] == 'corpse' and target['room'] == c['room'], 'invalid_input', 'corpse must be in the same room')
            approach = 'explore'
            roll = self.roll(g, c, approach, target, action_id, corpse=target is not None)
            known = c['information']
            source = list(target['information'].values()) if target else [public_clue(x) for x in g['clues'] if x['source_room'] == c['room']]
            options = [x for x in source if x['id'] not in known or known[x['id']].get('kind') in ('possession', 'narrated')]
            self.rng(g, action_id+':loot').shuffle(options)
            count = 2 if roll['successes'] == 3 else int(roll['successes'] > 0)
            result['acquired_information'] = options[:count]
            for info in result['acquired_information']: self.acquire(g, aid, info, 'explore')
            result.update(outcome='acquired' if result['acquired_information'] else 'no_information',
                          cost_incurred=roll['successes'] == 1)
        elif act == 'question':
            require(set(payload) <= {'target_id', 'approach', 'hypothesis'}, 'invalid_input', 'question requires hypothesis/approach/target_id')
            target_id = integer(payload.get('target_id'), 'target_id', 0, 11)
            target = g['characters'][str(target_id)]
            require(target_id != cid and target['room'] == c['room'] and target['mode'] != 'corpse', 'invalid_input', 'target must be a living character in the same room')
            approach = payload.get('approach', 'question')
            require(approach in ('question', 'interrogate', 'threaten', 'negotiate'), 'invalid_input', 'unknown question approach')
            hypothesis = payload.get('hypothesis')
            require(isinstance(hypothesis, dict) and set(hypothesis) == {'suspect', 'weapon', 'place'}, 'invalid_input', 'three-candidate hypothesis required')
            for k, size in (('suspect', 12), ('weapon', 8), ('place', 8)): integer(hypothesis[k], k, 0, size-1)
            qid = 'question:'+str(uuid.uuid4())
            blocked = target.get('blocked', {}).get(str(cid), 0) >= g['cycle_id']
            roll = self.roll(g, c, approach, target, action_id)
            if blocked: roll['successes'] = 0
            q = {'question_id': qid, 'action_id': action_id, 'asker_id': aid, 'asker_character': cid,
                 'target_id': target_id, 'target_agent': target['controller'], 'hypothesis': copy.deepcopy(hypothesis),
                 'approach': approach, 'successes': roll['successes'], 'status': 'pending',
                 'deadline': self.e.clock()+g['rules']['question_timeout'], 'response_id': None,
                 'response_fingerprint': None, 'transferred_ids': []}
            g['questions'][qid] = q
            g['question_pairs'].append(self.pair(q))
            result.update(execution_status='pending', outcome='awaiting_response', question_id=qid)
            self.e.event(g, 'question_pair', self.pair(q))
            if approach == 'interrogate': self.relation(g, target_id, cid, -5, 'interrogation')
            if approach == 'threaten': self.relation(g, target_id, cid, -10, 'threat')
            if roll['successes'] == 0:
                if approach in ('question', 'negotiate'): self.relation(g, target_id, cid, -5, 'failed_question')
                if approach == 'interrogate': target.setdefault('blocked', {})[str(cid)] = g['cycle_id']+1
                if approach == 'threaten' and c['information']:
                    info = self.rng(g, action_id+':exposure').choice(list(c['information'].values()))
                    target['information'].setdefault(info['id'], possession(info))
                    g['disclosures'].append({'clue_id': info['id'], 'sender': aid, 'recipient': target['controller'], 'kind': 'possession', 'cycle': g['cycle_id']})
            elif approach in ('question', 'negotiate'): self.relation(g, target_id, cid, 5, 'successful_question')
        elif act == 'accuse':
            require(set(payload) <= {'suspect', 'weapon', 'place', 'output_tokens', 'elapsed_seconds', 'confidence'}, 'invalid_input', 'unknown deduction field')
            guess = {k: integer(payload.get(k), k, 0, size-1) for k, size in (('suspect', 12), ('weapon', 8), ('place', 8))}
            tokens = integer(payload.get('output_tokens'), 'output_tokens', 0, 10000000)
            elapsed = payload.get('elapsed_seconds', 0)
            require(type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0, 'invalid_input', 'invalid elapsed time')
            g['deductions'][aid] = {'guess': guess, 'output_tokens': tokens, 'elapsed_seconds': elapsed, 'action_id': action_id}
            self.e.event(g, 'deduction_submission', {'agent_id': aid, 'guess': guess, 'output_tokens': tokens,
                         'elapsed_seconds': elapsed}, 'private', (aid,))
            result.update(execution_status='pending', outcome='awaiting_adjudication')
        else:
            require(not payload, 'invalid_input', 'stay/pass has no payload')
        result['roll'] = roll
        g['spent'][aid] = action_id
        p['lease_until'] = self.e.clock()+g['rules']['player_lease']
        g['receipts'][action_id] = {'fingerprint': fp, 'result': result}
        if act != 'accuse':
            g['activity'].append({'actor': cid, 'agent_id': aid, 'kind': act, 'room': c['room'],
                'target': target['character_id'] if target else None, 'target_mode': target['mode'] if target else None,
                'approach': approach, 'successes': roll['successes'] if roll else 0, 'prepared': roll['prepared'] if roll else False,
                'cycle': g['cycle_id'], 'alone_players': not any(x['controller'] and x['mode'] == 'active' and x['character_id'] != cid and x['room'] == c['room'] for x in g['characters'].values())})
        if act == 'question':
            q = g['questions'][result['question_id']]
            if q['successes'] == 0 or target['mode'] == 'npc' or not target['information']:
                self.complete_question(db, g, q, self.default_response(g, q), mandatory=True)
        elif act != 'accuse': self.complete_action(db, g, result)
        self.award_missions(g)
        return copy.deepcopy(g['receipts'][action_id]['result'])
    def complete_action(self, db, g, result):
        aid = result['agent_id']
        result['held_information_ids']=list(self.ch(g,g['players'][aid])['information'])
        e = self.e.event(g, 'action_completed', {'result': copy.deepcopy(result)}, 'private', (aid,))
        self.e.experience(db, g, aid, e['event_id'], 'player', result['outcome'], result['outcome'] in ('acquired', 'winner'))
    def options(self, g, q):
        c = g['characters'][str(q['target_id'])]
        full = [x for x in c['information'].values() if x.get('kind') == 'definite']
        ambiguous = []
        owned_full = [x for x in c['information'].values() if x.get('kind') == 'definite']
        for cat, candidate in q['hypothesis'].items():
            if owned_full and all(not (x['category'] == cat and x['candidate'] == candidate) for x in owned_full):
                ambiguous.append({'kind': 'ambiguous', 'category': cat, 'candidate': candidate,
                                  'text': f'내가 가진 단서는 {cat} 후보 {candidate}에 관한 것이 아니다.'})
        indirect = []
        for clue in c['information'].values():
            if clue.get('kind') == 'indirect':
                indirect.append({'kind': 'indirect', 'clue_id': clue['id'], 'narrative_options': narrative_options(clue)})
            elif clue.get('kind') == 'narrated':
                indirect.append({'kind':'indirect','clue_id':clue['id'],'narrative_options':clue.get('narrations',[clue['text']])})
        return {'definite': [x['id'] for x in full], 'ambiguous': ambiguous, 'indirect': indirect}
    def default_response(self, g, q):
        opts = self.options(g, q)
        c = g['characters'][str(q['target_id'])]
        probability = .5
        if c['mbti'][2] == 'T': probability += {'negotiate': .2, 'question': 0, 'interrogate': -.1, 'threaten': -.2}[q['approach']]
        else:
            r = g['relationships'][str(q['target_id'])][str(q['asker_character'])]
            probability += .2 if r >= 70 else -.2 if r <= 30 else 0
        definite = opts['definite'][:1]
        vague = [dict(x) for x in opts['ambiguous']]
        for x in opts['indirect']:
            vague.append({'kind': 'indirect', 'clue_id': x['clue_id'], 'text': x['narrative_options'][0]})
        n = q['successes']
        use_full = bool(definite) and (n == 3 or (n == 2 and self.rng(g, q['question_id']+':form').random() < probability))
        statements = ([{'kind': 'definite', 'clue_id': definite[0]}] if use_full else [])
        need = 0 if n == 0 else 1 if n == 1 else 0 if n == 2 and use_full else 2
        statements += vague[:need]
        if n == 2 and not use_full and len(vague) < 2 and definite:
            statements = [{'kind': 'definite', 'clue_id': definite[0]}]
        return statements
    def validate_response(self, g, q, statements):
        require(isinstance(statements, list) and len(statements) <= 3 and all(isinstance(x, dict) for x in statements), 'invalid_input', 'response is a statement list')
        opts = self.options(g, q)
        known = g['characters'][str(q['target_id'])]['information']
        seen, full, vague, safe = set(), 0, 0, []
        for s in statements:
            require(set(s) <= {'kind', 'clue_id', 'category', 'candidate', 'text'}, 'invalid_input', 'unknown statement field')
            key = (s.get('kind'), s.get('clue_id'), s.get('category'), s.get('candidate'))
            require(key not in seen, 'invalid_input', 'duplicate statement')
            seen.add(key)
            if s.get('kind') == 'definite':
                require(s.get('clue_id') in opts['definite'], 'untruthful', 'definite clue must be owned and fully known')
                full += 1
                safe.append(public_clue(known[s['clue_id']]))
            elif s.get('kind') == 'ambiguous':
                match = next((x for x in opts['ambiguous'] if x['category'] == s.get('category') and x['candidate'] == s.get('candidate')), None)
                require(match is not None and s.get('text', match['text']) == match['text'], 'untruthful', 'ambiguous claim is not entailed by owned evidence')
                vague += 1
                safe.append(copy.deepcopy(match))
            elif s.get('kind') == 'indirect':
                clue = known.get(s.get('clue_id'), {})
                allowed = narrative_options(clue) if clue.get('kind') == 'indirect' else clue.get('narrations',[clue.get('text')]) if clue.get('kind') == 'narrated' else []
                require(s.get('text') in allowed and isinstance(s.get('text'),str), 'untruthful', 'narration is not in the truth-preserving grammar')
                vague += 1
                safe.append({'kind': 'narrated', 'id': clue['id'], 'text': s['text']})
            else: raise ContractError('invalid_input', 'unknown statement kind')
        n = q['successes']
        # If evidence makes a required form impossible, disclose all available allowed slots.
        max_vague = min(2, len(opts['ambiguous'])+len(opts['indirect']))
        if n == 0: valid = full == vague == 0
        elif n == 1: valid = (full == 0 and vague == min(1, max_vague)) or (max_vague == 0 and full == 1 and vague == 0)
        elif n == 2:
            valid = (full == 1 and vague == 0 and bool(opts['definite'])) or (full == 0 and vague == max_vague and (max_vague > 0 or not opts['definite']))
        else: valid = full == int(bool(opts['definite'])) and vague == max_vague
        require(valid, 'disclosure_required', 'response does not meet success-tier disclosure requirement')
        return safe
    def complete_question(self, db, g, q, statements, mandatory=False):
        safe = self.validate_response(g, q, statements)
        target = g['characters'][str(q['target_id'])]
        npc = target['mode'] == 'npc'
        result = g['receipts'][q['action_id']]['result']
        acquired, descriptions = [], []
        for info in safe:
            if info['kind'] == 'ambiguous':
                descriptions.append(info)
                g['players'][q['asker_id']]['ambiguous_statements'].append(copy.deepcopy(info)|{'question_id':q['question_id']})
                continue
            # NPC statements are full original indirect facts, player narration stays redacted.
            if info['kind'] == 'narrated' and npc: info = public_clue(target['information'][info['id']])
            self.acquire(g, q['asker_id'], info, 'response_definite' if info['kind'] == 'definite' else 'response_indirect', q['target_agent'])
            acquired.append(info)
            q['transferred_ids'].append(info['id'])
        q['status'] = 'completed'
        q['mandatory_fallback'] = mandatory
        g['transfer_count'] += int(bool(acquired))
        g['recent_questions'] = (g['recent_questions']+[copy.deepcopy(q)])[-12:]
        result.update(execution_status='completed', outcome='acquired' if acquired or descriptions else 'no_information',
                      acquired_information=acquired, statements=descriptions)
        self.complete_action(db, g, result)
        self.e.event(g, 'question_supervision', self.pair(q) | {'successes': q['successes'], 'approach': q['approach'], 'transferred_ids': q['transferred_ids']}, 'supervisor')
        self.award_missions(g)
    def respond(self, db, g, d):
        p = self.player(g, d)
        q = g['questions'].get(d.get('question_id')) or self.record(db,g,'question',d.get('question_id'))
        require(q and q['target_agent'] == p['agent_id'], 'unauthorized', 'only the addressed player may respond')
        rid = identifier(d.get('response_id'), 'response_id')
        fp = fingerprint(d)
        if q['response_id'] is not None:
            require(q['response_id'] == rid and q['response_fingerprint'] == fp, 'conflict', 'response already committed')
            return {'question_id': q['question_id'], 'committed': True}
        require(q['status'] == 'pending' and self.e.clock() < q['deadline'], 'expired', 'response deadline passed')
        self.complete_question(db, g, q, d.get('statements'))
        q.update(response_id=rid, response_fingerprint=fp)
        p['lease_until'] = self.e.clock()+g['rules']['player_lease']
        return {'question_id': q['question_id'], 'committed': True}

    def adjudicate(self, db, g):
        correct = [(aid, x) for aid, x in g['deductions'].items() if x['guess'] == g['truth']]
        tokens = sorted(x['output_tokens'] for _, x in correct)
        verdicts = []
        for aid, choice in g['deductions'].items():
            p, won = g['players'][aid], choice['guess'] == g['truth']
            result = g['receipts'][choice['action_id']]['result']
            if won:
                rank = 1+sum(n < choice['output_tokens'] for n in tokens)
                p['rank'] = rank
                verdicts.append({'agent_id': aid, 'character_id': p['character_id'], 'winner': True,
                                 'rank': rank, 'guess': choice['guess']})
                result.update(execution_status='completed', outcome='winner', rank=rank)
            else:
                p['mode'] = 'corpse'
                for a in g['assignments']:
                    if a['assignment_id']==p['assignment_id']:a['status']='corpse'
                c = self.ch(g, p)
                c.update(mode='corpse', controller=None, goal=None)
                verdicts.append({'agent_id': aid, 'character_id': p['character_id'], 'winner': False, 'mode': 'corpse'})
                result.update(execution_status='completed', outcome='dead', actor_mode='corpse')
                self.e.event(g, 'actor_died', {'character_id': p['character_id']})
            self.complete_action(db, g, result)
        if verdicts:
            ref = 'deduction:'+str(g['phase_epoch'])
            g['announcements'][ref] = {'kind': 'deduction_result', 'data': {'ref': ref, 'kind': 'deduction_result',
                'cycle_id': g['cycle_id'], 'verdicts': verdicts}, 'published': False}
            self.e.event(g, 'deduction_ready', {'ref': ref, 'verdicts': verdicts}, 'supervisor')
        g['deductions'] = {}
        if correct: g['resolution'] = 'winner'
        elif not any(p['mode'] == 'active' for p in g['players'].values()): g['resolution'] = 'all_dead'

    def advance(self, db, g, d):
        require(type(d.get('phase_epoch')) is int and d['phase_epoch'] == g['phase_epoch'], 'conflict', 'stale phase epoch')
        active = [aid for aid, p in g['players'].items() if p['mode'] == 'active']
        require(active and all(aid in g['spent'] for aid in active) and
                not any(q['status'] == 'pending' for q in g['questions'].values()), 'pending', 'all actions and replies must finish')
        if g['phase'] == 'deduction':
            self.adjudicate(db, g)
            alive = [str(p['character_id']) for p in g['players'].values() if p['mode'] == 'active']
            g['cycle_history'].append({'cycle': g['cycle_id'], 'rooms': {i: c['room'] for i, c in g['characters'].items()}, 'alive': alive})
            self.award_missions(g)
            if g['resolution'] is None and g['cycle_id'] >= g['rules']['deadline_cycles']:
                g['resolution'] = 'deadline'
            if g['resolution'] is not None:
                self.e.event(g, 'resolution_ready', {'resolution': g['resolution']}, 'supervisor')
                return self.visible(g)
            g['cycle_id'] += 1
            for c in g['characters'].values():
                if c['mode'] == 'corpse' and c['information']:
                    cid = self.rng(g, f'decay:{c["character_id"]}:{g["cycle_id"]}').choice(sorted(c['information']))
                    del c['information'][cid]
            remaining = g['rules']['deadline_cycles']-g['cycle_id']+1
            if remaining in (30, 10):
                ref = 'deadline:'+str(remaining)
                g['announcements'][ref] = {'kind': 'deadline_warning', 'data': {'ref': ref, 'kind': 'deadline_warning', 'remaining_cycles': remaining}, 'published': False}
            g['phase'] = 'movement'
        elif g['phase'] == 'movement':
            # The simultaneous movement boundary also moves NPCs, including staying.
            for c in g['characters'].values():
                if c['mode'] == 'npc': c['room'] = self.rng(g, f'npc-move:{g["cycle_id"]}:{c["character_id"]}').randrange(8)
            g['phase'] = 'interaction'
        else: g['phase'] = 'deduction'
        g['phase_epoch'] += 1
        g['spent'] = {}
        self.e.event(g, 'phase_advanced', {'phase': g['phase'], 'phase_epoch': g['phase_epoch'], 'cycle_id': g['cycle_id']})
        return self.visible(g)
    def finish(self, db, g, reason=None):
        require(not g['ended'], 'conflict', 'already ended')
        reason = reason or g['resolution']
        require(reason in ('winner', 'all_dead', 'deadline', 'time_out', 'no_players', 'operator_stopped'), 'conflict', 'no official terminal resolution')
        self.settle_trades(db, g, force=True)
        feedback = {}
        pool = self.pool(db, g['pool_id'])
        for aid, p in g['players'].items():
            rank = p.get('rank')
            unsolved = g['rules'].get('unsolved_score', 0) if reason in ('deadline', 'time_out') else 0
            base = WIN_SCORES[min(rank, 4)-1] if rank else WRONG_ACCUSE_SCORE if p['mode'] == 'corpse' else unsolved
            score = base+sum(REWARDS[m['tier']] for m in p['missions'] if m['achieved'])
            a = self.e.load(db, 'agents', aid)
            record = {'game_id': g['id'], 'character_id': p['character_id'], 'job_id': p['character_id'],
                      'final_score': score, 'participation_index': len(a['history'])+1,
                      'sub_missions': copy.deepcopy(p['missions']), 'rank': rank,
                      'survived': p['mode'] != 'corpse', 'outcome': 'loss' if reason == 'time_out' else 'win' if rank else 'loss', 'mbti': a['fixed_mbti']}
            history_record = copy.deepcopy(record)
            history_record['sub_missions'] = [{k: m[k] for k in ('mission_id', 'tier', 'achieved', 'achieved_cycle')}
                                              for m in p['missions']]
            a['history'].append(history_record)
            self.e.put(db, 'agents', aid, a)
            feedback[aid] = {k: v for k, v in record.items() if k != 'mbti'}
            for m in feedback[aid]['sub_missions']: m['targets'].pop('target_ei', None)
        g.update(ended=True, status='ended', resolution=reason, finished_at=self.e.clock(), final_feedback=feedback)
        for a in g['assignments']:
            if a['status']=='active':a['status']='completed'
        for aid in pool['agent_ids']:
            a = self.e.load(db, 'agents', aid)
            if a['busy_game_id'] == g['id']:
                a['busy_game_id'] = None
                if a['pool_status'] == 'active': a['pool_status'] = 'available'
                self.e.put(db, 'agents', aid, a)
        self.e.event(g, 'game_finished', {'reason': reason, 'feedback': feedback})
    def fail_player(self, db, g, aid, reason):
        p = g['players'][aid]
        if p['mode'] != 'active': return
        p.update(mode='npc', token='')
        for a in g['assignments']:
            if a['assignment_id']==p['assignment_id']:a['status']='npc'
        c = self.ch(g, p)
        c.update(mode='npc', controller=None, goal=None)
        for q in g['questions'].values():
            if q['status'] == 'pending' and q['target_agent'] == aid:
                self.complete_question(db, g, q, self.default_response(g, q), mandatory=True)
            elif q['status'] == 'pending' and q['asker_id'] == aid:
                q['status'] = 'cancelled'
                g['receipts'][q['action_id']]['result'].update(execution_status='completed', outcome='no_information')
        a = self.e.load(db, 'agents', aid)
        a['pool_status'] = 'fault'
        self.e.put(db, 'agents', aid, a)
        g['technical_failure'] = g['technical_failure'] or reason != 'promoted_to_gm'
        g['map_version'] += 1
        self.e.event(g, 'player_fault', {'agent_id': aid, 'character_id': p['character_id'], 'reason': reason})
        if g['resolution'] is None and not any(p['mode'] == 'active' for p in g['players'].values()): g['resolution'] = 'no_players'
    def failover(self, db, g, reason, chosen=None):
        old = g['controller']
        pool = self.pool(db, g['pool_id'])
        if old:
            a = self.e.load(db, 'agents', old['controller_id'])
            a['pool_status'] = 'fault'
            a['pool_role'] = 'retired_gm'
            self.e.put(db, 'agents', a['agent_id'], a)
            for assignment in g['assignments']:
                if assignment['role'] == 'gm' and assignment['term'] == old['term']: assignment['status'] = 'retired'
            self.e.event(g, 'gm_retired', {'controller_id': old['controller_id'], 'term': old['term'], 'reason': reason})
        g['controller'] = None
        g['failovers'] += 1
        g['technical_failure'] = True
        active = sorted(aid for aid, p in g['players'].items() if p['mode'] == 'active' and self.e.clock() < p['lease_until'])
        reserve = [a['agent_id'] for a in self.cards(db, pool) if a['pool_role'] == 'player' and a['pool_status'] == 'available']
        options = active or sorted(reserve)
        if chosen is not None:
            require(chosen in active or chosen in reserve, 'invalid_input', 'ineligible successor')
            options = [chosen]
        if not options:
            g['status'] = 'waiting_for_human'
            self.e.event(g, 'human_intervention_required', {'choices': ['FINISH'], 'reason': 'no_eligible_GM'})
            return
        aid = options[0]
        if aid in active: self.fail_player(db, g, aid, 'promoted_to_gm')
        else: self.e.reserve(db, aid, g['id'])
        a = self.e.load(db, 'agents', aid)
        a.update(pool_status='active', pool_role='gm')
        self.e.put(db, 'agents', aid, a)
        pool['current_gm_id'] = aid
        self.save_pool(db, pool)
        self.e.controller(g, aid)
        g['status'] = 'running' if g['started'] else 'awaiting_setup'
        self.e.event(g, 'gm_succeeded', {'controller_id': aid, 'term': g['term'], 'context_source': 'engine-only'})
    def gm_command(self, db, g, d):
        c = self.e.gm(g, d, allow_ended=True)
        cid = identifier(d.get('command_id'), 'command_id')
        fp = fingerprint(d)
        old = g['commands'].get(cid) or self.record(db,g,'command',cid)
        if old:
            require(old['fingerprint'] == fp, 'conflict', 'command_id reused')
            return copy.deepcopy(old['result'])
        op = d.get('operation')
        require(not g['ended'] or op == 'REPORT', 'not_running', 'game ended')
        if op == 'PLAN': result = self.plan(db, g, d)
        elif op == 'ADVANCE':
            require(g['resolution'] is None, 'not_running', 'terminal resolution already ready')
            result = self.advance(db, g, d)
        elif op == 'FINISH':
            self.finish(db, g)
            result = self.visible(g)
        elif op == 'ANNOUNCE':
            ref = d.get('ref')
            a = g['announcements'].get(ref)
            require(a is not None and a['kind'] == d.get('kind'), 'invalid_input', 'reference official announcement data')
            require('data' not in d and 'text' not in d, 'invalid_input', 'GM cannot rewrite verdicts')
            if not a['published']:
                a['published'] = True
                self.e.event(g, 'announcement', a['data'])
            result = copy.deepcopy(a['data'])
        elif op == 'REPORT':
            require(g['ended'] and g['gm_report'] is None, 'conflict', 'one report after finalization')
            m = d.get('metrics')
            require(isinstance(m, dict) and set(m) in ({'completion', 'balance', 'exploration', 'record_completeness'},
                    {'completion', 'balance', 'exploration', 'record'}), 'invalid_input', 'C4 metrics required')
            m = {('record_completeness' if k == 'record' else k): v for k, v in m.items()}
            require(g['resolution'] in ('winner','all_dead','deadline','time_out') or m['completion'] == 0,
                    'invalid_input', 'non-legitimate ending cannot claim progress completion')
            for key, value in m.items():
                require((key == 'balance' and value is None) or (type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1), 'invalid_input', 'metric outside 0..1')
            evidence = d.get('evidence')
            require(isinstance(evidence, list) and bool(evidence) and all(x in {e['event_id'] for e in self.all_events(db,g)} for x in evidence), 'invalid_input', 'official evidence event IDs required')
            weights = {'completion': .35, 'balance': .25, 'exploration': .2, 'record_completeness': .2}
            denominator = sum(weights[k] for k in m if m[k] is not None)
            score = sum(weights[k]*v for k, v in m.items() if v is not None)/denominator
            g['gm_report'] = {'metrics': copy.deepcopy(m), 'evidence': evidence, 'score': score,
                'technical_failure': g['technical_failure'], 'success': score >= .7 and not g['technical_failure']}
            member=self.e.load(db,'agents',c['controller_id'])
            member.setdefault('gm_reports',[]).append({'game_id':g['id'],'score':score,'metrics':copy.deepcopy(m),
                'success':g['gm_report']['success'],'technical_failure':g['technical_failure']})
            self.e.put(db,'agents',c['controller_id'],member)
            event = self.e.event(g, 'gm_reported', {'score': score, 'success': g['gm_report']['success']}, 'supervisor')
            self.e.experience(db, g, c['controller_id'], event['event_id'], 'gm', 'performance_report', g['gm_report']['success'])
            result = g['gm_report']
        else: raise ContractError('invalid_input', 'unknown competitive GM operation')
        if not g['ended']: c['lease_until'] = self.e.clock()+g['rules']['gm_lease']
        g['commands'][cid] = {'fingerprint': fp, 'result': copy.deepcopy(result)}
        return result
    def operator(self, db, g, d):
        self.e.operator(d)
        require(not g['ended'], 'conflict', 'already ended')
        op = d.get('operation')
        if op == 'PAUSE':
            require(g['status'] != 'paused', 'conflict', 'already paused')
            g['paused_at'] = self.e.clock()
            g['status'] = 'paused'
        elif op == 'RESUME':
            require(g['status'] == 'paused' and g['controller'], 'conflict', 'no paused controller')
            delta = self.e.clock()-g.pop('paused_at')
            g['controller']['lease_until'] = self.e.clock()+g['rules']['gm_lease']
            for p in g['players'].values(): p['lease_until'] = self.e.clock()+g['rules']['player_lease']
            for x in list(g['questions'].values())+list(g['trades'].values()): x['deadline'] += delta
            g['status'] = 'running' if g['started'] else 'awaiting_setup'
        elif op == 'FAIL_PLAYER':
            require(d.get('agent_id') in g['players'], 'not_found', 'player not found')
            self.fail_player(db, g, d['agent_id'], 'injected_failure')
        elif op == 'FAIL_GM':
            require(g['controller'] and type(d.get('term')) is int and d['term'] == g['term'], 'stale_controller', 'current term required')
            self.failover(db, g, 'injected_failure')
        elif op == 'APPOINT_GM': self.failover(db, g, 'operator_appointment', d.get('agent_id'))
        elif op == 'TIMEOUT':
            # Prepare an official terminal result; the GM publishes and finishes it.
            if g['resolution'] is None:
                g['resolution'] = 'time_out'
                g['timeout_at'] = self.e.clock()
                ref = 'time_out'
                g['announcements'][ref] = {'kind':'time_out', 'published':False,
                    'data':{'ref':ref, 'kind':'time_out', 'resolution':'time_out',
                            'winners':[], 'losers':list(g['players']),
                            'text':'제한 시간이 종료되었습니다. 사건을 해결하지 못해 전원 패배합니다.'}}
                self.e.event(g, 'resolution_ready', {'resolution':'time_out'}, 'supervisor')
        elif op == 'FINISH': self.finish(db, g, g['resolution'] or 'operator_stopped')
        else: raise ContractError('invalid_input', 'unknown operator command')
        self.e.event(g, 'operator_action', {'operation': op})
        return self.e.credentials(g) | {'state': self.gm_view(db, g)}
    def sweep(self, db, g):
        if g['ended'] or g['status'] == 'paused': return
        if g['resolution'] is not None:
            if g['controller'] and self.e.clock() >= g['controller']['lease_until']: self.failover(db,g,'lease_expired')
            return
        for q in g['questions'].values():
            if q['status'] == 'pending' and self.e.clock() >= q['deadline']:
                self.complete_question(db, g, q, self.default_response(g, q), mandatory=True)
        self.settle_trades(db, g)
        for aid, p in g['players'].items():
            if p['mode'] == 'active' and self.e.clock() >= p['lease_until']: self.fail_player(db, g, aid, 'lease_expired')
        if g['controller'] and self.e.clock() >= g['controller']['lease_until']: self.failover(db, g, 'lease_expired')

    def trade(self, db, g, d, operation):
        p = self.player(g, d)
        aid = p['agent_id']
        if operation == 'propose':
            require(g['phase'] == 'interaction', 'invalid_input', 'negotiate in interaction phase')
            require(not any(t['proposer'] == aid and t['epoch'] == g['phase_epoch'] for t in g['trades'].values()), 'conflict', 'one proposal per phase')
            other = d.get('recipient_agent_id')
            npc = None
            if other is None and type(d.get('target_id')) is int:
                target_cid = integer(d['target_id'], 'target_id', 0, 11)
                other = g['characters'][str(target_cid)]['controller']
            if other not in g['players']:
                target_cid = integer(d.get('target_id'), 'NPC target_id', 0, 11)
                npc = g['characters'][str(target_cid)]
                require(npc['mode'] == 'npc', 'invalid_input', 'target is not NPC')
                other = 'npc:'+str(target_cid)
            own = self.ch(g, p)
            target = npc or self.ch(g, g['players'][other])
            require(target['room'] == own['room'] and other != aid and target['mode'] != 'corpse', 'invalid_input', 'trade with a living peer in the same room')
            offered, offer_category = self.offer(own,d)
            category = d.get('requested_category')
            hypothesis=d.get('requested_hypothesis')
            require(category in ('suspect', 'weapon', 'place', 'indirect') or isinstance(hypothesis,dict), 'invalid_input', 'request a category or hypothesis')
            if hypothesis is not None:
                require(isinstance(hypothesis,dict) and set(hypothesis)=={'suspect','weapon','place'},'invalid_input','invalid trade hypothesis')
                for k,limit in (('suspect',12),('weapon',8),('place',8)):integer(hypothesis[k],k,0,limit-1)
            tid = identifier(d.get('trade_id'), 'trade_id')
            require(tid not in g['trades'], 'conflict', 'trade ID already exists')
            accepted = self.rng(g, tid+':accept').random() < (.4 if npc and npc['mbti'][3] == 'J' else .7) if npc else False
            t = {'trade_id': tid, 'proposer': aid, 'recipient': other, 'target_character': target['character_id'],
                 'offers': {aid: offered}, 'offer_categories':{aid:offer_category}, 'requested_category': category,
                 'requested_hypothesis':hypothesis, 'status': 'accepted' if accepted else 'refused' if npc else 'proposed',
                 'accepted': accepted, 'fulfilled': {aid: False, other: False}, 'first_sender': None,
                 'deadline': self.e.clock()+g['rules']['trade_timeout'], 'cycle': g['cycle_id'], 'epoch': g['phase_epoch']}
            g['trades'][tid] = t
            g['disclosures'].append({'clue_id': offered[0] if offered else None, 'sender': aid, 'recipient': other, 'kind': 'possession', 'cycle': g['cycle_id']})
            if accepted:
                matches = [x['id'] for x in target['information'].values() if self.matches_trade(x,t)]
                t['offers'][other] = matches[:1]
                t['offer_categories'][other]=category
                if not matches: t.update(accepted=False, status='refused')
            self.e.event(g, 'trade_proposed', {'trade_id': tid, 'proposer': aid, 'recipient': other}, 'private', (aid, other))
            return self.trade_view(g, t)
        t = g['trades'].get(d.get('trade_id'))
        require(t is not None and aid in (t['proposer'], t['recipient']), 'unauthorized', 'trade participant required')
        require(self.e.clock() < t['deadline'] and t['status'] not in ('settled', 'refused'), 'expired', 'trade closed')
        if operation == 'respond':
            require(aid == t['recipient'] and t['status'] == 'proposed', 'conflict', 'recipient must answer proposal')
            decision = d.get('decision')
            require(decision in ('accept', 'refuse', 'counter'), 'invalid_input', 'unknown trade response')
            if decision == 'refuse': t['status'] = 'refused'
            else:
                own = self.ch(g, p)
                offers,offer_category=self.offer(own,d)
                offered_options=[own['information'][x] for x in offers] if offers else [x for x in own['information'].values() if x.get('category','indirect')==offer_category]
                require(any(self.matches_trade(x,t) for x in offered_options),'invalid_input','offer must match requested category/hypothesis')
                t['offers'][aid] = offers
                t['offer_categories'][aid]=offer_category
                t.update(status='proposed' if decision == 'counter' else 'accepted', accepted=decision == 'accept')
                if decision == 'counter':
                    original_ids=t['offers'][t['proposer']]
                    original = self.ch(g, g['players'][t['proposer']])['information'][original_ids[0]] if original_ids else {}
                    t['requested_category'] = original.get('category', t['offer_categories'][t['proposer']])
                    t['requested_hypothesis']=None
                    t['proposer'], t['recipient'] = t['recipient'], t['proposer']
            return self.trade_view(g, t)
        require(t['status'] == 'accepted', 'conflict', 'trade must be accepted')
        require(not t['fulfilled'][aid], 'conflict', 'already transferred')
        own = self.ch(g, p)
        recipient = t['recipient'] if aid == t['proposer'] else t['proposer']
        committed=t['offers'][aid]
        offered = d.get('information_id',committed[0] if committed else None)
        require(offered in own['information'], 'unauthorized', 'offered clue no longer owned')
        require((offered in committed if committed else own['information'][offered].get('category','indirect')==t['offer_categories'][aid]) and
                own['information'][offered].get('kind') in ('definite','indirect'), 'unauthorized','transfer must honor the promised ID/category')
        if aid==t['recipient']:require(self.matches_trade(own['information'][offered],t),'invalid_input','transfer does not match requested information')
        info = public_clue(own['information'][offered])
        if recipient not in g['players']:
            nc = g['characters'][recipient.split(':', 1)[1]]
            nc['information'][info['id']] = info
        else: self.acquire(g, recipient, info, 'trade', aid)
        t['fulfilled'][aid] = True
        t['first_sender'] = t['first_sender'] or aid
        if recipient not in g['players']:
            npc = g['characters'][recipient.split(':', 1)[1]]
            honor = npc['mbti'][3] == 'J' or self.rng(g, t['trade_id']+':honor').random() < .5
            if honor and t['offers'].get(recipient):
                info2 = npc['information'][t['offers'][recipient][0]]
                self.acquire(g, aid, info2, 'trade', recipient)
                t['fulfilled'][recipient] = True
        self.settle_trades(db, g)
        return self.trade_view(g, t)
    def trade_view(self, g, t):
        return {k: copy.deepcopy(v) for k, v in t.items() if k not in ('offers',)} | {
            'offers': {aid: [possession(next(c['information'][cid] for c in g['characters'].values() if cid in c['information']))
                             for cid in ids] or [{'kind':'possession_category','category':t['offer_categories'][aid]}] for aid, ids in t['offers'].items()}}
    def offer(self,own,d):
        ids=d.get('offer_ids',[]);category=d.get('offer_category')
        require(isinstance(ids,list) and len(ids)<=1,'invalid_input','offer one ID or one category')
        if ids:
            require(isinstance(ids[0],str) and ids[0] in own['information'] and own['information'][ids[0]].get('kind') in ('definite','indirect'), 'unauthorized','offer an owned complete clue')
            return ids,own['information'][ids[0]].get('category','indirect')
        require(category in ('suspect','weapon','place','indirect') and any(x.get('category','indirect')==category and x.get('kind') in ('definite','indirect') for x in own['information'].values()),'unauthorized','category signal requires owned evidence')
        return [],category
    def matches_trade(self,info,t):
        if info.get('kind') not in ('definite','indirect'):return False
        if t['requested_category'] is not None and info.get('category','indirect')!=t['requested_category']:return False
        h=t.get('requested_hypothesis')
        if h is None:return True
        if info['kind']=='definite':return h[info['category']]==info['candidate']
        return (info.get('character')==h['suspect'] or info.get('other')==h['suspect'] or
                info.get('weapon')==h['weapon'] or info.get('room')==h['place'])
    def settle_trades(self, db, g, force=False):
        for t in g['trades'].values():
            if t['status'] == 'accepted' and (force or self.e.clock() >= t['deadline'] or all(t['fulfilled'].values())):
                t['status'] = 'settled'
                for aid, fulfilled in t['fulfilled'].items():
                    other = t['recipient'] if aid == t['proposer'] else t['proposer']
                    cid = g['players'][aid]['character_id'] if aid in g['players'] else int(aid.split(':')[1])
                    other_cid = g['players'][other]['character_id'] if other in g['players'] else int(other.split(':')[1])
                    self.relation(g, other_cid, cid, 10 if fulfilled else -20, 'trade_fulfilled' if fulfilled else 'trade_broken')
                    if aid in g['players']:
                        a = self.e.load(db, 'agents', aid)
                        a['negotiation_reputation']['fulfilled' if fulfilled else 'broken'] += 1
                        self.e.put(db, 'agents', aid, a)
                    if other in g['players']:
                        observer=self.e.load(db,'agents',other)
                        peer=observer.setdefault('peer_reputation',{}).setdefault(aid,{'fulfilled':0,'broken':0})
                        peer['fulfilled' if fulfilled else 'broken']+=1
                        self.e.put(db,'agents',other,observer)
                self.e.event(g, 'trade_settled', {'trade_id': t['trade_id'], 'fulfilled': t['fulfilled']}, 'private', (t['proposer'], t['recipient']))
        self.award_missions(g)

    def events(self, db, g, d, role, aid=None):
        after = integer(d.get('after', 0), 'after')
        events = []
        for e in self.all_events(db,g,after):
            if e['sequence'] <= after: continue
            if e['visibility'] == 'shared' or (role == 'player' and aid in e['participants']):
                events.append(copy.deepcopy(e))
            elif role == 'gm' and e['visibility'] == 'supervisor': events.append(copy.deepcopy(e))
            elif role == 'gm' and e['type'] == 'action_completed':
                r = e['payload']['result']
                projection = {k: copy.deepcopy(r[k]) for k in ('game_id', 'agent_id', 'action_id', 'cycle_id', 'phase', 'phase_epoch', 'action')}
                projection.update(acquired_information_ids=[c['id'] for c in r['acquired_information']],
                                  execution_status=r['execution_status'])
                events.append({k: copy.deepcopy(v) for k, v in e.items() if k != 'payload'} | {'payload': {'result': projection}})
        return {'events': events, 'event_sequence': g.get('event_count',0)}
    def handle(self, db, g, route, d):
        if route == 'games/credentials': self.e.operator(d); return self.e.credentials(g)
        if route == 'games/operator': return self.operator(db, g, d)
        if route == 'games/observe':
            self.e.operator(d)
            return self.gm_view(db, g) | {'assignments': copy.deepcopy(g['assignments']), 'observer_token': g['observer_token'],
                                         'relationship_graph': self.relationship_graph(g)}
        if route == 'games/export':
            self.e.operator(d)
            return {'game_id': g['id'], 'ruleset': SPEC, 'seed': g['seed'], 'rules': copy.deepcopy(g['rules']),
                    'model': self.pool(db, g['pool_id'])['model_version'], 'metrics': self.metrics(g),
                    'relationships': self.relationship_graph(g), 'relationship_events': [copy.deepcopy(x) for x in self.all_events(db,g) if x['type'] == 'relationship_changed'],
                    'feedback': copy.deepcopy(g.get('final_feedback')), 'provenance': copy.deepcopy(g.get('provenance', {})),
                    'visibility': 'operator-research; contains private relationship measurements'}
        if route == 'observer/snapshot':
            require(token_matches(d.get('observer_token'), g['observer_token']), 'unauthorized', 'spectator credential required')
            return self.visible(g)
        if route.startswith('gm/'):
            self.e.gm(g, d, allow_waiting=True, allow_ended=True)
            if route == 'gm/observe': return self.gm_view(db, g)
            if route == 'gm/command': return self.gm_command(db, g, d)
            if route == 'gm/heartbeat':
                require(not g['ended'], 'not_running', 'ended')
                g['controller']['lease_until'] = self.e.clock()+g['rules']['gm_lease']
                return {'term': g['term'], 'lease_until': g['controller']['lease_until']}
            if route == 'gm/mission-candidates':
                require(not g['started'], 'conflict', 'candidate draw is setup-only')
                activation = d.get('activate', [])
                return {'mission_candidates': self.mission_candidates(db, g, activation),
                        'characters': self.preview_characters(db, g, activation),
                        'indirect_clue_ids': [x['id'] for x in g['clues'] if x['kind'] == 'indirect']}
        if route == 'events':
            if 'controller_token' in d:
                self.e.gm(g, d, allow_waiting=True, allow_ended=True)
                return self.events(db, g, d, 'gm')
            p = self.player(g, d, read=True)
            self.check_archive_scope(db,g,p)
            return self.events(db, g, d, 'player', p['agent_id'])
        p = self.player(g, d, read=True)
        self.check_archive_scope(db,g,p)
        if route == 'agent/observe': return self.player_view(g, p)
        if route == 'relationships/graph': return self.relationship_graph(g, p['character_id'])
        if route == 'agent/heartbeat':
            require(not g['ended'] and p['mode'] == 'active', 'not_running', 'inactive')
            p['lease_until'] = self.e.clock()+g['rules']['player_lease']
            return {'lease_until': p['lease_until']}
        if route == 'agent/action': return self.action(db, g, d)
        if route == 'agent/result':
            receipt = g['receipts'].get(d.get('action_id')) or self.record(db,g,'action',d.get('action_id'))
            require(receipt is not None and receipt['result']['agent_id'] == p['agent_id'], 'unauthorized', 'own receipt required')
            return copy.deepcopy(receipt['result'])
        if route == 'questions/respond': return self.respond(db, g, d)
        if route == 'questions/inbox':
            return {'questions': [self.pair(q) | {'asker_id': q['asker_id'], 'asker_character': q['asker_character'],
                        'approach': q['approach'], 'hypothesis': copy.deepcopy(q['hypothesis']), 'successes': q['successes'],
                        'response_options': self.options(g, q), 'deadline': q['deadline']}
                    for q in g['questions'].values() if q['target_agent'] == p['agent_id'] and q['status'] == 'pending' and self.e.clock() < q['deadline']]}
        if route.startswith('trades/'):
            operation = route.split('/')[1]
            if operation == 'inbox': return {'trades': [self.trade_view(g, t) for t in g['trades'].values() if p['agent_id'] in (t['proposer'], t['recipient'])]}
            require(operation in ('propose', 'respond', 'transfer'), 'not_found', 'unknown trade route')
            mid = identifier(d.get('message_id', f'{d.get("trade_id")}:{operation}:{p["agent_id"]}'), 'message_id')
            encoded = fingerprint(d)
            old = g['trade_receipts'].get(mid) or self.record(db,g,'trade_message',mid)
            if old:
                require(old['fingerprint'] == encoded, 'conflict', 'trade message_id reused')
                return copy.deepcopy(old['result'])
            result = self.trade(db, g, d, operation)
            g['trade_receipts'][mid] = {'fingerprint': encoded, 'result': copy.deepcopy(result)}
            return result
        if route == 'agent/share': raise ContractError('invalid_input', 'voluntary sharing removed; use negotiation')
        if route == 'documents/search':
            query = text(d.get('query', ''), 'query', 200)
            documents = [copy.deepcopy(x) for x in self.ch(g, p)['information'].values() if query in x.get('text', '')]
            self.e.event(g, 'document_search', {'agent_id': p['agent_id'], 'result_ids': [x['id'] for x in documents]}, 'private', (p['agent_id'],))
            return {'documents': documents, 'source': 'owned-case-records'}
        raise ContractError('not_found', 'unknown competitive route')
    def check_archive_scope(self,db,g,p):
        if g['ended']:
            member=self.e.load(db,'agents',p['agent_id'])
            require(member['busy_game_id'] in (None,g['id']),'stale_assignment',
                    'old episode secrets are sealed while assigned to another game; use pool/feedback')


def public_clue(clue): return {k: copy.deepcopy(v) for k, v in clue.items() if k != 'source_room'}
def possession(clue): return {'id': clue['id'], 'kind': 'possession', 'category': clue.get('category', 'indirect')}
