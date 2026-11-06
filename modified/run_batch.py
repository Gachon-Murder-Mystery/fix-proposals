"""Run isolated games sequentially; resume successful seeds from a manifest."""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def save(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start-seed', type=int, default=1)
    parser.add_argument('--count', type=int, default=30)
    parser.add_argument('--llm', action='store_true')
    parser.add_argument('--fake-llm', action='store_true', help='B path with a free rule-based stand-in model')
    parser.add_argument('--codex', action='store_true', help='temporary: local Codex CLI as the model')
    parser.add_argument('--deadline', type=int, default=120, help='game cycle limit')
    parser.add_argument('--time-limit', type=int, default=3600, help='per-game driver limit in seconds')
    parser.add_argument('--batch-dir', type=Path, help='existing directory resumes successful seeds')
    # [FIX-I1] experiment-30 gave every seed its own game.sqlite3, so the engine bootstrapped a
    # brand-new pool (new agent ids, empty history) per seed: 30 first games. Now all seeds
    # share one pool DB, so the same 16 persistent agents play and accumulate experience.
    parser.add_argument('--isolated', action='store_true',
                        help='old behaviour: fresh pool per seed (no cross-game experience)')
    args = parser.parse_args()
    if sum((args.llm, args.fake_llm, args.codex)) > 1:
        parser.error('choose one of --llm / --fake-llm / --codex')
    if args.count < 1 or not 1 <= args.deadline <= 1000 or args.time_limit < 1:
        parser.error('count/time-limit must be positive; deadline must be 1..1000')
    directory = (args.batch_dir or ROOT / 'runs' / ('batch-' + time.strftime('%Y%m%d-%H%M%S'))).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / 'batch.json'
    config = {k: getattr(args, k) for k in ('start_seed', 'count', 'llm', 'fake_llm', 'codex',
                                            'deadline', 'time_limit', 'isolated')}
    state = json.loads(manifest.read_text(encoding='utf-8')) if manifest.exists() else {'config': config, 'attempts': []}
    if state['config'] != config:
        parser.error('batch configuration differs; use the original options or a new --batch-dir')
    if args.llm:
        from run_game import load_env
        load_env()
        if not os.environ.get('GAME_API_KEY') or not os.environ.get('GAME_MODEL'):
            parser.error('--llm requires GAME_API_KEY and GAME_MODEL in .env or environment')
    pool_db = directory / 'pool.sqlite3'  # [FIX-I1] one persistent pool for the batch
    memory = directory / 'agent-memory'   # [FIX-I2] B's per-agent lessons live beside it
    state.setdefault('pool_db', None if args.isolated else str(pool_db))
    save(manifest, state)
    print(f'Batch: {directory}', flush=True)
    child_env = dict(os.environ, GAME_MEMORY_DIR=str(memory))
    for seed in range(args.start_seed, args.start_seed + args.count):
        if any(a['seed'] == seed and a['status'] == 'completed' for a in state['attempts']):
            print(f'seed {seed}: already completed; skip', flush=True)
            continue
        attempt_dir = directory / f'seed-{seed}' / f"attempt-{1 + sum(a['seed'] == seed for a in state['attempts'])}"
        attempt_dir.mkdir(parents=True)
        row = {'seed': seed, 'status': 'running', 'directory': str(attempt_dir), 'started_at': time.time()}
        state['attempts'].append(row)
        save(manifest, state)
        data = attempt_dir / 'game.sqlite3' if args.isolated else pool_db
        command = [sys.executable, '-u', str(ROOT / 'run_game.py'), '--seed', str(seed),
                   '--deadline', str(args.deadline), '--time-limit', str(args.time_limit),
                   '--data', str(data), '--out', 'summary.json', '--progress']
        for flag in ('llm', 'fake_llm', 'codex'):
            if getattr(args, flag):
                command.append('--' + flag.replace('_', '-'))
        print(f'seed {seed}: starting', flush=True)
        interrupted = False
        with (attempt_dir / 'stdout.json').open('w', encoding='utf-8') as stdout, (attempt_dir / 'stderr.log').open('w', encoding='utf-8') as stderr:
            proc = subprocess.Popen(command, cwd=ROOT, stdout=stdout, stderr=stderr,
                                    start_new_session=True, env=child_env)
            try:
                code = proc.wait()
            except KeyboardInterrupt:
                interrupted = True
                os.killpg(proc.pid, signal.SIGINT)
                try:
                    code = proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGTERM)
                    code = proc.wait(timeout=10)
        row.update(status='interrupted' if interrupted else 'failed', returncode=code, finished_at=time.time())
        if code == 0 and not interrupted:
            try:
                result = json.loads((attempt_dir / 'stdout.json').read_text(encoding='utf-8'))
                row.update(status='completed', game_id=result['game_id'], resolution=result['resolution'],
                           cycles=result['cycles'], winners=result['winners'],
                           full_record_file=result['full_record_file'], summary_file=result['summary_file'])
            except (ValueError, KeyError):
                row['error'] = 'child exited successfully but result JSON was missing or invalid'
        save(manifest, state)
        print(f"seed {seed}: {row['status']} ({row.get('resolution', 'see stderr.log')})", flush=True)
        if interrupted:
            return 130
    done = sum(a['status'] == 'completed' for a in state['attempts'])
    print(f'{done}/{args.count} seeds completed. Manifest: {manifest}', flush=True)
    return 0 if done == args.count else 1


if __name__ == '__main__':
    raise SystemExit(main())
