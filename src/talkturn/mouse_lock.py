"""Freeze the physical mouse while a TalkTurn connection is in use.

Physical Esc held alone for HOLD seconds frees the mouse and sends `talkturn out`.
Injected input (TalkTurn's own Kakao automation) always passes. The hooks die with
this process, so a crash can never leave the mouse locked.
"""
import ctypes
from ctypes import wintypes
import os
import subprocess
import sys
import time
from . import runtime

HOLD = 2.5             # seconds of continuous physical Esc
TAP = 0.4              # a shorter press is an ordinary Esc for the app; a longer aborted hold is dropped
REPEAT_GAP = 1.5       # longer than any typematic delay: silence means the key-up was missed
REARM = 2.0            # Windows silently drops a low-level hook whose callback once ran late
KEY = 0x1B             # VK_ESCAPE
KEY_INJECTED = 0x10    # LLKHF_INJECTED
MOUSE_INJECTED = 0x01  # LLMHF_INJECTED
MUTEX = 'Local\\talkturn-mouse-lock'  # one physical mouse, one lock
NOTICE = '마우스 잠금 · Esc 길게(2.5초) 누르면 해제 + talkturn out'


class EscHold:
    """A short press is replayed to apps on release; a continuous hold fires once."""

    def __init__(self, hold=HOLD, gap=REPEAT_GAP, tap=TAP):
        self.hold, self.gap, self.tap = hold, gap, tap
        self.since = self.last = None
        self.fired = False

    def down(self, now):
        if self.since is None:
            self.since = now
        self.last = now

    def up(self, now):
        """Return whether the swallowed press was a tap that apps should still receive."""
        tapped = not self.fired and self.since is not None and now - self.since < self.tap
        self.since = self.last = None
        return tapped

    def poll(self, now):
        """Return the held fraction 0..1 and latch `fired` at 1."""
        if self.since is None:
            return 0.0
        if now - self.last > self.gap:  # e.g. released on the lock screen, where hooks see nothing
            self.since = self.last = None
            return 0.0
        fraction = min(1.0, (now - self.since) / self.hold)
        self.fired = self.fired or fraction >= 1.0
        return fraction


class MSLL(ctypes.Structure):
    _fields_ = [('pt', wintypes.POINT), ('mouseData', wintypes.DWORD), ('flags', wintypes.DWORD),
                ('time', wintypes.DWORD), ('dwExtraInfo', ctypes.c_size_t)]


class KBDLL(ctypes.Structure):
    _fields_ = [('vkCode', wintypes.DWORD), ('scanCode', wintypes.DWORD), ('flags', wintypes.DWORD),
                ('time', wintypes.DWORD), ('dwExtraInfo', ctypes.c_size_t)]


# Private library objects: 64-bit handles need exact prototypes, and other modules keep theirs.
if sys.platform == 'win32':
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    gdi32 = ctypes.WinDLL('gdi32')
    LRESULT = ctypes.c_ssize_t
    HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
    user32.SetWindowsHookExW.restype = wintypes.HHOOK
    user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    user32.CallNextHookEx.restype = LRESULT
    user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
    user32.SetTimer.restype = ctypes.c_size_t
    user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    gdi32.CreateFontW.argtypes = [ctypes.c_int] * 13 + [wintypes.LPCWSTR]
    gdi32.CreateFontW.restype = ctypes.c_void_p
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def active():
    if sys.platform == 'darwin':
        from . import mac_kakao
        return mac_kakao.lock_active()
    handle = kernel32.OpenMutexW(0x00100000, False, MUTEX)
    if handle:
        kernel32.CloseHandle(handle)
    return bool(handle)


