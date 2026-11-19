"""Experimental macOS Kakao layer (Accessibility + Quartz + Vision). Imported only on darwin.

Kakao for Mac internals (window titles, AX tree, scroll direction) are unverified on real devices:
every unknown fails with an explicit error instead of a guess. `talkturn mac-report` collects the facts.
"""
import fcntl
import io
import json
from pathlib import Path
import platform
import plistlib
import re
import subprocess
import sys
import tempfile
import time

from AppKit import (NSApplicationActivateIgnoringOtherApps, NSPasteboard, NSPasteboardItem, NSPasteboardTypePNG,
                    NSPasteboardTypeString, NSPasteboardTypeTIFF, NSRunningApplication, NSWorkspace)
from ApplicationServices import (AXIsProcessTrustedWithOptions, AXUIElementCopyAttributeValue,
                                 AXUIElementCreateApplication, AXUIElementIsAttributeSettable,
                                 AXUIElementPerformAction, AXUIElementSetAttributeValue, AXValueGetValue,
                                 kAXTrustedCheckOptionPrompt, kAXValueCGPointType, kAXValueCGSizeType)
from CoreFoundation import (CFEqual, CFMachPortCreateRunLoopSource, CFMachPortInvalidate, CFRunLoopAddSource,
                            CFRunLoopGetCurrent, CFRunLoopRunInMode, kCFRunLoopCommonModes, kCFRunLoopDefaultMode)
from Foundation import NSData
from PIL import Image
from Quartz import (CGEventCreate, CGEventCreateKeyboardEvent, CGEventCreateMouseEvent, CGEventGetFlags,
                    CGEventGetIntegerValueField, CGEventGetLocation, CGEventPost, CGEventSetFlags,
                    CGEventSetIntegerValueField, CGEventSourceSecondsSinceLastEventType, CGEventTapCreate,
                    CGEventTapEnable, CGEventTapIsEnabled, CGGetActiveDisplayList, CGPreflightScreenCaptureAccess,
                    CGRequestScreenCaptureAccess, CGWarpMouseCursorPosition, CGWindowListCopyWindowInfo,
                    kCGEventFlagMaskAlternate, kCGEventFlagMaskCommand, kCGEventFlagMaskControl,
                    kCGEventFlagMaskShift, kCGEventKeyDown, kCGEventKeyUp, kCGEventLeftMouseDown,
                    kCGEventLeftMouseDragged, kCGEventLeftMouseUp, kCGEventMouseMoved, kCGEventOtherMouseDown,
                    kCGEventOtherMouseDragged, kCGEventOtherMouseUp, kCGEventRightMouseDown,
                    kCGEventRightMouseDragged, kCGEventRightMouseUp, kCGEventScrollWheel,
                    kCGEventSourceStateHIDSystemState, kCGEventSourceUserData, kCGEventTapDisabledByTimeout,
                    kCGEventTapDisabledByUserInput, kCGEventTapOptionDefault, kCGHIDEventTap,
                    kCGHeadInsertEventTap, kCGKeyboardEventKeycode, kCGMouseButtonLeft, kCGNullWindowID,
                    kCGSessionEventTap, kCGWindowListExcludeDesktopElements, kCGWindowListOptionAll)

from . import runtime
from ._kakao import config, parser

BUNDLE_ID = 'com.kakao.KakaoTalkMac'
MARK = 0x544B5452  # 'TKTR': stamped on every event TalkTurn posts so the mouse lock lets it through
KEY_V, KEY_A, KEY_C, KEY_RETURN, KEY_ESCAPE = 9, 0, 8, 36, 53
PLACEHOLDER = {'', '메시지 입력'}
PERMISSION_ERROR = '맥 권한 필요 · 시스템 설정 > 개인정보 보호 및 보안에서 이 터미널 앱의 손쉬운 사용·화면 기록을 켜고 터미널을 다시 여세요.'
LOCK_PATH = Path(tempfile.gettempdir()) / 'talkturn-mouse-lock.lock'
MODIFIERS = kCGEventFlagMaskShift | kCGEventFlagMaskControl | kCGEventFlagMaskAlternate | kCGEventFlagMaskCommand


# --- permissions -----------------------------------------------------------------------------

