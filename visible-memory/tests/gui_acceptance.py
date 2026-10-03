"""Opt-in real Windows window/chooser acceptance. Uses an isolated fictional vault.

Run: python -B tests/gui_acceptance.py --output <report directory>
Screenshots use Pillow if already present in the test runtime, never a runtime dependency.
Only native dialogs belonging to this test process are operated.
"""
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import settings
from vm_location import DirectoryService


def snapshot(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def main():
    import tkinter as tk
    from tkinter import filedialog
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user.FindWindowW.restype = wintypes.HWND
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.GetDlgItem.argtypes = [wintypes.HWND, ctypes.c_int]
    user.GetDlgItem.restype = wintypes.HWND
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    report = {'ok': False, 'checks': [], 'manual_human_acceptance': False, 'native_window_tested': False, 'native_chooser_tested': False, 'screenshots': []}
    events = queue.Queue()
    try:
        from PIL import ImageGrab
    except ImportError:
        ImageGrab = None

    def capture(hwnd, name):
        if ImageGrab is None:
            return
        rect = wintypes.RECT()
        if user.GetWindowRect(hwnd, ctypes.byref(rect)):
            path = args.output / name
            ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom), all_screens=True).save(path)
            report['screenshots'].append(str(path.absolute()))

    with tempfile.TemporaryDirectory(prefix='visible-memory GUI 虚构验收 ') as temp:
        base = Path(temp)
        cfg = base / 'isolated config' / 'config.json'
        chosen = base / '虚构记忆 目录'
        chosen.mkdir()
        before = snapshot(base)
        root = tk.Tk()
        window = settings.SettingsWindow(root, DirectoryService(cfg), '用户明确测试目录设置窗口（虚构库）')

        def check(value, label):
            if not value:
                raise AssertionError(label)
            report['checks'].append(label)

        def automate_dialog(cancel=False):
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                hwnd = user.FindWindowW(None, '选择记忆目录')
                pid = wintypes.DWORD()
                if hwnd:
                    user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if hwnd and pid.value == os.getpid():
                    time.sleep(.4)
                    capture(hwnd, 'folder-picker-cancel.png' if cancel else 'folder-picker-select.png')
                    if cancel:
                        user.PostMessageW(hwnd, 0x0010, 0, 0)
                    else:
                        button = user.GetDlgItem(hwnd, 1)
                        if button:
                            user.PostMessageW(button, 0x00F5, 0, 0)  # BM_CLICK
                        else:
                            user.PostMessageW(hwnd, 0x0111, 1, 0)  # IDOK
                    events.put(('dialog', 'cancel' if cancel else 'select'))
                    return
                time.sleep(.05)
            events.put(('error', 'Native chooser not found in this test process'))

        stage = {'value': 0}
        deadline = time.monotonic() + 28

        def drive():
            try:
                if time.monotonic() > deadline:
                    raise TimeoutError('GUI acceptance timed out')
                if stage['value'] == 0:
                    check(root.title() == '可见记忆管理 — 目录设置', 'Actual window title and layout initialized')
                    check(snapshot(base) == before, 'Opening window reads config only, creates no files')
                    report['native_window_tested'] = True
                    hwnd = user.FindWindowW(None, root.title())
                    capture(hwnd, 'settings-unconfigured.png')
                    window.folder_dialog = lambda **kwargs: filedialog.askdirectory(initialdir=str(chosen), **kwargs)
                    threading.Thread(target=automate_dialog, daemon=True).start()
                    window.browse_button.invoke()
                    check(Path(window.path.get()) == chosen, 'Native folder selection populates directory field')
                    check(snapshot(base) == before, 'Folder selection makes no persistent changes')
                    report['native_chooser_tested'] = True
                    window.radios[1].invoke()
                    window.preview_button.invoke()
                    stage['value'] = 1
                elif stage['value'] == 1 and not window.running:
                    check(window.controller.can_apply, 'Preview enables Apply after shared-service validation')
                    check(snapshot(base) == before, 'Preview leaves filesystem unchanged')
                    capture(user.FindWindowW(None, root.title()), 'settings-preview.png')
                    window.path.set(str(base / 'different directory'))
                    check(not window.controller.can_apply, 'Editing directory invalidates preview and Apply')
                    window.path.set(str(chosen))
                    window.preview_button.invoke()
                    stage['value'] = 2
                elif stage['value'] == 2 and not window.running:
                    check(window.controller.can_apply, 'Re-preview restores valid Apply')
                    window.apply_button.invoke()
                    stage['value'] = 3
                elif stage['value'] == 3 and not window.running:
                    check(bool(window.last_result and window.last_result.get('ok')), 'Apply succeeds using shared directory service')
                    check(window.current.get() == str(chosen), 'Window displays actual configured directory')
                    check(list((chosen / 'entries').iterdir()) == [], 'Created vault is empty and fictional')
                    saved = window.path.get()
                    after_apply = snapshot(base)
                    threading.Thread(target=lambda: automate_dialog(True), daemon=True).start()
                    window.browse_button.invoke()
                    check(window.path.get() == saved, 'Native chooser cancellation leaves directory field unchanged')
                    check(snapshot(base) == after_apply, 'Native chooser cancellation makes no changes')
                    capture(user.FindWindowW(None, root.title()), 'settings-complete.png')
                    report['ok'] = True
                    root.destroy()
                    return
                root.after(150, drive)
            except Exception as exc:
                report['error'] = str(exc)
                root.destroy()

        root.after(500, drive)
        root.mainloop()
    while not events.empty():
        kind, value = events.get()
        if kind == 'error':
            report.update(ok=False, error=value)
    (args.output / 'gui-acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
