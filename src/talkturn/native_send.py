"""Use the installed Kakao MCP only for sending to the user-opened self-chat window."""
import json
from pathlib import Path
import sys
import time
import ctypes
from ctypes import wintypes
import subprocess
import struct
import win32api
import win32clipboard
import win32con
import win32gui
import win32process
from ._kakao import controller, config, parser

ctypes.windll.user32.SetProcessDPIAware()
from .runtime import ROOT, PYTHON, executable, module_command, read_json, write_json


def safe_status(receipt):
    if 'messages' in receipt:
        return {'read':True, 'message_count':len(receipt['messages'])}
    return {key:value for key,value in receipt.items() if key!='baseline_hashes'}


def baseline_fingerprints(raw):
    """Reuse the pre-send local copy without retaining chat text in a receipt."""
    from .reply_bridge import fingerprint
    messages=parser.parse_clipboard_messages(raw)
    counts={}
    for message in messages:
        base=fingerprint(message)
        counts[base]=counts.get(base,0)+1
        message['_occurrence']=counts[base]
    return [fingerprint(message) for message in messages][-2000:]


def verify_image_paste(hwnd, edit, pid, sequence):
    """Check the exact focused edit and the unchanged image clipboard before V-down."""
    class GuiThreadInfo(ctypes.Structure):
        _fields_=[('cbSize',wintypes.DWORD),('flags',wintypes.DWORD),
                  ('hwndActive',wintypes.HWND),('hwndFocus',wintypes.HWND),
                  ('hwndCapture',wintypes.HWND),('hwndMenuOwner',wintypes.HWND),
                  ('hwndMoveSize',wintypes.HWND),('hwndCaret',wintypes.HWND),
                  ('rcCaret',wintypes.RECT)]
    thread,process=win32process.GetWindowThreadProcessId(hwnd)
    if process!=pid or not win32gui.IsChild(hwnd,edit):
        raise RuntimeError('Self-chat input identity changed before image paste')
    info=GuiThreadInfo(cbSize=ctypes.sizeof(GuiThreadInfo))
    query=ctypes.windll.user32.GetGUIThreadInfo
    query.argtypes=[wintypes.DWORD,ctypes.POINTER(GuiThreadInfo)]
    query.restype=wintypes.BOOL
    if not query(thread,ctypes.byref(info)):
        raise RuntimeError('Self-chat input focus is unavailable for image paste')
    if win32gui.GetForegroundWindow()!=hwnd or info.hwndActive!=hwnd or info.hwndFocus!=edit:
        raise RuntimeError('Self-chat input is not focused for image paste')
    if not sequence or ctypes.windll.user32.GetClipboardSequenceNumber()!=sequence:
        raise RuntimeError('Image clipboard changed before paste')
    if not win32clipboard.IsClipboardFormatAvailable(win32con.CF_DIB):
        raise RuntimeError('Image clipboard has no CF_DIB before paste')
    if ctypes.windll.user32.GetClipboardSequenceNumber()!=sequence:
        raise RuntimeError('Image clipboard changed before paste')


def scroll_to_latest(hwnd, pid, title):
    """Move only the verified chat list to its newest message, without typing or focus changes."""
    if (not win32gui.IsWindow(hwnd) or win32gui.GetWindowText(hwnd) != title
            or win32process.GetWindowThreadProcessId(hwnd)[1] != pid):
        raise RuntimeError('The configured self-chat window changed before scrolling')
    listing = controller.find_child_window_recursive(hwnd, config.KAKAO_LIST_CONTROL_CLASS)
    if (not listing or not win32gui.IsChild(hwnd, listing)
            or win32process.GetWindowThreadProcessId(listing)[1] != pid):
        raise RuntimeError('Self-chat message list is unavailable')
    win32gui.SendMessageTimeout(listing, win32con.WM_VSCROLL, win32con.SB_BOTTOM, 0,
                               win32con.SMTO_ABORTIFHUNG, 1000)
    time.sleep(0.15)  # Allow Kakao to paint the new viewport before PrintWindow.
    return listing