def permissions(prompt=False):
    accessibility = bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: bool(prompt)}))
    screen = bool(CGPreflightScreenCaptureAccess())
    if prompt and not screen:
        CGRequestScreenCaptureAccess()
    return {'accessibility': accessibility, 'screen': screen}


_asked = False


def require_permissions():
    global _asked
    ask = sys.stdin is not None and sys.stdin.isatty() and not _asked  # hooks/receiver never open Settings; a terminal asks once
    state = permissions(prompt=ask)
    if all(state.values()):
        return
    if ask:
        _asked = True
        for key, pane in (('accessibility', 'Privacy_Accessibility'), ('screen', 'Privacy_ScreenCapture')):
            if not state[key]:
                subprocess.run(['open', 'x-apple.systempreferences:com.apple.preference.security?' + pane],
                               capture_output=True, timeout=10)
    raise RuntimeError(PERMISSION_ERROR)


# --- windows and Accessibility ---------------------------------------------------------------

def _names(owner):
    # ponytail: the Korean-locale process name is a guess; real-device report decides if it is needed
    return {owner, '카카오톡'} if owner == 'KakaoTalk' else {owner}


def _cg_windows(owner='KakaoTalk'):
    names = _names(owner)
    return [w for w in CGWindowListCopyWindowInfo(kCGWindowListOptionAll | kCGWindowListExcludeDesktopElements,
                                                  kCGNullWindowID) or [] if w.get('kCGWindowOwnerName') in names]


def windows(owner='KakaoTalk'):
    rows = []
    for w in _cg_windows(owner):
        title = w.get('kCGWindowName') or ''
        if w.get('kCGWindowLayer') != 0 or not title:
            continue
        b = w['kCGWindowBounds']
        rows.append({'hwnd': int(w['kCGWindowNumber']), 'title': str(title), 'pid': int(w['kCGWindowOwnerPID']),
                     'bounds': (b['X'], b['Y'], b['Width'], b['Height'])})
    return rows


def _attr(element, name):
    error, value = AXUIElementCopyAttributeValue(element, name, None)
    return value if error == 0 else None


def _settable(element, name):
    error, value = AXUIElementIsAttributeSettable(element, name, None)
    return error == 0 and bool(value)


def _frame(element):
    position, size = _attr(element, 'AXPosition'), _attr(element, 'AXSize')
    if position is None or size is None:
        return None
    ok_a, point = AXValueGetValue(position, kAXValueCGPointType, None)
    ok_b, extent = AXValueGetValue(size, kAXValueCGSizeType, None)
    return (point.x, point.y, extent.width, extent.height) if ok_a and ok_b else None


def _walk(root, depth=14, limit=3000):
    """Bounded depth-first walk in document order: [(element, depth)]."""
    found, stack = [], [(root, 0)]
    while stack and len(found) < limit:
        element, level = stack.pop()
        found.append((element, level))
        if level < depth:
            stack.extend((child, level + 1) for child in reversed(list(_attr(element, 'AXChildren') or [])))
    return found


def _window_el(window):
    app = AXUIElementCreateApplication(window['pid'])
    for element in _attr(app, 'AXWindows') or []:
        if _attr(element, 'AXTitle') != window['title']:
            continue
        frame = _frame(element)
        if frame and all(abs(a - b) <= 2 for a, b in zip(frame, window['bounds'])):
            return element
        if _attr(element, 'AXMinimized'):  # a minimized window has no comparable frame
            return element
    return None


def chat_parts(window_el):
    """(input, listing): the lowest text area/field, and the biggest scroll area that does not hold it."""
    nodes = []
    for element, _ in _walk(window_el):
        role = _attr(element, 'AXRole')
        if role in ('AXTextArea', 'AXTextField', 'AXScrollArea'):
            frame = _frame(element)
            if frame:
                nodes.append((element, role, frame))
    inputs = [node for node in nodes if node[1] != 'AXScrollArea']
    if not inputs:
        return None, None
    edit = max(inputs, key=lambda node: node[2][1] + node[2][3])
    ex, ey, ew, eh = edit[2]
    holds = lambda f: f[0] - 2 <= ex and f[1] - 2 <= ey and f[0] + f[2] + 2 >= ex + ew and f[1] + f[3] + 2 >= ey + eh
    lists = [node for node in nodes if node[1] == 'AXScrollArea' and not holds(node[2])]
    return edit[0], (max(lists, key=lambda node: node[2][2] * node[2][3])[0] if lists else None)


