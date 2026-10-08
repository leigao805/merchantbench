#!/usr/bin/env python3
"""Optional static research figure; install matplotlib separately to use."""
import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    with (args.directory / 'metrics.csv').open() as f:
        rows = list(csv.DictReader(f))
    summary = json.loads((args.directory / 'summary.json').read_text())
    report_note = 'no reports' if summary['daily_reports']['available_days'] == 0 else 'check report coverage'
    curves = defaultdict(list)
    with (args.directory / 'asset_curves.csv').open() as f:
        for row in csv.DictReader(f):
            curves[(row['mode'], int(row['seed']))].append(
                (float(row['elapsed_days']), float(row['net_assets'])))
    colors = {'random': '#c65b27', 'daily_report': '#226ab3'}
    labels = {'random': 'Random', 'daily_report': f'Daily-report mode ({report_note})'}
    seeds = sorted({int(r['seed']) for r in rows})
    fig, axes = plt.subplots(1, 3, figsize=(16, 4.6), layout='constrained')
    for mode in colors:
        first = True
        for (key, _), points in sorted(curves.items()):
            if key != mode:
                continue
            axes[0].plot(*zip(*points), color=colors[mode], alpha=.65, linewidth=1,
                         label=labels[mode] if first else None)
            first = False
        data = {int(r['seed']): r for r in rows if r['mode'] == mode}
        offset = -.18 if mode == 'random' else .18
        xs = [i + offset for i, seed in enumerate(seeds) if seed in data]
        axes[1].bar(xs, [float(data[s]['final_net_assets']) for s in seeds if s in data],
                    width=.35, color=colors[mode])
        axes[2].bar(xs, [100 * float(data[s]['max_drawdown_fraction']) for s in seeds if s in data],
                    width=.35, color=colors[mode])
    initial = float(rows[0]['initial_assets'])
    axes[0].axhline(initial, color='#888888', linestyle='--', linewidth=.8)
    axes[0].set(xlabel='Simulated days (including settlement tail)', ylabel='Net assets', title='Full hourly asset paths')
    axes[0].legend(fontsize=8, loc='best')
    axes[1].set(title='Final net assets, paired seeds', ylabel='Net assets', xlabel='Environment seed')
    axes[1].axhline(initial, color='#888888', linestyle='--', linewidth=.8)
    axes[2].set(title='Maximum hourly drawdown', ylabel='Percent', xlabel='Environment seed')
    for ax in axes[1:]:
        ax.set_xticks(range(len(seeds)), seeds, rotation=60)
    for ax in axes:
        ax.spines[['top', 'right']].set_visible(False)
        ax.grid(axis='y', alpha=.15)
        ax.set_axisbelow(True)
    fig.suptitle(f"MerchantBench | {rows[0]['days']}-day requested horizon | upstream rule policies", fontsize=12)
    output = args.output or args.directory / 'comparison.png'
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=160)
    print(output)


if __name__ == '__main__':
    main()
