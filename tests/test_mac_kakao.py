"""macOS desktop layer against TextEdit as a stand-in for Kakao. Needs a real macOS session."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

DARWIN = sys.platform == 'darwin'
if DARWIN:
    from PIL import Image, ImageDraw, ImageFont
    from AppKit import (NSApplicationActivateIgnoringOtherApps, NSPasteboard, NSPasteboardTypePNG,
                        NSPasteboardTypeString, NSRunningApplication)
    from talkturn import mac_kakao


def wait_for(check, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        mac_kakao._sleep(0.1)  # pump the run loop so NSWorkspace sees app switches
    return check()


@unittest.skipUnless(DARWIN, 'macOS only')
class MacKakaoTest(unittest.TestCase):
    editor = False
    folder = None

    @classmethod
    def setUpClass(cls):
        # Never touch a TextEdit the developer already has open.
        if subprocess.run(['pgrep', '-x', 'TextEdit'], capture_output=True).returncode == 0:
            return
        cls.folder = tempfile.TemporaryDirectory()
        document = Path(cls.folder.name) / 'talkturn-standin.txt'
        document.write_text('', encoding='utf-8')
        # `open` goes through LaunchServices: no Automation consent prompt (osascript hung on CI waiting for one)
        cls.editor = subprocess.run(['open', '-a', 'TextEdit', str(document)], capture_output=True, timeout=30).returncode == 0

    @classmethod
    def tearDownClass(cls):
        if cls.editor:
            for app in NSRunningApplication.runningApplicationsWithBundleIdentifier_('com.apple.TextEdit'):
                app.forceTerminate()  # we launched it for a temp file only: no save prompt to answer
        if cls.folder:
            cls.folder.cleanup()

    def textedit(self):
        if not self.editor:
            self.skipTest('TextEdit was already running or could not be opened')
        state = mac_kakao.permissions()
        if not (state['screen'] and state['accessibility']):
            self.skipTest('Screen Recording / Accessibility not granted to this process')
        rows = wait_for(lambda: mac_kakao.windows(owner='TextEdit'), 5)
        if not rows:
            self.skipTest('TextEdit window is not visible to this session')
        return rows[0]

    def test_ocr_finds_korean_words_inside_the_image(self):
        font_path = os.environ.get('TALKTURN_FONT')
        if not font_path:
            self.skipTest('TALKTURN_FONT not set')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'ocr.png'
            with Image.new('RGB', (900, 200), 'white') as image:
                ImageDraw.Draw(image).text((30, 50), '안녕하세요 톡턴 테스트', font=ImageFont.truetype(font_path, 56), fill='black')
                image.save(path)
            result = mac_kakao.ocr(path)
        self.assertIsNotNone(result)
        self.assertEqual(result['angle'], 0)
        self.assertTrue(result['words'])
        for word in result['words']:
            self.assertGreaterEqual(word['x'], -1)
            self.assertGreaterEqual(word['y'], -1)
            self.assertLessEqual(word['x'] + word['width'], 901)
            self.assertLessEqual(word['y'] + word['height'], 201)
        self.assertTrue(any(any('가' <= char <= '힣' for char in word['text']) for word in result['words']))

    def test_pasteboard_snapshot_restores_text_and_png(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'one.png'
            Image.new('RGB', (8, 8), 'red').save(path)
            board = NSPasteboard.generalPasteboard()
            original = mac_kakao.snapshot()
            try:
                mac_kakao._set_text('복원 확인 abc')
                saved_text = mac_kakao.snapshot()
                mac_kakao._set_image(path)
                saved_image = mac_kakao.snapshot()
                mac_kakao._set_text('덮어씀')
                mac_kakao.restore(saved_text)
                self.assertEqual(board.stringForType_(NSPasteboardTypeString), '복원 확인 abc')
                mac_kakao.restore(saved_image)
                self.assertIsNotNone(board.dataForType_(NSPasteboardTypePNG))
                self.assertGreater(board.dataForType_(NSPasteboardTypePNG).length(), 0)
            finally:
                mac_kakao.restore(original)

    def test_finds_text_area_and_reads_a_guarded_korean_paste(self):
        window = self.textedit()
        element = mac_kakao._window_el(window)
        self.assertIsNotNone(element)
        edit, _ = mac_kakao.chat_parts(element)
        self.assertIsNotNone(edit)
        self.assertEqual(mac_kakao._attr(edit, 'AXRole'), 'AXTextArea')
        original = mac_kakao.snapshot()
        try:
            mac_kakao._set_text('톡턴 붙여넣기 확인')
            NSRunningApplication.runningApplicationWithProcessIdentifier_(window['pid']).activateWithOptions_(
                NSApplicationActivateIgnoringOtherApps)
            mac_kakao.AXUIElementSetAttributeValue(edit, 'AXFocused', True)
            if not wait_for(lambda: mac_kakao._frontmost_pid() == window['pid'], 3):
                self.skipTest('TextEdit could not be brought to the front')
            mac_kakao._key(mac_kakao.KEY_V, mac_kakao.kCGEventFlagMaskCommand)
            value = wait_for(lambda: mac_kakao._attr(edit, 'AXValue') or '', 3)
            self.assertEqual(str(value).strip(), '톡턴 붙여넣기 확인')
        finally:
            mac_kakao.restore(original)

    def test_screencapture_of_the_window_is_not_empty(self):
        window = self.textedit()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'window.png'
            result = subprocess.run(['screencapture', '-x', '-o', '-l', str(window['hwnd']), str(path)],
                                    capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertGreater(path.stat().st_size, 0)
            with Image.open(path) as image:
                self.assertGreater(image.width, 0)

    def test_mouse_lock_starts_and_ends_with_reply_enabled(self):
        if not mac_kakao.permissions()['accessibility']:
            self.skipTest('Accessibility not granted: an event tap cannot be created')
        if mac_kakao.lock_active():
            self.skipTest('a mouse lock is already active on this machine')
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / 'reply-enabled').write_text('', encoding='utf-8')
            child = subprocess.Popen([sys.executable, '-m', 'talkturn.mouse_lock', str(os.getpid())],
                                     env=dict(os.environ, TALKTURN_HOME=home), stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                if not wait_for(lambda: mac_kakao.lock_active() or child.poll() is not None, 3) or child.poll() is not None:
                    self.skipTest('mouse lock could not start: ' + (child.communicate(timeout=5)[1] or b'').decode(errors='replace')[-200:])
                self.assertTrue(mac_kakao.lock_active())
                (Path(home) / 'reply-enabled').unlink()
                self.assertTrue(wait_for(lambda: not mac_kakao.lock_active(), 3))
            finally:
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=10)

    def test_idle_seconds_is_not_negative(self):
        self.assertGreaterEqual(mac_kakao.idle_seconds(), 0)


if __name__ == '__main__':
    unittest.main()