def chat_windows(owner='KakaoTalk'):
    require_permissions()
    rows = []
    for window in windows(owner):
        element = _window_el(window)
        if element is not None and all(part is not None for part in chat_parts(element)):
            rows.append({'hwnd': window['hwnd'], 'title': window['title'], 'pid': window['pid']})
    return rows


def kakao_running():
    return any(app.bundleIdentifier() == BUNDLE_ID or app.localizedName() in _names('KakaoTalk')
               for app in NSWorkspace.sharedWorkspace().runningApplications())


def window_valid(window_id, pid, title):
    return any(w['hwnd'] == window_id and w['pid'] == pid and w['title'] == title for w in windows())


def _paired(settings):
    title, wid, pid = settings['room_name'], settings['window_id'], settings['process_id']
    matches = [w for w in windows() if w['title'] == title]
    if len(matches) != 1 or matches[0]['hwnd'] != wid:
        raise RuntimeError('The configured self-chat window must remain open')
    if matches[0]['pid'] != pid:
        raise RuntimeError('Kakao process identity changed')
    element = _window_el(matches[0])
    if element is None:
        raise RuntimeError('Self-chat window is not accessible')
    return matches[0], element


def _settings():
    return json.loads((runtime.ROOT / 'delivery.json').read_text(encoding='utf-8'))


# --- input events ----------------------------------------------------------------------------

def _sleep(seconds):
    """Sleep while pumping the run loop so NSWorkspace state stays current in a CLI process."""
    end = time.monotonic() + seconds
    while True:
        left = end - time.monotonic()
        if left <= 0:
            return
        CFRunLoopRunInMode(kCFRunLoopDefaultMode, min(left, 0.02), False)
        time.sleep(max(0, min(end - time.monotonic(), 0.02)))


def _post(event):
    CGEventSetIntegerValueField(event, kCGEventSourceUserData, MARK)
    CGEventPost(kCGHIDEventTap, event)


def _key(code, flags=0):
    for down in (True, False):
        event = CGEventCreateKeyboardEvent(None, code, down)
        CGEventSetFlags(event, flags)
        _post(event)
        time.sleep(0.02)


def _click(x, y):
    CGWarpMouseCursorPosition((x, y))
    for kind in (kCGEventLeftMouseDown, kCGEventLeftMouseUp):
        _post(CGEventCreateMouseEvent(None, kind, (x, y), kCGMouseButtonLeft))
        time.sleep(0.03)


def _cursor():
    location = CGEventGetLocation(CGEventCreate(None))
    return (location.x, location.y)


def _frontmost():
    return NSWorkspace.sharedWorkspace().frontmostApplication()


def _frontmost_pid():
    app = _frontmost()
    return app.processIdentifier() if app else 0


def _same(a, b):
    return a is not None and b is not None and bool(CFEqual(a, b))


# --- pasteboard ------------------------------------------------------------------------------

def snapshot():
    """Every pasteboard item's types and data (dynamic `dyn.` types are regenerated by the system)."""
    saved = []
    for item in NSPasteboard.generalPasteboard().pasteboardItems() or []:
        entry = {}
        for kind in item.types():
            data = item.dataForType_(kind)
            if data is not None and not str(kind).startswith('dyn.'):
                entry[str(kind)] = data
        saved.append(entry)
    return saved


def restore(saved):
    board = NSPasteboard.generalPasteboard()
    board.clearContents()
    items = []
    for entry in saved:
        item = NSPasteboardItem.alloc().init()
        for kind, data in entry.items():
            item.setData_forType_(data, kind)
        items.append(item)
    if items:
        board.writeObjects_(items)


def _set_text(text):
    board = NSPasteboard.generalPasteboard()
    board.declareTypes_owner_([NSPasteboardTypeString], None)
    board.setString_forType_(text, NSPasteboardTypeString)
    return board.changeCount()