def engage():
    """Lock the mouse for this home's running receiver; return whether the lock is held."""
    from .service import process_running
    try:
        pid = int((runtime.ROOT / 'reply-bridge.pid').read_text(encoding='utf-8'))
    except (FileNotFoundError, ValueError):
        return False
    if not process_running(pid):
        return False
    if not active():
        python = runtime.PYTHON.with_name('pythonw.exe')
        with (runtime.ROOT / 'bridge-stderr.log').open('ab') as log:
            subprocess.Popen([str(python if python.exists() else runtime.PYTHON), '-m', 'talkturn.mouse_lock', str(pid)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
                             env=dict(os.environ, TALKTURN_HOME=str(runtime.ROOT)), creationflags=runtime.NO_WINDOW,
                             **({'start_new_session': True} if sys.platform == 'darwin' else {}))
    return True


def _send_out():
    return subprocess.Popen([sys.executable, '-c', 'from talkturn.service import disconnect; disconnect()'],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=runtime.NO_WINDOW)


def _modifier_down():
    return any(user32.GetAsyncKeyState(key) & 0x8000 for key in (0x10, 0x11, 0x12, 0x5B, 0x5C))


def _banner():
    user32.SetProcessDPIAware()
    scale = user32.GetDpiForSystem() / 96
    width, height = int(600 * scale), int(34 * scale)
    # Topmost tool window that never takes focus: the terminal keeps the keyboard.
    window = user32.CreateWindowExW(0x08000088, 'STATIC', '', 0x80800201,
                                    (user32.GetSystemMetrics(0) - width) // 2, 0, width, height, None, None, None, None)
    font = gdi32.CreateFontW(-int(15 * scale), 0, 0, 0, 600, 0, 0, 0, 1, 0, 0, 0, 0, 'Malgun Gothic')
    user32.SendMessageW(window, 0x30, font or 0, 1)  # WM_SETFONT
    user32.ShowWindow(window, 4)                     # SW_SHOWNOACTIVATE
    return window


def run(bridge_pid):
    if sys.platform == 'darwin':
        from . import mac_kakao
        return mac_kakao.lock(bridge_pid)
    from .service import process_running
    ctypes.set_last_error(0)
    mutex = kernel32.CreateMutexW(None, False, MUTEX)
    if ctypes.get_last_error() == 183:
        kernel32.CloseHandle(mutex)
        return
    esc = EscHold()
    state = {'locked': True, 'tap': False, 'shown': None}
    out = None      # the `talkturn out` child once Esc fired
    failed = 0.0
    banner = timer = None

    @HOOKPROC
    def on_mouse(code, wparam, lparam):
        if code == 0 and state['locked'] and not MSLL.from_address(lparam).flags & MOUSE_INJECTED:
            return 1
        return user32.CallNextHookEx(None, code, wparam, lparam)

    @HOOKPROC
    def on_key(code, wparam, lparam):
        if code == 0:
            key = KBDLL.from_address(lparam)
            if key.vkCode == KEY and not key.flags & KEY_INJECTED:
                if wparam in (0x100, 0x104):  # key down, including typematic repeats
                    if esc.since is not None or not _modifier_down():  # leave Ctrl+Shift+Esc alone
                        esc.down(time.monotonic())
                        return 1
                elif esc.since is not None:
                    state['tap'] = esc.up(time.monotonic())
                    return 1
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def show(text):
        if text != state['shown']:
            state['shown'] = text
            user32.SetWindowTextW(banner, text)

    def rearm(kind, callback, old):
        fresh = user32.SetWindowsHookExW(kind, callback, module, 0)  # new first: no unhooked gap
        if not fresh:
            return old
        user32.UnhookWindowsHookEx(old)
        return fresh

    def tick():
        nonlocal mouse_hook, key_hook, out, failed, armed
        now = time.monotonic()
        if state['tap']:  # replay the swallowed short press; injected, so it passes the hook
            state['tap'] = False
            user32.keybd_event(KEY, 0, 0, 0)
            user32.keybd_event(KEY, 0, 2, 0)
        held = esc.poll(now)
        if out is None:
            if not (runtime.ROOT / 'reply-enabled').exists() or not process_running(bridge_pid):
                return False  # out/stop elsewhere or a dead receiver: never keep the lock alone
            if now - armed > REARM:  # a dropped key hook would leave the lock without its Esc exit
                armed = now
                mouse_hook = rearm(14, on_mouse, mouse_hook)
                key_hook = rearm(13, on_key, key_hook)
            if not esc.fired:
                filled = int(held * 10)
                show(f'Esc 누르는 중 {"■" * filled}{"□" * (10 - filled)} 떼면 취소' if held
                     else 'TalkTurn 사용 중 · ' + NOTICE)
                return True
            state['locked'] = False
            user32.UnhookWindowsHookEx(mouse_hook)
            mouse_hook = None
            show('마우스 해제됨 · talkturn out 보내는 중…')
            out = _send_out()
            return True
        if key_hook and esc.since is None:  # Esc is up: stop swallowing it
            user32.UnhookWindowsHookEx(key_hook)
            key_hook = None
        if key_hook or out.poll() is None:
            return True
        if out.returncode and not failed:
            failed = now
            show('talkturn out 전송 실패 · 수신은 종료됨')
        return bool(failed) and now - failed < 3

    module = kernel32.GetModuleHandleW(None)
    mouse_hook = user32.SetWindowsHookExW(14, on_mouse, module, 0)  # WH_MOUSE_LL
    key_hook = user32.SetWindowsHookExW(13, on_key, module, 0)      # WH_KEYBOARD_LL
    armed = time.monotonic()
    try:
        if not mouse_hook or not key_hook:
            raise ctypes.WinError(ctypes.get_last_error())  # never lock the mouse without the Esc exit
        banner = _banner()
        timer = user32.SetTimer(None, 0, 50, None)
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.DispatchMessageW(ctypes.byref(message))
            if message.message == 0x113 and not tick():  # WM_TIMER
                break
    finally:
        for hook in (mouse_hook, key_hook):
            if hook:
                user32.UnhookWindowsHookEx(hook)
        if timer:
            user32.KillTimer(None, timer)
        if banner:
            user32.DestroyWindow(banner)
        kernel32.CloseHandle(mutex)


if __name__ == '__main__':
    run(int(sys.argv[1]))
