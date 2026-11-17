"""Adapted from kronenz/kakaotalk-mcp (MIT). Only paired-window delivery helpers retained."""

import time, ctypes, subprocess
from typing import Optional, List, Dict
import win32gui, win32clipboard, win32con
from . import config
_user32 = ctypes.windll.user32

def find_chat_window(room_name: str) -> Optional[int]:
    """Find a chat window by exact title (room name).

    Returns hwnd or None.
    """
    results: List[int] = []

    def _cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            if cls == config.KAKAO_CHAT_WINDOW_CLASS and title == room_name:
                results.append(hwnd)
        return True

    win32gui.EnumWindows(_cb, None)
    return results[0] if results else None

def list_chat_windows() -> List[Dict]:
    """List all currently open KakaoTalk chat windows.

    Returns list of dicts with hwnd and title.
    """
    main_hwnd = win32gui.FindWindow(
        config.KAKAO_MAIN_WINDOW_CLASS, config.KAKAO_MAIN_WINDOW_TITLE
    )
    windows: List[Dict] = []

    def _cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            if (
                cls == config.KAKAO_CHAT_WINDOW_CLASS
                and hwnd != main_hwnd
                and title
                and title != config.KAKAO_MAIN_WINDOW_TITLE
            ):
                windows.append({"hwnd": hwnd, "title": title})
        return True

    win32gui.EnumWindows(_cb, None)
    return windows

def find_child_window_recursive(parent_hwnd: int, class_name: str) -> Optional[int]:
    """Recursively search for a child window by class name.

    Needed because RICHEDIT50W may be nested several levels deep.
    Returns hwnd or None.
    """
    found: List[int] = []

    def _cb(hwnd, _):
        if win32gui.GetClassName(hwnd) == class_name:
            found.append(hwnd)
            return False  # stop enumeration
        return True

    try:
        win32gui.EnumChildWindows(parent_hwnd, _cb, None)
    except Exception:
        pass
    return found[0] if found else None

def bring_window_to_front(hwnd: int):
    """Restore and bring a window to the foreground.

    Uses ctypes directly (not pywin32) to avoid exceptions on failure,
    and simulates Alt keypress to bypass Windows foreground restrictions.
    """
    VK_MENU = 0x12  # Alt key
    SW_SHOW = 5
    SW_RESTORE = 9
    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_SHOWWINDOW = 0x0040

    _user32.ShowWindow(hwnd, SW_RESTORE)

    # Simulate Alt keypress to unlock SetForegroundWindow
    _user32.keybd_event(VK_MENU, 0, 0, 0)
    _user32.keybd_event(VK_MENU, 0, config.KEYEVENTF_KEYUP, 0)

    # Use ctypes SetForegroundWindow (returns 0 on fail, no exception)
    _user32.SetForegroundWindow(hwnd)

    # Also temporarily set topmost to ensure visibility
    _user32.SetWindowPos(
        hwnd, HWND_TOPMOST, 0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
    )
    _user32.SetWindowPos(
        hwnd, HWND_NOTOPMOST, 0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
    )

def _send_ctrl_key_combo(vk_key: int):
    """Send Ctrl+<key> combo using keybd_event (requires foreground focus)."""
    _user32.keybd_event(config.VK_CONTROL, 0, 0, 0)
    time.sleep(0.02)
    _user32.keybd_event(vk_key, 0, 0, 0)
    time.sleep(0.02)
    _user32.keybd_event(vk_key, 0, config.KEYEVENTF_KEYUP, 0)
    time.sleep(0.02)
    _user32.keybd_event(config.VK_CONTROL, 0, config.KEYEVENTF_KEYUP, 0)

def _read_clipboard_text(max_retries: int = None, interval_sec: float = None) -> str:
    """Read text from clipboard with retry logic."""
    if max_retries is None:
        max_retries = config.CLIPBOARD_MAX_RETRIES
    if interval_sec is None:
        interval_sec = config.CLIPBOARD_RETRY_INTERVAL_SEC

    for _ in range(max_retries):
        try:
            win32clipboard.OpenClipboard()
            try:
                data = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                return data if data else ""
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            time.sleep(interval_sec)
    return ""

def send_message_to_room(room_name: str, text: str) -> Dict:
    """Send a text message to a KakaoTalk chat room.

    Pastes text via clipboard into RICHEDIT50W and sends with keybd_event Enter.
    NOTE: This briefly brings the chat window to the foreground.

    Returns:
        Dict with success (bool) and message or error.
    """
    hwnd = find_chat_window(room_name)
    if hwnd is None:
        return {"success": False, "error": f"Chat window '{room_name}' not found"}

    edit_hwnd = find_child_window_recursive(hwnd, config.KAKAO_EDIT_CLASS)
    if edit_hwnd is None:
        return {"success": False, "error": f"Edit control not found in '{room_name}'"}

    # Bring window to foreground and focus the edit control
    bring_window_to_front(hwnd)
    time.sleep(config.WINDOW_ACTIVATE_WAIT_SEC)

    # Click on the edit control to ensure focus
    try:
        rect = win32gui.GetWindowRect(edit_hwnd)
        cx = (rect[0] + rect[2]) // 2
        cy = (rect[1] + rect[3]) // 2
        _user32.SetCursorPos(cx, cy)
        _user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
        _user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP
        time.sleep(config.EDIT_CLICK_WAIT_SEC)
    except Exception:
        pass

    # Paste text via clipboard (handles Korean correctly, unlike WM_SETTEXT on some versions)
    win32clipboard.OpenClipboard()
    win32clipboard.EmptyClipboard()
    win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
    win32clipboard.CloseClipboard()
    time.sleep(config.CLIPBOARD_PASTE_WAIT_SEC)
    _send_ctrl_key_combo(0x56)  # Ctrl+V
    time.sleep(config.AFTER_PASTE_WAIT_SEC)

    # Press Enter using keybd_event (not WM_KEYDOWN — WM_KEYDOWN inserts newline in RICHEDIT)
    _user32.keybd_event(config.VK_RETURN, 0, 0, 0)
    _user32.keybd_event(config.VK_RETURN, 0, config.KEYEVENTF_KEYUP, 0)

    return {"success": True, "message": f"Message sent to '{room_name}'"}

def _copy_image_to_clipboard(file_path):
    from io import BytesIO
    from PIL import Image
    stream = BytesIO()
    with Image.open(file_path) as image:
        image.convert('RGB').save(stream, 'BMP')
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_DIB, stream.getvalue()[14:])
    finally:
        win32clipboard.CloseClipboard()