def _set_image(path):
    with Image.open(path) as source:
        picture = source.convert('RGBA')
    board = NSPasteboard.generalPasteboard()
    board.declareTypes_owner_([NSPasteboardTypePNG, NSPasteboardTypeTIFF], None)
    for kind, name in ((NSPasteboardTypePNG, 'PNG'), (NSPasteboardTypeTIFF, 'TIFF')):
        buffer = io.BytesIO()
        picture.save(buffer, name)
        raw = buffer.getvalue()
        board.setData_forType_(NSData.dataWithBytes_length_(raw, len(raw)), kind)
    return board.changeCount()


def _pasteboard_text():
    return NSPasteboard.generalPasteboard().stringForType_(NSPasteboardTypeString) or ''


# --- chat session ----------------------------------------------------------------------------

def _norm(text):
    return text.replace('\r\n', '\n').replace('\r', '\n')


def baseline_fingerprints(raw):
    """Same as native_send.baseline_fingerprints (that module cannot be imported here)."""
    from .reply_bridge import fingerprint
    messages = parser.parse_clipboard_messages(raw)
    counts = {}
    for message in messages:
        base = fingerprint(message)
        counts[base] = counts.get(base, 0) + 1
        message['_occurrence'] = counts[base]
    return [fingerprint(message) for message in messages][-2000:]


def scroll_to_latest(listing):
    bar = _attr(listing, 'AXVerticalScrollBar')
    if bar is None:
        return  # ponytail: no bar = content fits (or Kakao hides it); a real-device report must confirm
    error = AXUIElementSetAttributeValue(bar, 'AXValue', 1.0)
    if error:
        raise RuntimeError(f'Cannot scroll the self-chat list (AX error {error})')
    _sleep(0.15)  # let Kakao paint the new viewport before it is read or captured


