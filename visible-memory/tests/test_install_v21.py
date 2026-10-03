"""Code-only install and distributable package tests. All targets are fictional."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import vm_config as config
import vm_install_code as installer
import build_release
import memory


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}


class CodeInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='虚构代码 安装 ')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / 'source'
        self.target = self.base / 'installed'
        self.agents = self.base / 'codex' / 'AGENTS.md'
        self.backup = self.base / 'backups'
        self.source.mkdir()
        self.fake_source = {'SKILL.md': b'---\nname: visible-memory\ndescription: synthetic\n---\n',
                            'README.md': b'Synthetic instructions\n', 'agents/openai.yaml': b'interface: {}\n',
                            'scripts/memory.py': b'# Synthetic new code\n'}
        self.old = {p: b'Old synthetic file ' + p.encode() for p in self.fake_source}
        self.put(self.source, self.fake_source)
        self.put(self.source, {'references/v2-release.json': config.json_bytes({'app': 'visible-memory', 'version': '2.0-test', 'files': installer.hashes(self.old)})})
        self.marker()
        self.agents.parent.mkdir()
        self.agents.write_bytes(b'prefix\r\n<!-- visible-memory:start -->\r\nold\r\n<!-- visible-memory:end -->\r\nsuffix\x00\r\n')

    def put(self, root, values):
        for rel, data in values.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def marker(self):
        values = {p: b for p, b in installer.files(self.source).items() if p != 'release.json'}
        self.put(self.source, {'release.json': config.json_bytes({'app': 'visible-memory', 'version': '2.1.0', 'vault_format': 2,
                          'config_format': 1, 'files': installer.hashes(values)})})

    def call(self, **kwargs):
        return installer.install_code(self.source, self.target, self.agents, self.backup, request='明确安装虚构 Skill', **kwargs)

    def test_preview_no_writes_and_no_vault(self):
        before = snapshot(self.base)
        with patch.object(memory.Vault, 'execute', side_effect=AssertionError('vault call')):
            result = self.call()
        self.assertTrue(result['ok'], result)
        self.assertEqual(before, snapshot(self.base))
        self.assertFalse(self.target.exists())
        self.assertFalse(self.backup.exists())

    def test_new_install_preserves_guide_and_idempotent(self):
        original = self.agents.read_bytes()
        result = self.call(apply=True)
        self.assertTrue(result['ok'], result)
        guide = self.agents.read_bytes()
        self.assertTrue(guide.startswith(b'prefix\r\n'))
        self.assertTrue(guide.endswith(b'\r\nsuffix\x00\r\n'))
        self.assertEqual(guide.count(b'<!-- visible-memory:start -->'), 1)
        self.assertIn(original, snapshot(self.backup).values())
        self.assertFalse((self.backup.parent / 'config.json').exists())
        before = snapshot(self.base)
        self.assertTrue(self.call(apply=True)['unchanged'])
        self.assertEqual(before, snapshot(self.base))

    def test_known_v2_code_upgrade(self):
        self.put(self.target, self.old)
        with patch.object(memory.Vault, '_header', side_effect=AssertionError('vault header read')):
            result = self.call(apply=True, upgrade_code=True)
        self.assertTrue(result['ok'], result)
        self.assertEqual(installer.files(self.target), installer.files(self.source))
        self.assertEqual(snapshot(Path(result['backup_path']) / 'skill'), self.old)

    def test_unknown_or_user_changed_target_refused(self):
        self.put(self.target, self.old)
        (self.target / 'scripts' / 'memory.py').write_bytes(b'Manual modification')
        before = snapshot(self.base)
        result = self.call(apply=True, upgrade_code=True)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'UNRECOGNIZED_INSTALLATION')
        self.assertTrue(result['error']['details'])
        self.assertEqual(before, snapshot(self.base))

    def test_code_failures_roll_back_no_data_or_config_restore(self):
        for stage in ('backed_up', 'file_written', 'agents_written'):
            with self.subTest(stage=stage):
                self.put(self.target, self.old)
                before = self.agents.read_bytes()
                def fault(phase, record):
                    if phase == stage:
                        raise OSError('Injected fictional code failure')
                result = self.call(apply=True, upgrade_code=True, fault=fault)
                self.assertFalse(result['ok'], result)
                self.assertTrue(result['rolled_back'], result)
                self.assertEqual(installer.files(self.target), self.old)
                self.assertEqual(self.agents.read_bytes(), before)

    def test_stale_preview_source_changed(self):
        preview = self.call()
        self.put(self.source, {'scripts/memory.py': b'Changed synthetic code'})
        self.marker()
        result = self.call(apply=True, preview_token=preview['preview_token'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'STALE_PREVIEW')
        self.assertFalse(self.target.exists())

    def test_cli_defaults_without_vault(self):
        with patch.dict(os.environ, {'CODEX_HOME': str(self.base / 'custom-codex'), 'LOCALAPPDATA': str(self.base / 'local')}), patch.object(Path, 'home', return_value=self.base):
            source, target, agents, backup = installer.defaults(source=self.source)
        self.assertEqual(target, self.base / '.agents' / 'skills' / 'visible-memory')
        self.assertEqual(agents, self.base / 'custom-codex' / 'AGENTS.md')
        self.assertFalse(target.exists())

    def test_builder_whitelist_and_portable_extraction(self):
        self.put(self.source, {'scripts/install.py': b'# fictional installer', 'tests/test_fake.py': b'# fictional tests'})
        output = self.base / 'package'
        bundle = build_release.build(self.source, output)
        self.assertTrue(bundle['ok'])
        with zipfile.ZipFile(bundle['archive']) as archive:
            self.assertEqual(archive.testzip(), None)
            self.assertFalse(any(p.endswith(('.sqlite', '.pyc')) or '/_state/' in p for p in archive.namelist()))
            archive.extractall(self.base / 'different user' / 'unpacked')
        portable = self.base / 'different user' / 'unpacked' / 'visible-memory'
        self.assertEqual(installer.files(portable), installer.files(self.source))
        self.assertEqual(hashlib.sha256(Path(bundle['archive']).read_bytes()).hexdigest(), bundle['sha256'])

    def test_builder_refuses_data_file_in_code_folder(self):
        self.put(self.source, {'scripts/private.sqlite': b'personal data fixture'})
        with self.assertRaises(memory.VaultError) as caught:
            build_release.build(self.source, self.base / 'package')
        self.assertEqual(caught.exception.code, 'NON_RELEASE_FILE')

    def test_wrong_install_target_never_reads_vault(self):
        memory.Vault(self.target).execute('init', request='初始化虚构库作为错误安装目标', apply=True)
        before = snapshot(self.target)
        original = config.raw_file
        def track(path):
            if self.target == Path(path) or self.target in Path(path).parents:
                raise AssertionError('Code installer tried to read vault content')
            return original(path)
        with patch.object(config, 'raw_file', track):
            result = self.call(apply=True, upgrade_code=True)
        self.assertFalse(result['ok'], result)
        self.assertEqual(result['error']['code'], 'UNOWNED_TARGET')
        self.assertEqual(snapshot(self.target), before)

    def test_process_crash_code_restore_only(self):
        self.put(self.target, self.old)
        guide = self.agents.read_bytes()
        code = """import sys, os
sys.path.insert(0, sys.argv[1])
from vm_install_code import install_code
def crash(stage, record):
    if stage == 'file_written': os._exit(29)
install_code(*sys.argv[2:6], request='虚构代码升级故障测试', apply=True, upgrade_code=True, fault=crash)
"""
        process = subprocess.run([sys.executable, '-B', '-c', code, str(Path(__file__).parents[1] / 'scripts'),
                                  str(self.source), str(self.target), str(self.agents), str(self.backup)])
        self.assertEqual(process.returncode, 29)
        journal = next(self.backup.glob('code-*/operation.json'))
        record = json.loads(journal.read_bytes())
        before = snapshot(self.base)
        preview = installer.recover_code(self.backup, record['operation_id'], request='查看中断升级恢复')
        self.assertEqual(before, snapshot(self.base))
        recovered = installer.recover_code(self.backup, record['operation_id'], request='恢复中断升级', apply=True)
        self.assertTrue(recovered['ok'], recovered)
        self.assertEqual(installer.files(self.target), self.old)
        self.assertEqual(self.agents.read_bytes(), guide)


if __name__ == '__main__':
    unittest.main()
