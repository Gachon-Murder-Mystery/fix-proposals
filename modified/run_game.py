"""Unified entry point; original team checkouts stay independent."""
import argparse
import importlib.util
import json
import os
import shlex
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def load_env(path=ROOT / '.env'):
    """Read simple dotenv assignments without overriding shell variables.

    Supports export, quoted values and inline comments; no shell execution.
    """
    if not path.is_file():
        return
    for number, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:].lstrip()
        name, separator, raw = line.partition('=')
        name = name.strip()
        if not separator or not name.isidentifier() or not name.isascii():
            raise ValueError(f'{path.name}:{number}: invalid environment assignment')
        try:
            parts = shlex.split(raw, comments=True, posix=True)
        except ValueError:
            raise ValueError(f'{path.name}:{number}: invalid quoted value') from None
        if len(parts) > 1:
            raise ValueError(f'{path.name}:{number}: quote values containing spaces')
        os.environ.setdefault(name, parts[0] if parts else '')


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

def memory_dir():
    """[FIX-I2] Where B keeps each persistent agent's lessons (next to the pool DB in batches)."""
    return Path(os.environ.get('GAME_MEMORY_DIR') or ROOT / 'runs' / 'agent-memory')


def llm_factory(role, session, catalog):
    backend = os.environ.get('GAME_LLM_BACKEND', 'api')  # [FIX-I3] api | fake | codex
    key = os.environ.get('GAME_API_KEY', '')
    model = os.environ.get('GAME_MODEL', '')
    if backend == 'api' and (not key or not model):
        raise RuntimeError('Set GAME_API_KEY and GAME_MODEL before --llm')
    b = ROOT / 'Agent_Reasoning'
    saved = {name: sys.modules.get(name) for name in ('schema', 'llm', 'agent')}
    try:
        load('schema', b / 'schema.py')
        gateway = load('llm', b / 'llm.py')
        agent_module = load('agent', b / 'agent.py')
        adapter = load('_team_b_adapter', b / 'adapter.py')
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
    log = ROOT / 'runs' / session['game_id'].replace(':', '_') / (session['agent_id'] + '.jsonl')
    log.parent.mkdir(parents=True, exist_ok=True)
    if backend == 'fake':
        from offline_llm import RuleBasedLLM
        client = RuleBasedLLM(log.parent / 'fake-calls.jsonl')
    elif backend == 'codex':
        from offline_llm import CodexCLIClient
        client = CodexCLIClient(os.environ['CODEX_EXE'], log.parent / 'codex-calls.jsonl',
                                model=os.environ.get('CODEX_MODEL') or None,
                                reasoning=os.environ.get('CODEX_REASONING', 'low'))
    else:
        engine_api = load('_team_engine_client', ROOT / 'Game-engine' / 'engine_client.py')
        llm_role = role.bind(**role.credentials)
        llm_role.timeout = 60
        client = engine_api.EngineLLMClient(llm_role, key, model=model,
            connection=os.environ.get('GAME_PROVIDER', 'openai'), stage='player.decision-or-answer')
    from llm_transcript import TranscriptClient
    client = TranscriptClient(client, log.parent, session['agent_id'], role)
    agent = agent_module.LLMAgent(session['agent_id'], client=client, catalog=catalog, log_path=log)
    return adapter.LLMPlayerRuntime(role, session, catalog, agent=agent, memory_dir=memory_dir())