class _Chat:
    def __init__(self, read_only=False):
        require_permissions()
        settings = _settings()
        self.title, self.pid = settings['room_name'], settings['process_id']
        self.window, self.el = _paired(settings)
        self.edit, self.listing = chat_parts(self.el)
        if self.edit is None:
            raise RuntimeError('Self-chat input is unavailable')
        if self.listing is None:
            raise RuntimeError('Self-chat message list is unavailable')
        self.app = AXUIElementCreateApplication(self.pid)
        self.read_only = read_only
        self.baseline = None
        self.previous, self.cursor = _frontmost(), _cursor()
        self.was_minimized = bool(_attr(self.el, 'AXMinimized'))
        self.saved = snapshot()

    def close(self):
        try:
            restore(self.saved)
        finally:
            try:
                if _frontmost_pid() == self.pid and self.previous and self.previous.processIdentifier() != self.pid:
                    self.previous.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
                CGWarpMouseCursorPosition(self.cursor)
                if self.was_minimized:
                    AXUIElementSetAttributeValue(self.el, 'AXMinimized', True)
            except Exception:
                pass

    def value(self):
        value = _attr(self.edit, 'AXValue')
        return _norm(str(value)) if isinstance(value, str) else ''

    def front(self):
        if _frontmost_pid() != self.pid:
            raise RuntimeError(f'Focus changed before Kakao key; foreground pid={_frontmost_pid()}, target={self.pid}')

    def input_focused(self):
        return _same(_attr(self.app, 'AXFocusedUIElement'), self.edit)

    def press(self, code, flags=0, *, in_input=False):
        self.front()
        if in_input and not self.input_focused():
            raise RuntimeError('Self-chat input is not focused')
        _key(code, flags)

    def click(self, x, y):
        self.front()
        _click(x, y)

    def activate(self):
        if _attr(self.el, 'AXMinimized'):
            AXUIElementSetAttributeValue(self.el, 'AXMinimized', False)
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(self.pid)
        if app is None:
            raise RuntimeError('Kakao process identity changed')
        app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
        AXUIElementPerformAction(self.el, 'AXRaise')
        deadline = time.monotonic() + 2
        while _frontmost_pid() != self.pid:
            if time.monotonic() > deadline:
                raise RuntimeError('Cannot focus self-chat')
            _sleep(0.05)
        _sleep(0.15)

    def focus_input(self):
        error = AXUIElementSetAttributeValue(self.edit, 'AXFocused', True)
        _sleep(0.15)
        if error or not self.input_focused():
            frame = _frame(self.edit)
            if frame is None:
                raise RuntimeError('Self-chat input is unavailable')
            self.click(frame[0] + frame[2] / 2, frame[1] + frame[3] / 2)
            _sleep(0.15)
        if not self.input_focused():
            raise RuntimeError('Self-chat input is not focused')

    def read_back(self):
        """Select all in the message list and copy it; '' when nothing was copied."""
        self.activate()
        _sleep(0.15)
        frame = _frame(self.listing)
        if frame is None:
            raise RuntimeError('Self-chat message list is unavailable')
        left, top, bottom = frame[0], frame[1], frame[1] + frame[3]
        spot = (left + 20, min(top + 24, bottom - 10))  # self-chat bubbles are right-aligned: avoid photos
        inner = next((e for e, _ in _walk(self.listing) if _attr(e, 'AXRole') in ('AXTable', 'AXList', 'AXOutline')),
                     self.listing)
        if not (_settable(inner, 'AXFocused') and AXUIElementSetAttributeValue(inner, 'AXFocused', True) == 0):
            self.click(*spot)
        _sleep(0.08)
        self.press(KEY_A, kCGEventFlagMaskCommand)
        _sleep(0.1)
        board = NSPasteboard.generalPasteboard()
        sequence = board.changeCount()
        self.press(KEY_C, kCGEventFlagMaskCommand)
        copied = ''
        for _ in range(15):
            _sleep(0.1)
            if board.changeCount() != sequence:
                copied = _pasteboard_text()
                break
        if copied:
            if self.baseline is None and not self.read_only:
                self.baseline = baseline_fingerprints(copied)
            if _frontmost_pid() == self.pid:
                self.click(*spot)  # deselect so reply thumbnails keep their colors
        scroll_to_latest(self.listing)
        return copied  # empty/unfocused chat: never parse unrelated pasteboard text

    def cleared(self, seconds=3):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.value().strip() in PLACEHOLDER:
                return True
            _sleep(0.1)
        return False

    def send_text(self, part):
        expected = parser.outgoing_texts(part)
        count = lambda messages: sum(item['text'].strip() in expected for item in messages)
        before = parser.parse_clipboard_messages(self.read_back())
        _set_text(part)
        self.activate()
        self.focus_input()
        if self.value().strip() not in PLACEHOLDER:
            raise RuntimeError('Self-chat input contains a real draft after focus')
        self.press(KEY_V, kCGEventFlagMaskCommand, in_input=True)
        deadline = time.monotonic() + 2
        while self.value().strip() != part.strip():
            if time.monotonic() > deadline:
                raise RuntimeError('Pasted result does not match; Enter was not pressed')
            _sleep(0.1)
        self.press(KEY_RETURN, in_input=True)
        if not self.cleared():
            raise RuntimeError('Text send could not be verified')  # the input kept the text: Return did nothing
        if before and count(parser.parse_clipboard_messages(self.read_back())) <= count(before):
            raise RuntimeError('Text send could not be verified')

    def send_photo(self, path):
        numbers = lambda: {int(w['kCGWindowNumber']) for w in _cg_windows() if w.get('kCGWindowOwnerPID') == self.pid}
        seen = numbers()
        sequence = _set_image(path)
        self.activate()
        self.focus_input()
        if self.value().strip() not in PLACEHOLDER:
            raise RuntimeError('Self-chat input contains a real draft after focus')
        if NSPasteboard.generalPasteboard().changeCount() != sequence:
            raise RuntimeError('Image clipboard changed before paste')
        self.press(KEY_V, kCGEventFlagMaskCommand, in_input=True)
        dialog = set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not dialog and not _attr(self.el, 'AXSheets'):
            _sleep(0.1)
            dialog = numbers() - seen
        sheet = bool(_attr(self.el, 'AXSheets'))
        if dialog or sheet:
            _sleep(0.3)
            self.press(KEY_RETURN)
            end = time.monotonic() + 12
            while (dialog & numbers()) or _attr(self.el, 'AXSheets'):
                if time.monotonic() > end:
                    raise RuntimeError('Image send could not be verified')
                _sleep(0.2)
        elif '￼' in self.value():
            self.press(KEY_RETURN, in_input=True)
            if not self.cleared(12):
                raise RuntimeError('Image send could not be verified')
        else:
            raise RuntimeError('Expected Kakao image confirmation was not shown')


