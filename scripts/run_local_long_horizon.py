#!/usr/bin/env python3
"""Own a local server and collector in one process/network namespace; sample resources."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.parse import urlparse

import run_batch as batch


def tree_rss():
    """Linux summed RSS sample (shared pages may be counted multiple times)."""
    if not Path('/proc/self/status').exists():
        return None
    # /proc can expose outer PIDs while os.getpid()/Popen.pid use a nested namespace.
    # Use the Pid reported by that same procfs as the root, including this launcher.
    try:
        root_pid = int(next(line.split(':', 1)[1] for line in
                            Path('/proc/self/status').read_text().splitlines()
                            if line.startswith('Pid:')))
    except (OSError, ValueError, StopIteration):
        return None
    table = {}
    for path in Path('/proc').glob('[0-9]*/status'):
        try:
            fields = {line.split(':', 1)[0]: line.split(':', 1)[1].strip()
                      for line in path.read_text().splitlines() if ':' in line}
            table[int(path.parent.name)] = (int(fields['PPid']), int(fields.get('VmRSS', '0 kB').split()[0]) * 1024)
        except (OSError, ValueError, KeyError):
            continue
    if root_pid not in table:
        return None
    family = {root_pid}
    while True:
        expanded = family | {pid for pid, (parent, _) in table.items() if parent in family}
        if expanded == family:
            break
        family = expanded
    return sum(table.get(pid, (0, 0))[1] for pid in family) or None


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--queue', default='experiments/long_horizon/queue.yaml')
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--runs-root', required=True, type=Path, help='fresh, local SQLite/snapshot directory')
    p.add_argument('--days', type=int)
    p.add_argument('--seeds', type=int, nargs='+')
    p.add_argument('--require-reports', action='store_true')
    p.add_argument('--timeout-seconds', type=float, default=14400)
    args = p.parse_args()
    if args.output.exists() or args.runs_root.exists():
        p.error('use fresh output and runs-root; reconnect existing servers with run_long_horizon --resume')
    config = batch.load_queue_config(args.queue)
    url = urlparse(config['base_url'])
    if url.scheme != 'http' or url.hostname != '127.0.0.1' or url.path not in ('', '/'):
        p.error('local launcher requires http://127.0.0.1:PORT')
    port = url.port or 80
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', port))  # refuse to attach to somebody else's server
    args.output.mkdir(parents=True)
    args.runs_root.mkdir(parents=True)
    logfile = (args.output / 'server.log').open('w')
    server = subprocess.Popen([sys.executable, str(batch.ROOT / 'env/run.py'), '--port', str(port),
                               '--runs-root', str(args.runs_root.resolve()),
                               '--db-path', str((args.runs_root / 'legacy.db').resolve())],
                              cwd=batch.ROOT, stdout=logfile, stderr=logfile)
    collector = None
    started = time.monotonic()
    peak = None
    code = 1
    try:
        ready = False
        for _ in range(100):
            if server.poll() is not None:
                raise RuntimeError('server exited: inspect server.log')
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.2):
                    ready = True
                    break
            except OSError:
                time.sleep(.1)
        if not ready:
            raise RuntimeError('server did not become ready')
        cmd = [sys.executable, str(batch.ROOT / 'scripts/run_long_horizon.py'),
               '--queue', str(Path(args.queue).resolve()), '--output', str(args.output.resolve()),
               '--timeout-seconds', str(args.timeout_seconds)]
        if args.days is not None:
            cmd += ['--days', str(args.days)]
        if args.seeds is not None:
            cmd += ['--seeds', *map(str, args.seeds)]
        if args.require_reports:
            cmd += ['--require-reports']
        collector = subprocess.Popen(cmd, cwd=batch.ROOT)
        while collector.poll() is None:
            sample = tree_rss()
            if sample is not None:
                peak = max(peak or 0, sample)
            time.sleep(.5)
        code = collector.returncode
    finally:
        if collector is not None and collector.poll() is None:
            collector.terminate()
            collector.wait(timeout=10)
        server.terminate()
        try:
            server.wait(timeout=30)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
        logfile.close()
        sizes = [f.stat().st_size for f in args.runs_root.rglob('*') if f.is_file()]
        profile = {'wall_seconds': time.monotonic() - started, 'exit_code': code,
                   'sampled_peak_summed_rss_bytes': peak,
                   'rss_note': 'Launcher/server/collector/agent tree, Linux /proc sampled every .5s; shared pages counted per process; not a strict memory bound',
                   'runtime_storage_bytes': sum(sizes), 'runtime_file_count': len(sizes),
                   'cpu_count_visible': os.cpu_count(), 'gpu_required': False,
                   'llm_api_calls': 0}
        (args.output / 'resources.json').write_text(json.dumps(profile, indent=2) + '\n')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