if __name__ == '__main__':
    load_env()
    # Register the factory under a stable name for C's dynamic import.
    sys.modules['run_game'] = sys.modules[__name__]
    if '--llm' in sys.argv:
        sys.argv.remove('--llm')
        if not os.environ.get('GAME_API_KEY') or not os.environ.get('GAME_MODEL'):
            raise SystemExit('--llm requires GAME_API_KEY and GAME_MODEL')
        os.environ['GAME_LLM_BACKEND'] = 'api'
        sys.argv.extend(['--player', 'run_game:llm_factory'])
    elif '--fake-llm' in sys.argv:  # [FIX-I3] B's full LLM path with a free rule-based stand-in
        sys.argv.remove('--fake-llm')
        os.environ['GAME_LLM_BACKEND'] = 'fake'
        sys.argv.extend(['--player', 'run_game:llm_factory'])
    elif '--codex' in sys.argv:  # [FIX-I3] temporary: local Codex CLI as the model
        sys.argv.remove('--codex')
        from offline_llm import find_codex
        exe = find_codex()
        if not exe:
            raise SystemExit('--codex: codex CLI not found (set CODEX_EXE)')
        os.environ['CODEX_EXE'] = exe
        os.environ['GAME_LLM_BACKEND'] = 'codex'
        sys.argv.extend(['--player', 'run_game:llm_factory'])
    from GM_Agent_Society import run_scenario
    output_parser = argparse.ArgumentParser(add_help=False)
    output_parser.add_argument('--out')
    output_args, remaining = output_parser.parse_known_args(sys.argv[1:])
    summary_name = Path(output_args.out).name if output_args.out else None
    if summary_name in ('', '.', '..', 'full-record.json'):
        raise SystemExit('--out requires a summary filename other than full-record.json')
    sys.argv[1:] = remaining
    original_run = run_scenario.run

    def run_with_export(client, operator_token, *args, **kwargs):
        from game_safety import TrackedClient, cleanup
        from research_trace import TraceClient
        client = TrackedClient(TraceClient(client))
        # LLM calls block the driver, but must not stop role leases.
        identities = []
        stopped = threading.Event()
        original_worker = run_scenario.AgentWorker

        def tracked_worker(identity, *worker_args, **worker_kwargs):
            identities.append(identity)
            return original_worker(identity, *worker_args, **worker_kwargs)

        def keep_alive():
            while not stopped.wait(2):
                for identity in list(identities):
                    try:
                        session = identity.call('pool/session')
                        role = session['role']
                        if role in ('gm', 'player'):
                            identity.bind(**session['credentials']).call(
                                'gm/heartbeat' if role == 'gm' else 'agent/heartbeat')
                    except Exception as exc:
                        if getattr(exc, 'code', None) not in ('not_running', 'unauthorized', 'stale_term'):
                            print(f'heartbeat failed: {type(exc).__name__}', file=sys.stderr)

        run_scenario.AgentWorker = tracked_worker
        heartbeat = threading.Thread(target=keep_alive, daemon=True)
        heartbeat.start()
        try:
            result = original_run(client, operator_token, *args, **kwargs)
        except BaseException:
            stopped.set()
            heartbeat.join(timeout=15)
            errors = cleanup(client, operator_token, ROOT)
            for error in errors:
                print(error, file=sys.stderr)
            if client.created and not errors:
                print('실패한 실행의 게임 종료·배정 해제·전체 기록 저장 완료', file=sys.stderr)
            raise
        finally:
            stopped.set()
            heartbeat.join(timeout=15)
            run_scenario.AgentWorker = original_worker
        # [FIX-I3] Say which backend produced the decisions; fake/codex runs are not the API model.
        result['llm_backend'] = os.environ.get('GAME_LLM_BACKEND', 'scripted')
        if result['llm_backend'] == 'codex':
            result['llm_note'] = 'Codex CLI; calls not archived in engine llm_records (see runs/<game>/codex-calls.jsonl)'
        target = ROOT / 'runs' / result['game_id'] / 'full-record.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        record = client.bind(operator_token=operator_token).call(
            'games/export', game_id=result['game_id'], scope='all')
        target.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        from research_trace import export_views
        export_views(record, target.parent)
        result['log_views'] = ['public-log.html', 'table-log.html', 'detail-log.html']
        result['full_record_file'] = str(target)
        if summary_name:
            summary = target.parent / summary_name
            result['summary_file'] = str(summary)
            summary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            print(f'게임 요약: {summary}', file=sys.stderr)
        print(f'전체 게임·LLM 기록: {target}', file=sys.stderr)
        return result

    run_scenario.run = run_with_export
    run_scenario.main()