def send(request: dict) -> dict:
    read_only = bool(request.get('read_only') or request.get('verify_only'))
    chat = _Chat(read_only=read_only)
    try:
        if not read_only and chat.value().strip() not in PLACEHOLDER:
            raise RuntimeError('Self-chat input contains a draft')
        images = [str(Path(path).resolve()) for path in request.get('images', [])]
        if any(not Path(path).is_file() or Path(path).suffix.lower() not in config.SUPPORTED_IMAGE_EXTENSIONS
               for path in images):
            raise ValueError('Invalid image file')
        text = request.get('text', '')
        if not isinstance(text, str):
            raise ValueError('Result text must be a string')
        if request.get('read_only'):
            return {'messages': parser.parse_clipboard_messages(chat.read_back())}
        if request.get('verify_only'):
            raw = chat.read_back()
            messages = parser.parse_chat_text(raw)['messages']
            matches = [index for index, item in enumerate(messages) if text.strip() in item['text']]
            photo_after = bool(matches) and any(item['is_photo'] for item in messages[matches[-1] + 1:])
            return {'text_present': text.strip() in raw, 'photo_after_text': photo_after}
        receipts = []
        for start in range(0, len(text), 3000):
            chat.send_text(text[start:start + 3000])
            receipts.append('text')
        for path in images:
            chat.send_photo(path)
            receipts.append('image')
        return {'verified': True, 'sent': receipts, 'baseline_hashes': chat.baseline or []}
    finally:
        chat.close()


def safe_status(receipt):
    if 'messages' in receipt:
        return {'read': True, 'message_count': len(receipt['messages'])}
    return {key: value for key, value in receipt.items() if key != 'baseline_hashes'}


# --- capture, OCR, screens, idle -------------------------------------------------------------

def capture():
    """Crop the message list (bottom 700 pt) of the paired window into ROOT/reply-view.png."""
    require_permissions()
    settings = _settings()
    window, element = _paired(settings)
    minimized = bool(_attr(element, 'AXMinimized'))
    try:
        if minimized:  # restore pixels without activating the app
            AXUIElementSetAttributeValue(element, 'AXMinimized', False)
            for _ in range(20):
                if not _attr(element, 'AXMinimized'):
                    break
                _sleep(0.1)
            else:
                raise RuntimeError('Cannot restore self-chat for capture without activation')
            window, element = _paired(settings)
        listing = chat_parts(element)[1]
        if listing is None:
            raise RuntimeError('Self-chat message list is unavailable')
        scroll_to_latest(listing)
        area = _frame(listing)
        if area is None:
            raise RuntimeError('Self-chat message list is unavailable')
        with tempfile.TemporaryDirectory(prefix='talkturn-capture-') as temporary:
            path = Path(temporary) / 'frame.png'
            result = subprocess.run(['screencapture', '-x', '-o', '-l', str(window['hwnd']), str(path)],
                                    capture_output=True, timeout=10)
            if result.returncode or not path.is_file() or not path.stat().st_size:
                raise RuntimeError('Cannot capture the self-chat window')
            with Image.open(path) as frame:
                bx, by, bw, bh = window['bounds']
                scale = frame.width / bw
                x1 = max(0, round((area[0] - bx) * scale))
                x2 = min(frame.width, round((area[0] + area[2] - bx) * scale))
                y2 = min(frame.height, round((area[1] + area[3] - by) * scale))
                y1 = max(0, round((area[1] - by) * scale), y2 - round(700 * scale))
                if x2 <= x1 or y2 <= y1:
                    raise RuntimeError('Reply area is outside the captured window')
                with frame.crop((x1, y1, x2, y2)) as cropped:
                    cropped.save(runtime.ROOT / 'reply-view.png')
    finally:
        if minimized:
            try:
                AXUIElementSetAttributeValue(element, 'AXMinimized', True)
            except Exception:
                pass


