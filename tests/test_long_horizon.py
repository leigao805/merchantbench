import importlib.util
import json
import gzip
import sys
from pathlib import Path

import pytest


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path('scripts') / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_drawdown_includes_initial_capital_and_handles_recovery():
    m = load('analyze_long_horizon')
    result = m.curve_metrics([[1, 80], [2, 120], [3, 60], [4, 130]], 100)
    assert result['max_drawdown_absolute'] == 60
    assert result['max_drawdown_fraction'] == .5
    assert result['asset_return_fraction'] == pytest.approx(.3)
    assert m.curve_metrics([[1, -20]], 100)['max_drawdown_fraction'] == 1.2


@pytest.mark.parametrize('series', [[], [[1, float('nan')]], [[2, 10], [1, 20]], [[1, 10], [1, 20]]])
def test_invalid_curves_fail_closed(series):
    with pytest.raises(ValueError):
        load('analyze_long_horizon').curve_metrics(series, 100)


def test_analysis_keeps_deaths_but_excludes_execution_errors(tmp_path):
    m = load('analyze_long_horizon')
    job = {'selection_mode': 'random', 'seed': 42, 'selection_seed': 42, 'days': 365}
    manifest = {'daily_reports': {}, 'entries': [
        {'state': 'complete', 'job': job, 'run_id': 'dead', 'artifact': 'dead.json.gz'},
        {'state': 'failed', 'job': {**job, 'selection_mode': 'daily_report'}}]}
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest))
    artifact = {'status': {'phase': 'finished', 'state': 'finished', 't': 100},
                'scenario': {'run': {'horizon_steps': 8760, 'initial_cash': 2000, 'initial_deposit': 1000}},
                'wall_seconds': 5,
                'merchant': {'is_alive': False, 'died_at_t': 90,
                             'series': {'net_assets': [[t, 3000 if t < 99 else 50] for t in range(100)],
                                        'cum_net_profit': [[100, -2950]],
                                        'shop_rating_mean': [[100, 2.0]]}}}
    with gzip.open(tmp_path / 'dead.json.gz', 'wt') as f:
        json.dump(artifact, f)
    result = m.analyze(tmp_path)
    assert result['completed'] == 1
    assert len(result['incomplete']) == 1
    assert result['groups']['random']['survived_horizon']['mean'] == 0
    assert result['groups']['random']['final_net_assets']['mean'] == 50
    assert result['pairs'] == []
    artifact['merchant']['is_alive'] = True
    with gzip.open(tmp_path / 'dead.json.gz', 'wt') as f:
        json.dump(artifact, f)
    with pytest.raises(ValueError, match='before requested horizon'):
        m.analyze(tmp_path)


def test_interval_zero_forwarded_and_invalid_values_rejected(monkeypatch):
    batch = load('run_batch')
    bodies = []
    monkeypatch.setattr(batch, 'request_json', lambda *a, **kw: bodies.append(kw['body']) or {'run_id': 'test'})
    config = batch.load_queue_config('experiments/long_horizon/queue.yaml')
    jobs = batch.jobs_from_config(config)
    assert len(jobs) == 20
    assert {j['seed'] for j in jobs if j['selection_mode'] == 'random'} == {j['seed'] for j in jobs if j['selection_mode'] == 'daily_report'}
    batch.create_run('http://localhost', jobs[0])
    assert bodies[0]['interval_ms'] == 0
    assert bodies[0]['scenario']['run']['horizon_steps'] == 8760
    for value in [-1, True, 1.5]:
        with pytest.raises(ValueError, match='interval_ms'):
            batch.jobs_from_config({**config, 'interval_ms': value})


def test_bootstrap_is_repeatable_and_no_single_seed_ci():
    m = load('analyze_long_horizon')
    assert m.bootstrap_mean_ci([5]) is None
    assert m.bootstrap_mean_ci([1, 2, 3]) == m.bootstrap_mean_ci([1, 2, 3])
    assert m.wilson_interval(10, 10)[0] == pytest.approx(.7224672)
    assert m.wilson_interval(0, 10)[1] == pytest.approx(.2775328)


def test_collector_resume_reuses_completed_runs_and_rejects_changed_config(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path('scripts').resolve()))
    runner = load('run_long_horizon')
    queue = tmp_path / 'queue.yaml'
    queue.write_text('scenario_path: env/scenarios/default.yaml\nbootstrap_agent: rule_based\ndays: 1\nqueue:\n  - selection_mode: random\n')
    out = tmp_path / 'out'
    calls = []
    monkeypatch.setattr(runner.batch, 'create_run', lambda *a: calls.append(a) or 'fake')
    monkeypatch.setattr(runner.batch, 'get_run_status', lambda *a: {'phase': 'finished', 'state': 'finished', 't': 24})
    monkeypatch.setattr(runner.batch, 'request_json', lambda *a: {
        'is_alive': True, 'series': {'net_assets': [[t, 3000] for t in range(24)],
                                  'cum_net_profit': [[23, 0]], 'shop_rating_mean': [[23, 4]]}})
    argv = ['run_long_horizon', '--queue', str(queue), '--output', str(out)]
    monkeypatch.setattr(sys, 'argv', argv)
    assert runner.main() == 0
    monkeypatch.setattr(sys, 'argv', argv + ['--resume'])
    assert runner.main() == 0
    assert len(calls) == 1
    monkeypatch.setattr(sys, 'argv', argv + ['--resume', '--days', '2'])
    with pytest.raises(SystemExit) as exc:
        runner.main()
    assert exc.value.code == 2
    assert len(calls) == 1


def test_ambiguous_submission_is_not_automatically_retried(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path('scripts').resolve()))
    runner = load('run_long_horizon')
    queue = tmp_path / 'queue.yaml'
    queue.write_text('scenario_path: env/scenarios/default.yaml\nbootstrap_agent: rule_based\ndays: 1\nqueue:\n  - selection_mode: random\n')
    out = tmp_path / 'out'
    calls = []
    def fail(*a):
        calls.append(a)
        raise TimeoutError('response lost after submission')
    monkeypatch.setattr(runner.batch, 'create_run', fail)
    argv = ['run_long_horizon', '--queue', str(queue), '--output', str(out)]
    monkeypatch.setattr(sys, 'argv', argv)
    assert runner.main() == 1
    monkeypatch.setattr(sys, 'argv', argv + ['--resume'])
    assert runner.main() == 1
    assert len(calls) == 1
    manifest = json.loads((out / 'manifest.json').read_text())
    assert manifest['entries'][0]['state'] == 'submitting'
