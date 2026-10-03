"""Synthetic-only scale acceptance. Never points at the desktop memory vault."""
from __future__ import annotations
import argparse
import contextlib
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
import time
import uuid
import subprocess
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import memory
import vm_config
from vm_location import DirectoryService


class MeasuredVault(memory.Vault):
    reads = 0
    def _raw(self, path):
        if Path(path).parent.name == 'entries':
            self.reads += 1
        return super()._raw(path)


def stats(values):
    ordered = sorted(values)
    return {'n': len(values), 'first_ms': round(values[0], 3),
            'median_ms': round(statistics.median(values), 3),
            'p95_ms': round(ordered[math.ceil(len(values) * .95) - 1], 3)}


def measure(vault, count, callback):
    timings, reads = [], []
    for iteration in range(count):
        vault.reads = 0
        start = time.perf_counter()
        result = callback(iteration)
        timings.append((time.perf_counter() - start) * 1000)
        reads.append(vault.reads)
        if not result.get('ok'):
            raise RuntimeError(result)
    return {**stats(timings), 'entry_reads_min': min(reads), 'entry_reads_max': max(reads)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--sizes', type=int, nargs='+', default=[1000, 5000, 10000])
    parser.add_argument('--runs', type=int, default=30)
    args = parser.parse_args()
    parent = Path(args.workspace).resolve()
    if 'Desktop' in parent.parts or args.runs < 30:
        raise ValueError('Use an isolated workspace and at least 30 repetitions')
    run_root = parent / ('visible-memory-v2-scale-' + uuid.uuid4().hex[:10])
    run_root.mkdir(parents=True)
    report = {'python': sys.version, 'sqlite': memory.sqlite3.sqlite_version,
              'root': str(run_root), 'timing': 'Direct API includes metadata diff and selected raw readback; excludes process/tool/model startup; OS cache not flushed.',
              'results': []}
    startup = []
    for _ in range(args.runs):
        start = time.perf_counter()
        subprocess.run([sys.executable, '-B', '-c', 'pass'], check=True, capture_output=True)
        startup.append((time.perf_counter() - start) * 1000)
    report['python_empty_process_ms'] = stats(startup)
    report['tool_and_model_time'] = 'Excluded; not measured by this local benchmark.'
    for count in args.sizes:
        root = run_root / str(count)
        vault = MeasuredVault(root)
        vault.execute('init', request='Initialize synthetic benchmark vault', apply=True)
        records = []
        total_bytes = 0
        for i in range(count):
            entry = {'id': str(uuid.uuid5(uuid.NAMESPACE_DNS, 'visible-memory-synthetic-' + str(i))),
                     'title': f'虚构记忆 {i:05d}', 'type': list(memory.TYPES)[i % 5],
                     'status': 'archived' if i % 7 == 0 else 'active',
                     'tags': ['虚构', f'专题{i % 31:02d}'], 'source': 'Synthetic scale acceptance; not personal data',
                     'scope': f'虚构项目{i % 13}', 'created_at': '2026-09-06T12:00:00+08:00',
                     'updated_at': f'2026-09-{i % 6 + 1:02d}T12:00:00+08:00', 'revision': 1,
                     'body': (f'公共检索 中文风格 UI 设计备忘 English details {i:05d}。' +
                              ('专题写作风格应清晰简洁。' if i % 97 == 0 else '') +
                              ('这是虚构的测试材料，记录约定、条件、例外与来源，不用于个人记忆。' * [1, 8, 25, 70][i % 4]))}
            raw = memory.render_entry(entry)
            path = root / 'entries' / (entry['id'] + '.md')
            path.write_bytes(raw)
            total_bytes += len(raw)
            records.append((entry, raw, path))
        print(json.dumps({'phase': 'rebuild', 'entries': count}, ensure_ascii=False), flush=True)
        started = time.perf_counter()
        rebuilt = vault.execute('rebuild', request='Build synthetic benchmark index', apply=True)
        if not rebuilt['ok']:
            raise RuntimeError(rebuilt)
        build_ms = (time.perf_counter() - started) * 1000
        row = {'entries': count, 'markdown_bytes': total_bytes,
               'index_bytes': (root / '_index' / 'search.sqlite').stat().st_size,
               'operations_bytes': (root / '_state' / 'operations.sqlite').stat().st_size,
               'rebuild_ms': round(build_ms, 3),
               'index_md_bytes': (root / 'INDEX.md').stat().st_size,
               'index_md_lines': len((root / 'INDEX.md').read_text(encoding='utf-8').splitlines())}
        row['selective'] = measure(vault, args.runs, lambda _: vault.execute('search', request='Search synthetic writing notes', query='专题写作'))
        row['short_word'] = measure(vault, args.runs, lambda _: vault.execute('search', request='Search synthetic short term', query='UI', scope='虚构项目1'))
        row['broad'] = measure(vault, args.runs, lambda _: vault.execute('search', request='Search synthetic common phrase', query='公共检索'))
        row['no_match'] = measure(vault, args.runs, lambda _: vault.execute('search', request='Search synthetic absent term', query='不存在的天文主题'))
        chosen = records[1][0]['id']
        row['show'] = measure(vault, args.runs, lambda _: vault.execute('show', request='Read specified synthetic record', entry_id=chosen))
        config_path = run_root / ('config-' + str(count)) / 'config.json'
        settings = DirectoryService(config_path)
        preview = settings.execute('use', request='Bind synthetic benchmark directory', path=root)
        settings.execute('use', request='Bind synthetic benchmark directory', path=root, apply=True, preview_token=preview['preview_token'])
        def configured(command, **kwargs):
            vm_config.require_request(kwargs['request'])
            actual_root, expected_id, info = vm_config.resolve_root(config=config_path)
            if actual_root != root:
                raise RuntimeError('Unexpected configured benchmark path')
            vault.expected_vault_id = expected_id
            return vault.execute(command, **kwargs)
        row['configured_selective'] = measure(vault, args.runs, lambda _: configured('search', request='Search synthetic writing notes', query='专题写作'))
        row['configured_show'] = measure(vault, args.runs, lambda _: configured('show', request='Read specified synthetic record', entry_id=chosen))
        def add(iteration):
            return vault.execute('add', request='Save synthetic benchmark record', apply=True,
                                 payload={'title': f'新增虚构 {iteration}', 'type': 'note', 'tags': ['压测'],
                                          'source': 'Synthetic acceptance', 'scope': '虚构测试',
                                          'body': f'第 {iteration} 次增量写入，仅测试性能。', 'request_id': str(uuid.uuid4())})
        row['add'] = measure(vault, args.runs, add)
        row['targets'] = {'selective': row['selective']['p95_ms'] <= (250 if count <= 5000 else 500),
                          'broad': row['broad']['p95_ms'] <= 2000,
                          'add': row['add']['p95_ms'] <= 1000,
                          'bounded_home': row['index_md_bytes'] <= 32768 and row['index_md_lines'] <= 200}
        report['results'].append(row)
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(row, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