def ocr(path):
    """Vision text recognition in the shape of reply_ocr.ps1: one observation = one line."""
    try:
        import Vision
        from Foundation import NSRange, NSURL
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        request.setRecognitionLanguages_(['ko-KR', 'en-US'])
        request.setUsesLanguageCorrection_(False)
        handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(str(path)), None)
        ok, _ = handler.performRequests_error_([request], None)
        if not ok:
            return None
        with Image.open(path) as picture:
            width, height = picture.size
        top_down = sorted(request.results() or [],
                          key=lambda o: -(o.boundingBox().origin.y + o.boundingBox().size.height))
        words = []
        for line, observation in enumerate(top_down):
            candidates = observation.topCandidates_(1)
            if not candidates:
                continue
            text = str(candidates[0].string())
            for match in re.finditer(r'\S+', text):
                start = len(text[:match.start()].encode('utf-16-le')) // 2
                length = len(match.group().encode('utf-16-le')) // 2
                box, _ = candidates[0].boundingBoxForRange_error_(NSRange(start, length), None)
                if box is None:
                    continue
                rect = box.boundingBox()  # normalized, origin bottom-left
                words.append({'line': line, 'text': match.group(), 'x': rect.origin.x * width,
                              'y': (1 - rect.origin.y - rect.size.height) * height,
                              'width': rect.size.width * width, 'height': rect.size.height * height})
        return {'words': words, 'angle': 0}
    except Exception:
        return None


def idle_seconds():
    return CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateHIDSystemState, 0xFFFFFFFF)


def screen_images(root):
    error, displays, count = CGGetActiveDisplayList(16, None, None)
    if error:
        raise RuntimeError(f'Cannot list displays (error {error})')
    paths = []
    for number in range(1, count + 1):
        path = Path(root) / f'screen-{number}.png'
        result = subprocess.run(['screencapture', '-x', '-D', str(number), str(path)], capture_output=True, timeout=15)
        if result.returncode or not path.is_file():
            raise RuntimeError(f'Cannot capture display {number}')
        paths.append(path)
    return paths


# --- mouse lock ------------------------------------------------------------------------------

def lock_active():
    with LOCK_PATH.open('a+b') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False


def lock(bridge_pid):
    """Swallow physical mouse/scroll input; physical Esc held mouse_lock.HOLD seconds unlocks + `out`."""
    from . import mouse_lock
    from .service import process_running
    handle = LOCK_PATH.open('a+b')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return
    esc = mouse_lock.EscHold()
    state = {'locked': True, 'keys': True, 'tap': False}
    mouse = {kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGEventRightMouseDown, kCGEventRightMouseUp,
             kCGEventOtherMouseDown, kCGEventOtherMouseUp, kCGEventMouseMoved, kCGEventLeftMouseDragged,
             kCGEventRightMouseDragged, kCGEventOtherMouseDragged, kCGEventScrollWheel}
    keys = {kCGEventKeyDown, kCGEventKeyUp}
    holder = []

    def callback(proxy, kind, event, refcon):
        if kind in (kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput):
            CGEventTapEnable(holder[0], True)
            return event
        if CGEventGetIntegerValueField(event, kCGEventSourceUserData) == MARK:
            return event
        if kind in keys:
            if state['keys'] and CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode) == KEY_ESCAPE:
                if kind == kCGEventKeyDown:
                    if esc.since is not None or not CGEventGetFlags(event) & MODIFIERS:  # leave Cmd+Esc etc. alone
                        esc.down(time.monotonic())
                        return None
                elif esc.since is not None:
                    state['tap'] = esc.up(time.monotonic())
                    return None
            return event
        return None if kind in mouse and state['locked'] else event

    out = None
    failed = 0.0

    def tick():
        nonlocal out, failed
        now = time.monotonic()
        if state['tap']:  # replay the swallowed short press, marked so it passes
            state['tap'] = False
            _key(KEY_ESCAPE)
        esc.poll(now)
        if not CGEventTapIsEnabled(holder[0]):
            CGEventTapEnable(holder[0], True)
        if out is None:
            if not (runtime.ROOT / 'reply-enabled').exists() or not process_running(bridge_pid):
                return False  # out/stop elsewhere or a dead receiver: never keep the lock alone
            if not esc.fired:
                return True
            state['locked'] = False
            out = mouse_lock._send_out()
            return True
        if state['keys'] and esc.since is None:  # Esc is up: stop swallowing it
            state['keys'] = False
        if state['keys'] or out.poll() is None:
            return True
        if out.returncode and not failed:
            failed = now
        return bool(failed) and now - failed < 3

    mask = 0
    for kind in mouse | keys:
        mask |= 1 << kind
    tap = None
    try:
        tap = CGEventTapCreate(kCGSessionEventTap, kCGHeadInsertEventTap, kCGEventTapOptionDefault, mask,
                               callback, None)
        if tap is None:
            raise RuntimeError('이벤트 탭 생성 실패 · 손쉬운 사용 권한 필요. Esc 해제 없이는 잠그지 않음.')
        holder.append(tap)
        CFRunLoopAddSource(CFRunLoopGetCurrent(), CFMachPortCreateRunLoopSource(None, tap, 0), kCFRunLoopCommonModes)
        CGEventTapEnable(tap, True)
        while True:
            CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.05, False)
            if not tick():
                break
    finally:
        if tap is not None:
            CGEventTapEnable(tap, False)
            CFMachPortInvalidate(tap)
        handle.close()


