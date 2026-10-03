#!/usr/bin/env python3
"""Whitelist-only source ZIP and SHA-256. No user configuration or vault data."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import uuid
import zipfile

import vm_config as config
from memory_v1 import VaultError, digest, json_bytes


def selected_files(root):
    root = config.guard(root)
    values = {}
    for name in ('SKILL.md', 'README.md', 'agents/openai.yaml'):
        raw = config.raw_file(root / name)
        if raw is None:
            raise VaultError('INVALID_RELEASE', f'缺少发布文件：{name}')
        values[name] = raw
    for folder in ('scripts', 'references', 'tests'):
        top = config.guard(root / folder)
        for path in sorted(top.rglob('*')):
            if '__pycache__' in path.parts or '.pytest_cache' in path.parts:
                continue
            config.guard(path)
            if path.is_dir():
                continue
            permitted = path.suffix == '.py' if folder in {'scripts', 'tests'} else path.suffix == '.md' or path.name in {'v1-release.json', 'v2-release.json'}
            if not permitted:
                if path.suffix in {'.pyc', '.pyo'}:
                    continue
                raise VaultError('NON_RELEASE_FILE', f'白名单目录中出现未知文件，未打包：{path.relative_to(root)}')
            values[path.relative_to(root).as_posix()] = config.raw_file(path)
    for rel, raw in values.items():
        text = raw.decode('utf-8-sig')
        if re.search(r'(?i)[A-Z]:[\\/]+Users[\\/]+[^<\s"\']+', text) or re.search(r'(?i)[A-Z]:[\\/]+AgentMemory', text):
            raise VaultError('PERSONAL_PATH_IN_RELEASE', f'发布文件含个人绝对路径：{rel}')
    return values


def build(source, output):
    source, output = config.guard(source), config.guard(output)
    if config.overlap(source, output):
        raise VaultError('OVERLAPPING_PATHS', '发布输出必须位于源码目录以外。')
    values = selected_files(source)
    manifest = {'app': 'visible-memory', 'version': config.VERSION, 'vault_format': 2, 'config_format': 1,
                'platform': 'windows', 'files': {p: digest(raw) for p, raw in sorted(values.items())}}
    marker = json_bytes(manifest)
    before = config.raw_file(source / 'release.json')
    if before != marker:
        config.atomic(source / 'release.json', marker, digest(before) if before is not None else None)
    values['release.json'] = marker
    config.guard(output).mkdir(parents=True, exist_ok=True)
    archive = config.guard(output / ('visible-memory-' + config.VERSION + '-source.zip'))
    temp = config.guard(output / ('.visible-memory-release-' + str(uuid.uuid4()) + '.tmp'))
    try:
        with zipfile.ZipFile(temp, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
            for rel, raw in sorted(values.items()):
                info = zipfile.ZipInfo('visible-memory/' + rel, date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                bundle.writestr(info, raw)
        contents = temp.read_bytes()
        sha = digest(contents)
        if archive.exists():
            if digest(config.raw_file(archive)) != sha:
                raise VaultError('RELEASE_CONFLICT', '同版本 ZIP 已存在且内容不同，请使用新的输出目录。')
        else:
            temp.rename(archive)
        checksum = archive.with_suffix(archive.suffix + '.sha256')
        expected = f'{sha}  {archive.name}\n'.encode('utf-8')
        current = config.raw_file(checksum)
        if current is None:
            config.atomic(checksum, expected)
        elif current != expected:
            raise VaultError('RELEASE_CONFLICT', '同名 SHA-256 文件不同，未覆盖。')
        with zipfile.ZipFile(archive) as bundle:
            if bundle.testzip() is not None or set(bundle.namelist()) != {'visible-memory/' + p for p in values}:
                raise VaultError('VERIFY_FAILED', '源码 ZIP 完整性验证失败。')
            for rel, raw in values.items():
                if bundle.read('visible-memory/' + rel) != raw:
                    raise VaultError('VERIFY_FAILED', '源码 ZIP 内容验证失败。')
        return {'ok': True, 'version': config.VERSION, 'files': len(values), 'archive': str(archive),
                'sha256_file': str(checksum), 'sha256': sha, 'bytes': len(contents)}
    finally:
        if temp.exists():
            config.guard(temp).unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).parent.parent)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = build(args.source, args.output)
    except (VaultError, OSError, ValueError) as exc:
        result = {'ok': False, 'error': {'code': getattr(exc, 'code', 'IO_ERROR'), 'message': str(exc)}}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get('ok') else 1


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
