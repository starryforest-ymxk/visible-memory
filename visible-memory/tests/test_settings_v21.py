"""Configuration, authorization, relocation and UI state; fictional temp data only."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

SCRIPTS = Path(__file__).parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import memory
import settings
import vm_config as config
import vm_location as location
import vm_operations


def snapshot(root):
    if not root.exists():
        return None
    return {'dirs': sorted(p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_dir()),
            'files': {p.relative_to(root).as_posix(): (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
                      for p in root.rglob('*') if p.is_file()}}


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='虚构目录 设置 ')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.cfg = self.base / '用户 配置' / 'config.json'
        self.root = self.base / '记忆 原目录'
        self.target = self.base / '记忆 新目录'
        self.service = location.DirectoryService(self.cfg)

    def init(self):
        memory.Vault(self.root).execute('init', request='创建虚构测试库', apply=True)
        return self.root

    def apply(self, command, path=None, service=None):
        service = service or self.service
        args = {'request': '用户明确要求虚构目录操作', 'path': path or self.root}
        preview = service.execute(command, **args)
        return service.execute(command, **args, apply=True, preview_token=preview['preview_token'])

    def configured(self):
        self.init()
        self.assertTrue(self.apply('use')['ok'])

    def add(self):
        return memory.Vault(self.root).execute('add', request='保存虚构记忆', apply=True,
            payload={'title': '虚构偏好', 'body': '虚构中文短语 UI，仅供测试。', 'scope': '虚构项目', 'source': '虚构用户',
                     'type': 'preference', 'tags': ['测试'], 'request_id': 'fixture-request'})

    def assert_code(self, code, callback):
        with self.assertRaises(memory.VaultError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)

    def test_missing_config_no_files(self):
        self.assert_code('NOT_CONFIGURED', lambda: config.resolve_root(config=self.cfg))
        result = self.service.execute('get', request='查看当前目录')
        self.assertFalse(result['configured'])
        self.assertFalse(self.cfg.parent.exists())

    def test_default_path_fallback_is_lazy(self):
        with patch.dict(os.environ, {'LOCALAPPDATA': str(self.base / 'Local')}, clear=True):
            self.assertEqual(config.default_config(), self.base / 'Local' / 'VisibleMemory' / 'config.json')
        with patch.dict(os.environ, {}, clear=True), patch.object(Path, 'home', return_value=self.base):
            self.assertEqual(config.default_config(), self.base / 'AppData' / 'Local' / 'VisibleMemory' / 'config.json')
        self.assertEqual(list(self.base.iterdir()), [])

    def test_request_checked_before_any_io(self):
        with patch.object(config, 'guard', side_effect=AssertionError('unexpected IO')):
            self.assert_code('EXPLICIT_REQUEST_REQUIRED', lambda: self.service.execute('get', request=' '))
        output = io.StringIO()
        with patch.object(config, 'read_config', side_effect=AssertionError('unexpected config read')), contextlib.redirect_stdout(output):
            rc = memory.main(['--config', str(self.cfg), 'add', '--request', ' ', '--input', str(self.base / 'absent')])
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(output.getvalue())['error']['code'], 'EXPLICIT_REQUEST_REQUIRED')

    def test_explicit_root_bypasses_bad_config(self):
        self.init()
        with patch.object(config, 'read_config', side_effect=AssertionError('unexpected config read')):
            root, expected, info = config.resolve_root(self.root)
        self.assertEqual(root, self.root)
        self.assertIsNone(expected)
        self.assertEqual(info['root_source'], 'explicit_root')

    def test_get_only_config_even_unavailable_vault(self):
        self.configured()
        before = snapshot(self.base)
        with patch.object(config, 'vault_header', side_effect=AssertionError('header read')), patch.object(location, 'inventory', side_effect=AssertionError('scan')):
            result = self.service.execute('get', request='查看目录')
        self.assertFalse(result['vault_inspected'])
        self.assertEqual(before, snapshot(self.base))

    def test_doctor_no_config_or_vault(self):
        with patch.object(config, 'read_config', side_effect=AssertionError('config read')):
            result = self.service.execute('doctor', request='检查环境')
        self.assertTrue(result['fts5_trigram'])
        self.assertFalse(result['gui_window_tested'])
        self.assertFalse(self.cfg.parent.exists())

    def test_use_only_header(self):
        self.init()
        self.add()
        read_paths = []
        original = memory.Vault._raw
        def track(vault, path):
            read_paths.append(Path(path).relative_to(vault.root).as_posix())
            return original(vault, path)
        before = snapshot(self.root)
        with patch.object(memory.Vault, '_raw', track), patch.object(location, 'inventory', side_effect=AssertionError('unexpected scan')):
            result = self.apply('use')
        self.assertTrue(result['ok'], result)
        self.assertEqual(set(read_paths), {'_state/vault.json'})
        self.assertEqual(before, snapshot(self.root))
        self.assertEqual(set(config.read_config(self.cfg)[0]), config.CONFIG_FIELDS)

    def test_use_preview_readonly(self):
        self.init()
        before = snapshot(self.base)
        self.service.execute('use', request='预览接入', path=self.root)
        self.assertEqual(before, snapshot(self.base))

    def test_create_preview_and_apply(self):
        before = snapshot(self.base)
        preview = self.service.execute('create', request='创建空库', path=self.root)
        self.assertEqual(before, snapshot(self.base))
        result = self.service.execute('create', request='创建空库', path=self.root, apply=True, preview_token=preview['preview_token'])
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['verified'])
        self.assertEqual(list((self.root / 'entries').iterdir()), [])
        self.assertEqual(config.read_config(self.cfg)[0]['vault_root'], str(self.root))

    def test_create_nonempty_does_not_read_unknown_body(self):
        self.root.mkdir()
        (self.root / 'private.txt').write_text('not memory', encoding='utf-8')
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('unknown body read')):
            self.assert_code('NONEMPTY_TARGET', lambda: self.service.execute('create', request='创建库', path=self.root))

    def test_stale_config_token(self):
        self.configured()
        preview = self.service.execute('create', request='创建新库', path=self.target)
        cfg, sha = config.read_config(self.cfg)
        cfg['revision'] += 1
        config.atomic(self.cfg, config.json_bytes(cfg), sha)
        self.assert_code('STALE_PREVIEW', lambda: self.service.execute('create', request='创建新库', path=self.target, apply=True, preview_token=preview['preview_token']))
        self.assertFalse(self.target.exists())

    def test_apply_requires_preview(self):
        self.init()
        self.assert_code('PREVIEW_REQUIRED', lambda: self.service.execute('use', request='接入库', path=self.root, apply=True))
        self.assertFalse(self.cfg.parent.exists())

    def test_identity_mismatch_and_invalid_config(self):
        self.configured()
        value, sha = config.read_config(self.cfg)
        value['vault_id'] = str(uuid.uuid4())
        config.atomic(self.cfg, config.json_bytes(value), sha)
        self.assert_code('VAULT_ID_MISMATCH', lambda: config.resolve_root(config=self.cfg))
        value['schema_version'] = 9
        config.atomic(self.cfg, config.json_bytes(value), config.digest(config.raw_file(self.cfg)))
        self.assert_code('INVALID_CONFIG', lambda: config.read_config(self.cfg))

    def test_uuid_pinned_in_write_lock(self):
        self.init()
        header, sha = memory.Vault(self.root)._header()
        vault = memory.Vault(self.root, expected_vault_id=header['vault_id'])
        header['vault_id'] = str(uuid.uuid4())
        config.atomic(self.root / '_state' / 'vault.json', config.json_bytes(header), sha)
        self.assert_code('VAULT_ID_MISMATCH', lambda: vault.execute('show', request='查看指定虚构条目', entry_id=str(uuid.uuid4())))

    def test_relocate_byte_and_timestamp_integrity(self):
        self.configured()
        self.add()
        source = snapshot(self.root)
        preview = self.service.execute('relocate', request='迁移库', path=self.target)
        before = snapshot(self.base)
        self.service.execute('relocate', request='迁移库', path=self.target)
        self.assertEqual(before, snapshot(self.base))
        result = self.service.execute('relocate', request='迁移库', path=self.target, apply=True, preview_token=preview['preview_token'])
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['source_retained'])
        self.assertEqual(snapshot(self.root), source)
        self.assertEqual(snapshot(self.target), source)
        self.assertEqual(config.read_config(self.cfg)[0]['revision'], 2)
        replay = memory.Vault(self.target).execute('add', request='重试虚构保存', apply=True,
            payload={'title': '虚构偏好', 'body': '虚构中文短语 UI，仅供测试。', 'scope': '虚构项目', 'source': '虚构用户', 'type': 'preference', 'tags': ['测试'], 'request_id': 'fixture-request'})
        self.assertTrue(replay['replayed'])

    def test_relocate_stale_source(self):
        self.configured()
        result = self.add()
        preview = self.service.execute('relocate', request='迁移', path=self.target)
        path = Path(result['path'])
        path.write_bytes(path.read_bytes() + b' changed')
        self.assert_code('STALE_PREVIEW', lambda: self.service.execute('relocate', request='迁移', path=self.target, apply=True, preview_token=preview['preview_token']))
        self.assertFalse(self.target.exists())

    def test_lock_and_logs_block_without_repair(self):
        self.configured()
        lock = self.root / '_state' / 'write.lock'
        lock.write_text('{}')
        before = snapshot(self.base)
        self.assert_code('LOCKED', lambda: self.service.execute('relocate', request='迁移', path=self.target))
        self.assertEqual(before, snapshot(self.base))
        lock.unlink()
        (self.root / '_index' / 'search.sqlite-wal').write_bytes(b'activity')
        before = snapshot(self.base)
        self.assert_code('ACTIVE_DATABASE_LOG', lambda: self.service.execute('relocate', request='迁移', path=self.target))
        self.assertEqual(before, snapshot(self.base))

    def test_pending_and_corrupt_durable_database(self):
        self.configured()
        db = self.root / '_state' / 'operations.sqlite'
        db.write_bytes(b'bad database')
        self.assert_code('OPERATIONS_UNAVAILABLE', lambda: self.service.execute('relocate', request='迁移', path=self.target))
        self.assertEqual(db.read_bytes(), b'bad database')

    def test_stale_navigation_and_index_preserved(self):
        self.configured()
        (self.root / '_index' / 'search.sqlite').unlink()
        (self.root / 'INDEX.md').write_text('手改首页 [link](missing.md)\n', encoding='utf-8')
        before = snapshot(self.root)
        result = self.apply('relocate', self.target)
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['health']['index_stale'])
        self.assertTrue(result['health']['views_stale'])
        self.assertEqual(snapshot(self.target), before)

    def test_reject_overlap_root_and_unsafe_alias(self):
        self.configured()
        for path in [self.root / 'child', self.root.parent, self.cfg.parent / 'vault', self.service.program_root / 'vault', Path(self.root.anchor)]:
            with self.subTest(path=path):
                with self.assertRaises(memory.VaultError):
                    self.service.execute('relocate', request='迁移', path=path)
        self.assert_code('UNSAFE_PATH', lambda: config.directory(self.base / '..' / 'escape'))

    def test_hardlink_refused(self):
        self.init()
        link = self.base / 'alias.json'
        os.link(self.root / '_state' / 'vault.json', link)
        self.assert_code('UNSAFE_PATH', lambda: self.service.execute('use', request='接入库', path=self.root))

    def test_failure_recovery_copy_and_config_phases(self):
        for phase in ['intent', 'file_copied', 'copied', 'verified', 'before_config', 'after_config', 'config_committed']:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory(prefix='虚构故障 ') as temp:
                base = Path(temp)
                cfg, src, dst = base / 'config' / 'config.json', base / 'src', base / 'dst'
                service = location.DirectoryService(cfg)
                memory.Vault(src).execute('init', request='虚构初始化', apply=True)
                preview = service.execute('use', request='接入', path=src)
                service.execute('use', request='接入', path=src, apply=True, preview_token=preview['preview_token'])
                before = snapshot(src)
                fired = []
                def fault(stage, record):
                    if stage == phase and not fired:
                        fired.append(stage)
                        raise OSError('synthetic injected failure')
                service.fault = fault
                preview = service.execute('relocate', request='迁移', path=dst)
                failed = service.execute('relocate', request='迁移', path=dst, apply=True, preview_token=preview['preview_token'])
                self.assertFalse(failed['ok'], failed)
                self.assertEqual(failed['config_applied'], phase in ('after_config', 'config_committed'))
                self.assertTrue(failed['pending_recovery'])
                service.fault = lambda *_: None
                recover_preview = service.execute('recover', request='恢复指定操作', operation_id=failed['operation_id'])
                after_preview = snapshot(base)
                service.execute('recover', request='再查看恢复预览', operation_id=failed['operation_id'])
                self.assertEqual(after_preview, snapshot(base))
                recovered = service.execute('recover', request='恢复指定操作', operation_id=failed['operation_id'], apply=True, preview_token=recover_preview['preview_token'])
                self.assertTrue(recovered['ok'], recovered)
                self.assertEqual(snapshot(src), before)
                self.assertEqual(snapshot(dst), before)

    def failed_copy(self):
        self.configured()
        def fault(stage, record):
            if stage == 'file_copied':
                raise OSError('synthetic pause')
        self.service.fault = fault
        result = self.apply('relocate', self.target)
        self.service.fault = lambda *_: None
        return result

    def test_recovery_refuses_unknown_target_and_source_edit(self):
        failed = self.failed_copy()
        (self.target / 'unknown.txt').write_text('manual', encoding='utf-8')
        self.assert_code('TARGET_CHANGED', lambda: self.service.execute('recover', request='恢复', operation_id=failed['operation_id']))
        (self.target / 'unknown.txt').unlink()
        self.add()
        self.assert_code('SOURCE_CHANGED', lambda: self.service.execute('recover', request='恢复', operation_id=failed['operation_id']))

    def test_creation_failure_configuration_stays_old_then_recovers(self):
        self.configured()
        def fault(stage, record):
            if stage == 'before_config':
                raise OSError('synthetic config publish failure')
        self.service.fault = fault
        failed = self.apply('create', self.target)
        self.assertFalse(failed['ok'])
        self.assertTrue(self.target.is_dir())
        self.assertEqual(config.read_config(self.cfg)[0]['vault_root'], str(self.root))
        self.service.fault = lambda *_: None
        p = self.service.execute('recover', request='恢复创建', operation_id=failed['operation_id'])
        result = self.service.execute('recover', request='恢复创建', operation_id=failed['operation_id'], apply=True, preview_token=p['preview_token'])
        self.assertTrue(result['ok'], result)
        self.assertEqual(config.read_config(self.cfg)[0]['vault_root'], str(self.target))

    def test_cancel_retains_partial_copy(self):
        failed = self.failed_copy()
        self.service.cancelled = lambda: True
        p = self.service.execute('recover', request='恢复', operation_id=failed['operation_id'])
        result = self.service.execute('recover', request='恢复', operation_id=failed['operation_id'], apply=True, preview_token=p['preview_token'])
        self.assertEqual(result['error']['code'], 'CANCELLED')
        self.assertTrue(self.target.exists())
        self.assertEqual(config.read_config(self.cfg)[0]['vault_root'], str(self.root))

    def test_controller_open_select_cancel_no_writes_and_invalidates(self):
        self.init()
        controller = settings.SettingsController(self.service)
        before = snapshot(self.base)
        controller.load('用户打开设置窗口')
        controller.choose(str(self.root), 'use')
        controller.run()
        self.assertTrue(controller.can_apply)
        self.assertEqual(snapshot(self.base), before)
        controller.choose(str(self.target))
        self.assertFalse(controller.can_apply)
        self.assertEqual(snapshot(self.base), before)

    def test_controller_apply_shared_service(self):
        controller = settings.SettingsController(self.service)
        controller.choose(str(self.root), 'create')
        self.assert_code('PREVIEW_REQUIRED', lambda: controller.run(apply=True))
        controller.run()
        result = controller.run(apply=True)
        self.assertTrue(result['ok'], result)
        self.assertEqual(controller.current, str(self.root))
        self.assertFalse(controller.can_apply)

    def test_actual_cli_request_and_config_route(self):
        self.configured()
        saved = self.add()
        process = subprocess.run([sys.executable, '-B', str(SCRIPTS / 'memory.py'), '--config', str(self.cfg),
            'show', '--request', '查看指定虚构记忆', '--id', saved['entry']['id']], capture_output=True, encoding='utf-8')
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result['root_source'], 'explicit_config')
        self.assertEqual(result['vault_root'], str(self.root))

    def test_crashed_process_locks_and_partial_temp_recover_explicitly(self):
        self.configured()
        (self.root / 'history' / 'synthetic-large.bin').write_bytes(b'Synthetic bytes only. ' * 150000)
        before = snapshot(self.root)
        code = """import sys, os