def wait_for_photo_count(read_back,previous,timeout=12):
    """Wait for Kakao's asynchronous upload; never repeat the image send."""
    deadline=time.monotonic()+timeout
    while True:
        count=sum(item['is_photo'] for item in parser.parse_chat_text(read_back())['messages'])
        if count>previous:
            return True
        if time.monotonic()>=deadline:
            return False
        time.sleep(.3)


def clipboard_snapshot():
    """Copy transferable clipboard data, excluding transient COM ownership markers."""
    win32clipboard.OpenClipboard()
    try:
        formats = []
        current = 0
        while True:
            current = win32clipboard.EnumClipboardFormats(current)
            if not current:
                break
            formats.append(current)
        saved = []
        for current in formats:
            name = win32clipboard.GetClipboardFormatName(current) if current >= 49152 else ''
            if name in {'DataObject', 'Ole Private Data'}:
                continue
            if current == win32con.CF_BITMAP and win32con.CF_DIB in formats:
                continue  # Windows synthesizes the bitmap handle from DIB bytes.
            data = win32clipboard.GetClipboardData(current)
            if current == win32con.CF_HDROP:
                data = struct.pack('<IiiII', 20, 0, 0, 0, 1) + ('\0'.join(data) + '\0\0').encode('utf-16-le')
            if not isinstance(data, (bytes, str)):
                raise RuntimeError('Clipboard contains a format that cannot be safely preserved')
            saved.append((current, data))
        return saved
    finally:
        win32clipboard.CloseClipboard()


def restore_clipboard(saved):
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        for format_id, data in saved:
            win32clipboard.SetClipboardData(format_id, data)
    finally:
        win32clipboard.CloseClipboard()