# --- report ----------------------------------------------------------------------------------

def _guarded(function):
    try:
        return function()
    except Exception as error:
        return {'error': f'{type(error).__name__}: {error}'[:200]}


def _kakao_version():
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        if app.bundleIdentifier() == BUNDLE_ID or app.localizedName() in _names('KakaoTalk'):
            with (Path(str(app.bundleURL().path())) / 'Contents/Info.plist').open('rb') as stream:
                return plistlib.load(stream).get('CFBundleShortVersionString')
    return None


def _tree():
    window, element = _paired(_settings())
    rows = []
    for node, depth in _walk(element):
        frame = _frame(node)
        rows.append([depth, _attr(node, 'AXRole'), _attr(node, 'AXSubrole'),
                     [round(v) for v in frame] if frame else None, len(_attr(node, 'AXChildren') or []),
                     _attr(node, 'AXValue') is not None, _settable(node, 'AXFocused')])
    return rows


def _probe():
    chat = _Chat(read_only=True)
    try:
        chat.activate()
        raw = chat.read_back()
        return {'copied': bool(raw), 'chars': len(raw), 'parsed_messages': len(parser.parse_clipboard_messages(raw))}
    finally:
        chat.close()


def report(probe=False):
    """Facts for fixing this layer on a real Mac. No chat text, no window titles, no AX strings."""
    from . import __version__
    settings = runtime.read_json('delivery.json', {})
    state = permissions(prompt=False)
    found = lambda: any(w['hwnd'] == settings.get('window_id') and w['pid'] == settings.get('process_id')
                        and w['title'] == settings.get('room_name') for w in windows())
    data = {'macos': platform.mac_ver()[0], 'machine': platform.machine(), 'python': sys.version.split()[0],
            'talkturn': __version__, 'permissions': state, 'kakao_running': kakao_running(),
            'kakao_version': _guarded(_kakao_version), 'kakao_windows': len(windows()),
            'paired_window_found': _guarded(found),
            'paired_window_tree': _guarded(_tree) if state['accessibility'] else None,
            'native_status_error': (runtime.read_json('native-status.json', {}) or {}).get('error'),
            'reply_log': [{'stage': item.get('stage'), 'gate': item.get('gate')}
                          for item in (runtime.read_json('reply-log.json', []) or [])[-20:]]}
    if probe:
        data['probe'] = _guarded(_probe)
    runtime.write_json('mac-report.json', data)
    return runtime.ROOT / 'mac-report.json'


if __name__ == '__main__':
    command = sys.argv[1:2]
    if command == ['capture']:
        capture()
    elif command == ['report']:
        print(report('--probe' in sys.argv[2:]))
    else:
        sys.stdin.reconfigure(encoding='utf-8')
        try:
            receipt = send(json.load(sys.stdin))
            (runtime.ROOT / 'native-status.json').write_text(json.dumps(safe_status(receipt)), encoding='utf-8')
            print(json.dumps(receipt))
        except Exception as error:
            receipt = {'verified': False, 'error': str(error)}
            (runtime.ROOT / 'native-status.json').write_text(json.dumps(receipt), encoding='utf-8')
            print(json.dumps(receipt), file=sys.stderr)
            sys.exit(1)