sys.path.insert(0, sys.argv[1])
from vm_location import DirectoryService
def crash(stage, record):
    if stage == 'copy_chunk': os._exit(23)
s = DirectoryService(sys.argv[2], fault=crash)
p = s.execute('relocate', request='虚构进程故障测试迁移', path=sys.argv[3])
s.execute('relocate', request='虚构进程故障测试迁移', path=sys.argv[3], apply=True, preview_token=p['preview_token'])
"""
        process = subprocess.run([sys.executable, '-B', '-c', code, str(SCRIPTS), str(self.cfg), str(self.target)])
        self.assertEqual(process.returncode, 23)
        records = [json.loads(p.read_bytes()) for p in (self.cfg.parent / 'operations').glob('*.json')]
        record = next(r for r in records if r['command'] == 'relocate')
        self.assertEqual(len(record['locks']), 3)
        self.assertTrue(all(Path(x['path']).exists() for x in record['locks']))
        before_preview = snapshot(self.base)
        preview = self.service.execute('recover', request='检查指定故障恢复', operation_id=record['operation_id'])
        self.assertEqual(before_preview, snapshot(self.base))
        recovered = self.service.execute('recover', request='继续指定故障操作', operation_id=record['operation_id'], apply=True, preview_token=preview['preview_token'])
        self.assertTrue(recovered['ok'], recovered)
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(snapshot(self.target), before)

    def test_unknown_or_alive_recovery_lock_stays(self):
        failed = self.failed_copy()
        record = self.service._load(failed['operation_id'])
        lock = self.cfg.parent / 'config.lock'
        owner = record['locks'][0]['owner']
        owner['pid'] = os.getpid()
        owner['process_creation'] = config.process_identity(os.getpid())[1]
        self.service._save(record)
        lock.write_bytes(config.json_bytes(owner))
        before = snapshot(self.base)
        self.assert_code('LOCKED', lambda: self.service.execute('recover', request='检查恢复', operation_id=failed['operation_id']))
        self.assertEqual(before, snapshot(self.base))
        lock.write_bytes(b'unknown lock')
        self.assert_code('LOCKED', lambda: self.service.execute('recover', request='检查恢复', operation_id=failed['operation_id']))
        self.assertEqual(lock.read_bytes(), b'unknown lock')

    def test_v1_selection_never_migrates(self):
        import memory_v1
        memory_v1.Vault(self.root).execute('init', request='虚构 v1 初始化', apply=True)
        before = snapshot(self.root)
        self.assert_code('MIGRATION_REQUIRED', lambda: self.service.execute('use', request='使用目录', path=self.root))
        self.assertEqual(before, snapshot(self.root))

    def test_foreign_config_lock_blocks_apply(self):
        self.init()
        p = self.service.execute('use', request='接入', path=self.root)
        self.cfg.parent.mkdir(parents=True)
        lock = self.cfg.parent / 'config.lock'
        lock.write_text('{}')
        self.assert_code('LOCKED', lambda: self.service.execute('use', request='接入', path=self.root, apply=True, preview_token=p['preview_token']))
        self.assertFalse(self.cfg.exists())
        self.assertEqual(lock.read_text(), '{}')

    def test_module_import_never_reads_user_state(self):
        import importlib
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('IO at import')), patch.object(Path, 'lstat', side_effect=AssertionError('stat at import')):
            importlib.reload(config)

    def test_manual_navigation_links_are_preserved_without_following(self):
        self.configured()
        text = '[external](https://example.invalid/fixture) [outside](../../outside.md)\n'
        (self.root / 'INDEX.md').write_text(text, encoding='utf-8')
        result = self.apply('relocate', self.target)
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['health']['views_stale'])
        self.assertEqual(len(result['health']['navigation_warnings']), 2)
        self.assertEqual((self.target / 'INDEX.md').read_text(encoding='utf-8'), text)

    def test_pending_vault_operation_stops_migration(self):
        self.configured()
        header, _ = memory.Vault(self.root)._header()
        with contextlib.closing(vm_operations.connect(memory.Vault(self.root), header, write=True)) as conn:
            with conn:
                conn.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), str(uuid.uuid4()), 'add', 'fixture request', None, '0' * 64, None, '1' * 64, memory.now(), 'prepared', None, 1, 1))
        self.assert_code('PENDING_VAULT_OPERATION', lambda: self.service.execute('relocate', request='迁移', path=self.target))

    def test_target_creation_during_apply_does_not_get_adopted(self):
        def fault(stage, record):
            if stage == 'initializing':
                memory.Vault(self.root).execute('init', request='另一个虚构创建者', apply=True)
        self.service.fault = fault
        result = self.apply('create')
        self.assertFalse(result['ok'], result)
        self.assertEqual(result['error']['code'], 'NONEMPTY_TARGET')
        self.assertFalse(self.cfg.exists())


if __name__ == '__main__':
    unittest.main()