def send(request: dict) -> dict:
    settings = json.loads((ROOT / 'delivery.json').read_text(encoding='utf-8'))
    title, hwnd, pid = settings['room_name'], settings['window_id'], settings['process_id']
    matches = [room for room in controller.list_chat_windows() if room['title'] == title]
    if len(matches) != 1 or matches[0]['hwnd'] != hwnd:
        raise RuntimeError('The configured self-chat window must remain open')
    if win32process.GetWindowThreadProcessId(hwnd)[1] != pid:
        raise RuntimeError('Kakao process identity changed')
    edit = controller.find_child_window_recursive(hwnd, config.KAKAO_EDIT_CLASS)
    if not edit:
        raise RuntimeError('Self-chat input is unavailable')

    def input_text():
        length = win32gui.SendMessage(edit, win32con.WM_GETTEXTLENGTH, 0, 0)
        buffer = ctypes.create_unicode_buffer(length + 1)
        get_text = ctypes.windll.user32.SendMessageW
        get_text.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        get_text.restype = wintypes.LPARAM
        get_text(edit, win32con.WM_GETTEXT, length + 1, ctypes.addressof(buffer))
        return buffer.value

    if not (request.get('read_only') or request.get('verify_only')) and input_text().strip() not in {'', '메시지 입력'}:
        raise RuntimeError('Self-chat input contains a draft')
    images = [str(Path(path).resolve()) for path in request.get('images', [])]
    if any(not Path(path).is_file() or Path(path).suffix.lower() not in config.SUPPORTED_IMAGE_EXTENSIONS
           for path in images):
        raise ValueError('Invalid image file')
    text = request.get('text', '')
    if not isinstance(text, str):
        raise ValueError('Result text must be a string')
    previous_window, previous_cursor = win32gui.GetForegroundWindow(), win32api.GetCursorPos()
    was_minimized = bool(win32gui.IsIconic(hwnd))
    saved_clipboard = clipboard_snapshot()
    original_user32 = controller._user32
    original_run = controller.subprocess.run
    original_combo = controller._send_ctrl_key_combo
    expected_text = None
    expected_image_sequence = None
    image_dialog = None
    baseline_hashes = None

    def hidden_run(*args, **kwargs):
        kwargs.setdefault('creationflags', subprocess.CREATE_NO_WINDOW)
        return original_run(*args, **kwargs)

    def target_active(allow_dialog=False):
        foreground = win32gui.GetForegroundWindow()
        if foreground == hwnd:
            return True
        if allow_dialog and image_dialog and foreground == image_dialog:
            return win32process.GetWindowThreadProcessId(foreground)[1] == pid
        return False

    class GuardedUser32:
        def __getattr__(self, name):
            return getattr(original_user32, name)

        def keybd_event(self, key, scan, flags, extra):
            # Alt is used by the upstream focus helper. All actual message input is guarded.
            if not flags & config.KEYEVENTF_KEYUP and key != 0x12 and not target_active(allow_dialog=key == config.VK_RETURN):
                foreground = win32gui.GetForegroundWindow()
                foreground_pid = win32process.GetWindowThreadProcessId(foreground)[1] if foreground else 0
                raise RuntimeError(f'Focus changed before Kakao key {key}; foreground={foreground}, pid={foreground_pid}, target={hwnd}')
            if key == config.VK_RETURN and not flags & config.KEYEVENTF_KEYUP and expected_text is not None:
                actual = input_text().replace('\r\n', '\n').replace('\r', '\n').strip()
                if actual != expected_text.strip():
                    raise RuntimeError('Pasted result does not match; Enter was not pressed')
            if key == 0x56 and not flags & config.KEYEVENTF_KEYUP and expected_image_sequence is not None:
                verify_image_paste(hwnd,edit,pid,expected_image_sequence)
            return original_user32.keybd_event(key, scan, flags, extra)

    def read_back():
        nonlocal baseline_hashes
        listing = controller.find_child_window_recursive(hwnd,config.KAKAO_LIST_CONTROL_CLASS)
        if not listing:
            raise RuntimeError('Self-chat message list is unavailable')
        controller.bring_window_to_front(hwnd)
        time.sleep(0.15)
        if not target_active():
            raise RuntimeError('Cannot focus self-chat for verification')
        left,top,right,bottom = win32gui.GetWindowRect(listing)
        # In self-chat all bubbles are right-aligned. Clicking the left gutter avoids opening photos.
        # Stay past the resize border; a 3px gutter click may resize instead of focusing the list.
        win32api.SetCursorPos((left+20,min(top+24,bottom-10)))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN,0,0,0,0)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP,0,0,0,0)
        time.sleep(0.08)
        original_combo(config.VK_A)
        time.sleep(0.1)
        sequence=ctypes.windll.user32.GetClipboardSequenceNumber()
        original_combo(config.VK_C)
        for _ in range(15):
            time.sleep(0.1)
            if ctypes.windll.user32.GetClipboardSequenceNumber()!=sequence:
                copied=controller._read_clipboard_text()
                if baseline_hashes is None and not (request.get('read_only') or request.get('verify_only')):
                    baseline_hashes=baseline_fingerprints(copied)
                # Leave the reply thumbnail unselected so its colors remain readable.
                if target_active():
                    win32api.SetCursorPos((left+20,min(top+24,bottom-10)))
                    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN,0,0,0,0)
                    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP,0,0,0,0)
                scroll_to_latest(hwnd, pid, title)
                return copied
        return ''  # Empty/unfocused chat: never parse unrelated clipboard contents.

    def guarded_combo(key):
        if key == 0x56 and input_text().strip() not in {'', '메시지 입력'}:
            raise RuntimeError('Self-chat input contains a real draft after focus')
        return original_combo(key)

    def send_photo(path):
        nonlocal image_dialog,expected_image_sequence
        before = set()
        def collect(handle, _):
            if win32gui.IsWindowVisible(handle) and win32process.GetWindowThreadProcessId(handle)[1] == pid:
                before.add(handle)
        win32gui.EnumWindows(collect, None)
        controller._copy_image_to_clipboard(path)
        expected_image_sequence=ctypes.windll.user32.GetClipboardSequenceNumber()
        controller.bring_window_to_front(hwnd)
        time.sleep(0.2)
        if not target_active():
            raise RuntimeError('Unable to focus self-chat for image')
        left, top, right, bottom = win32gui.GetWindowRect(edit)
        win32api.SetCursorPos(((left+right)//2, (top+bottom)//2))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(0.15)
        try:
            guarded_combo(0x56)
        finally:
            expected_image_sequence=None
        for _ in range(30):
            time.sleep(0.1)
            foreground = win32gui.GetForegroundWindow()
            if foreground not in before and foreground and win32process.GetWindowThreadProcessId(foreground)[1] == pid:
                image_dialog = foreground
                break
        if not image_dialog:
            raise RuntimeError('Expected Kakao image confirmation was not shown')
        time.sleep(0.3)
        controller._user32.keybd_event(config.VK_RETURN, 0, 0, 0)
        controller._user32.keybd_event(config.VK_RETURN, 0, config.KEYEVENTF_KEYUP, 0)
        time.sleep(0.4)
        image_dialog = None

    controller._user32 = GuardedUser32()
    controller.subprocess.run = hidden_run
    controller._send_ctrl_key_combo = guarded_combo
    receipts = []
    try:
        if request.get('read_only'):
            return {'messages':parser.parse_clipboard_messages(read_back())}
        if request.get('verify_only'):
            raw = read_back()
            messages = parser.parse_chat_text(raw)['messages']
            matches = [index for index, item in enumerate(messages) if text.strip() in item['text']]
            photo_after = bool(matches) and any(item['is_photo'] for item in messages[matches[-1]+1:])
            return {'text_present':text.strip() in raw,'photo_after_text':photo_after}
        if text:
            # Kakao PC accepts ordinary long messages. Split only very large results.
            for start in range(0, len(text), 3000):
                part = text[start:start + 3000]
                expected_bodies = parser.outgoing_texts(part)
                previous_count = sum(item['text'].strip() in expected_bodies for item in parser.parse_clipboard_messages(read_back()))
                expected_text = part
                result = controller.send_message_to_room(title, part)
                if not result.get('success'):
                    raise RuntimeError('Text send failed')
                time.sleep(0.3)
                current_count = sum(item['text'].strip() in expected_bodies for item in parser.parse_clipboard_messages(read_back()))
                if current_count <= previous_count:
                    raise RuntimeError('Text send could not be verified')
                receipts.append('text')
                expected_text = None
        for path in images:
            before = sum(item['is_photo'] for item in parser.parse_chat_text(read_back())['messages'])
            send_photo(path)
            if not wait_for_photo_count(read_back,before):
                raise RuntimeError('Image send could not be verified')
            receipts.append('image')
        return {'verified':True, 'sent':receipts, 'baseline_hashes':baseline_hashes or []}
    finally:
        controller._user32 = original_user32
        controller.subprocess.run = original_run
        controller._send_ctrl_key_combo = original_combo
        original_user32.keybd_event(config.VK_CONTROL, 0, config.KEYEVENTF_KEYUP, 0)
        original_user32.keybd_event(0x12, 0, config.KEYEVENTF_KEYUP, 0)
        restore_clipboard(saved_clipboard)
        if win32gui.GetForegroundWindow() == hwnd and win32gui.IsWindow(previous_window):
            try:
                win32gui.SetForegroundWindow(previous_window)
                win32api.SetCursorPos(previous_cursor)
                if was_minimized:
                    win32gui.ShowWindow(hwnd,win32con.SW_SHOWMINNOACTIVE)
            except Exception:
                pass


if __name__ == '__main__':
    sys.stdin.reconfigure(encoding='utf-8')
    try:
        receipt = send(json.load(sys.stdin))
        (ROOT / 'native-status.json').write_text(json.dumps(safe_status(receipt)), encoding='utf-8')
        print(json.dumps(receipt))
    except Exception as error:
        receipt = {'verified':False,'error':str(error)}
        (ROOT / 'native-status.json').write_text(json.dumps(receipt), encoding='utf-8')
        print(json.dumps(receipt), file=sys.stderr)
        sys.exit(1)
