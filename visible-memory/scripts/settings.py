#!/usr/bin/env python3
"""On-demand directory settings. No startup wizard, no plugin, no background work."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import queue
import sys
import threading

from memory_v1 import VaultError
import vm_config
from vm_location import DirectoryService


def error_result(exc):
    return {'ok': False, 'applied': False, 'error': {'code': getattr(exc, 'code', 'IO_OR_INPUT_ERROR'),
            'message': str(exc), 'details': getattr(exc, 'details', None)}}


class SettingsController:
    """UI state without Tk dependencies, using exactly the CLI service."""
    def __init__(self, service):
        self.service = service
        self.path = ''
        self.action = ''
        self.preview = None
        self.busy = False
        self.current = None

    def load(self, request):
        result = self.service.execute('get', request=request)
        self.current = (result.get('config') or {}).get('vault_root')
        return result

    def choose(self, path=None, action=None):
        if self.busy:
            raise VaultError('BUSY', '窗口正在执行一个目录操作。')
        if path is not None:
            self.path = path
        if action is not None:
            self.action = action
        self.preview = None

    @property
    def can_apply(self):
        return not self.busy and bool(self.preview and self.preview.get('ok') and self.preview.get('preview_token'))

    def run(self, *, apply=False):
        if self.busy:
            raise VaultError('BUSY', '同一窗口同时只能执行一个操作。')
        if not self.action or not self.path.strip():
            raise VaultError('SELECTION_REQUIRED', '请选择目录操作并输入目标目录，再点击预览。')
        if apply and not self.can_apply:
            raise VaultError('PREVIEW_REQUIRED', '应用前需要有效预览。')
        labels = {'use': '使用已有库', 'create': '创建空库', 'relocate': '迁移当前库'}
        request = f'用户在目录设置窗口明确选择「{labels[self.action]}」：{self.path}'
        token = self.preview['preview_token'] if apply else None
        self.busy = True
        try:
            result = self.service.execute(self.action, request=request, path=self.path, apply=apply, preview_token=token)
            if apply:
                self.preview = None
                if result.get('current_directory'):
                    self.current = result['current_directory']
            else:
                self.preview = result if result.get('ok') else None
            return result
        finally:
            self.busy = False


class SettingsWindow:
    def __init__(self, root, service, request):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk, self.root = tk, ttk, root
        self.controller = SettingsController(service)
        self.events = queue.Queue()
        self.cancel_event = threading.Event()
        self.running = False
        self.close_pending = False
        self.last_result = None
        self.folder_dialog = None  # Tests may inject the native chooser binding.
        service.progress = lambda value: self.events.put(('progress', value))
        service.cancelled = self.cancel_event.is_set
        root.title('可见记忆管理 — 目录设置')
        root.geometry('780x610')
        root.minsize(660, 520)
        frame = ttk.Frame(root, padding=18)
        frame.pack(fill='both', expand=True)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text='当前记忆目录').grid(row=0, column=0, sticky='w', pady=(0, 10))
        self.current = tk.StringVar(value='未配置')
        ttk.Entry(frame, textvariable=self.current, state='readonly').grid(row=0, column=1, columnspan=2, sticky='ew', pady=(0, 10))
        ttk.Label(frame, text='新目录').grid(row=1, column=0, sticky='w')
        self.path = tk.StringVar()
        self.entry = ttk.Entry(frame, textvariable=self.path)
        self.entry.grid(row=1, column=1, sticky='ew', padx=(0, 10))
        self.browse_button = ttk.Button(frame, text='选择文件夹…', command=self.browse)
        self.browse_button.grid(row=1, column=2)
        actions = ttk.LabelFrame(frame, text='选择操作', padding=12)
        actions.grid(row=2, column=0, columnspan=3, sticky='ew', pady=14)
        self.action = tk.StringVar()
        self.radios = []
        for col, (value, label) in enumerate([('use', '使用已有库'), ('create', '创建空库'), ('relocate', '迁移当前库')]):
            button = ttk.Radiobutton(actions, text=label, value=value, variable=self.action)
            button.grid(row=0, column=col, padx=(0, 24), sticky='w')
            self.radios.append(button)
        ttk.Label(frame, text='选择目录不会立即更改配置。请先预览，再应用。迁移成功后原目录仍保留。', wraplength=700).grid(row=3, column=0, columnspan=3, sticky='w')
        controls = ttk.Frame(frame)
        controls.grid(row=4, column=0, columnspan=3, sticky='ew', pady=14)
        self.preview_button = ttk.Button(controls, text='预览操作', command=lambda: self.start(False))
        self.preview_button.pack(side='left')
        self.apply_button = ttk.Button(controls, text='应用', command=lambda: self.start(True), state='disabled')
        self.apply_button.pack(side='left', padx=10)
        self.cancel_button = ttk.Button(controls, text='取消 / 关闭', command=self.cancel)
        self.cancel_button.pack(side='right')
        self.status = tk.StringVar(value='仅在你预览或应用指定操作时检查记忆目录。')
        ttk.Label(frame, textvariable=self.status, wraplength=700).grid(row=5, column=0, columnspan=3, sticky='w')
        self.bar = ttk.Progressbar(frame, mode='determinate')
        self.bar.grid(row=6, column=0, columnspan=3, sticky='ew', pady=(8, 12))
        self.output = tk.Text(frame, wrap='word', height=14, state='disabled')
        self.output.grid(row=7, column=0, columnspan=3, sticky='nsew')
        frame.rowconfigure(7, weight=1)
        self.path.trace_add('write', self.invalidate)
        self.action.trace_add('write', self.invalidate)
        root.protocol('WM_DELETE_WINDOW', self.cancel)
        try:
            result = self.controller.load(request)
            self.current.set(self.controller.current or '未配置')
            self.render({'说明': '打开窗口仅查看用户目录配置，不加载任何记忆正文。', '配置位置': result['config_path']})
        except (VaultError, OSError, ValueError) as exc:
            self.status.set('配置无法读取，请查看错误。')
            self.render(error_result(exc))
        root.after(80, self.poll)

    def render(self, value):
        if '说明' in value:
            text = '\n\n'.join(f'{key}：{item}' for key, item in value.items())
        elif value.get('preview'):
            summary = value.get('summary', {})
            labels = {'use': '使用已有库', 'create': '创建空库', 'relocate': '迁移当前库'}
            lines = ['预览操作：' + labels.get(summary.get('command'), '恢复目录操作'),
                     '当前目录：' + (summary.get('current_directory') or '未配置'),
                     '目标目录：' + summary.get('target_directory', '')]
            if summary.get('source_retained'):
                lines += [f"将复制 {summary.get('files', 0)} 个文件，共 {summary.get('bytes', 0) / (1024 * 1024):.2f} MiB。",
                          '副本验证通过后切换目录，原目录保留。']
            elif summary.get('command') == 'create':
                lines.append('创建空记忆库，成功后设为当前目录。')
            else:
                lines.append('接入该已有库；不会读取记忆列表或自动刷新索引。')
            health = summary.get('health') or {}
            if health.get('index_stale') or health.get('views_stale'):
                lines.append('已有索引或导航待刷新状态将原样保留。')
            lines.append('检查以上内容后，点击“应用”提交。')
            text = '\n\n'.join(lines)
        else:
            lines = ['操作完成。' if value.get('ok') else '操作已停止。']
            if value.get('current_directory'):
                lines.append('当前实际目录：' + value['current_directory'])
            if value.get('source_retained'):
                lines.append('原目录保留。')
            if value.get('total_files'):
                lines.append(f"已复制：{value.get('copied_files', 0)} / {value['total_files']} 个文件。")
            if value.get('data_verified') is not None:
                lines.append('库验证：' + ('已完成' if value['data_verified'] else '尚未完成'))
            if value.get('config_applied') is not None:
                lines.append('目录配置：' + ('已切换' if value['config_applied'] else '尚未切换'))
            if value.get('error'):
                lines.append('原因：' + value['error']['message'])
            if value.get('pending_recovery'):
                lines += ['操作尚需恢复；已创建文件保留。', '恢复编号：' + value.get('operation_id', ''),
                          '恢复记录：' + value.get('record_path', '')]
            text = '\n\n'.join(lines)
        self.output.configure(state='normal')
        self.output.delete('1.0', 'end')
        self.output.insert('end', text)
        self.output.configure(state='disabled')

    def invalidate(self, *_):
        if self.running:
            return
        self.controller.choose(self.path.get(), self.action.get())
        self.apply_button.configure(state='disabled')
        self.status.set('目录或操作已改变，需要重新预览。')

    def browse(self):
        if self.running:
            return
        from tkinter import filedialog
        chooser = self.folder_dialog or filedialog.askdirectory
        selected = chooser(parent=self.root, title='选择记忆目录', mustexist=False)
        if selected:
            self.path.set(selected)

    def _enabled(self, value):
        state = 'normal' if value else 'disabled'
        for widget in [self.entry, self.browse_button, self.preview_button, *self.radios]:
            widget.configure(state=state)
        self.apply_button.configure(state='normal' if value and self.controller.can_apply else 'disabled')

    def start(self, apply):
        if self.running:
            return
        self.controller.choose(self.path.get(), self.action.get()) if not apply else None
        if not self.controller.action or not self.controller.path.strip() or (apply and not self.controller.can_apply):
            self.status.set('请选择操作和目录，完成预览后才能应用。')
            return
        self.running = True
        self.cancel_event.clear()
        self._enabled(False)
        self.status.set('正在应用…' if apply else '正在预览…')
        self.bar.configure(mode='indeterminate')
        self.bar.start(25)

        def worker():
            try:
                value = self.controller.run(apply=apply)
            except (VaultError, OSError, ValueError, sqlite_error()) as exc:
                value = error_result(exc)
            self.events.put(('result', value))
        threading.Thread(target=worker, daemon=False, name='visible-memory-settings').start()

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'progress':
                    self.bar.stop()
                    self.bar.configure(mode='determinate', maximum=max(1, value.get('total', 1)), value=value.get('completed', 0))
                    self.status.set(f"正在复制：{value.get('completed', 0)} / {value.get('total', 0)}")
                else:
                    self.last_result = value
                    self.running = False
                    self.bar.stop()
                    self._enabled(True)
                    self.current.set(self.controller.current or '未配置')
                    self.render(value)
                    if value.get('preview'):
                        self.status.set('预览完成；检查摘要后可点击应用。')
                    elif value.get('ok'):
                        self.status.set('操作完成。原目录保留。' if value.get('source_retained') else '操作完成。')
                    else:
                        self.status.set('操作停止。请查看当前目录、完成阶段和恢复编号。')
                    if self.close_pending:
                        self.root.destroy()
                        return
        except queue.Empty:
            pass
        self.root.after(80, self.poll)

    def cancel(self):
        if self.running:
            self.cancel_event.set()
            # Keep the receipt visible; close only after the safe cancellation ends.
            self.close_pending = False
            self.status.set('正在等待安全取消点；已复制文件会保留。')
        else:
            self.root.destroy()


def sqlite_error():
    import sqlite3
    return sqlite3.Error


def gui(service, request):
    vm_config.require_request(request)
    try:
        import tkinter as tk
        root = tk.Tk()
    except (ImportError, RuntimeError) as exc:
        raise VaultError('GUI_UNAVAILABLE', f'Tkinter 不可用；请使用 settings.py 的 CLI：{exc}') from None
    except Exception as exc:
        # TclError is unavailable when importing tkinter itself failed.
        raise VaultError('GUI_UNAVAILABLE', f'目录窗口无法打开；CLI 仍可用：{exc}') from None
    window = SettingsWindow(root, service, request)
    root.mainloop()
    return {**(window.last_result or {'ok': True, 'applied': False}), 'window_closed': True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('gui', 'get', 'doctor', 'use', 'create', 'relocate', 'recover'):
        item = sub.add_parser(name)
        item.add_argument('--request', required=True)
        if name in ('use', 'create', 'relocate', 'recover'):
            item.add_argument('--apply', action='store_true')
            item.add_argument('--preview-token')
        if name in ('use', 'create', 'relocate'):
            item.add_argument('--path', required=True)
        if name == 'recover':
            item.add_argument('--operation-id', required=True)
    args = vars(parser.parse_args(argv))
    try:
        vm_config.require_request(args['request'])
        service = DirectoryService(args.pop('config'))
        result = gui(service, args['request']) if args['command'] == 'gui' else service.execute(**args)
    except (VaultError, OSError, ValueError, sqlite_error()) as exc:
        result = error_result(exc)
    print(json.dumps({**result, 'program_version': vm_config.VERSION}, ensure_ascii=False, indent=2))
    return 0 if result.get('ok') else 1


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
