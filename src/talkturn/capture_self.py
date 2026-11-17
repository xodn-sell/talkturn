"""Capture only the paired self-chat window for matching a new reply's quote."""
import ctypes
import json
from pathlib import Path
import tempfile
from PIL import Image
import win32gui
import win32ui
import win32process
import win32con

ctypes.windll.user32.SetProcessDPIAware()
from .runtime import ROOT as root
settings = json.loads((root/'delivery.json').read_text(encoding='utf-8'))
hwnd = settings['window_id']
assert win32gui.IsWindow(hwnd) and win32process.GetWindowThreadProcessId(hwnd)[1] == settings['process_id']
assert win32gui.GetWindowText(hwnd) == settings['room_name']
from .native_send import scroll_to_latest
was_minimized = win32gui.IsIconic(hwnd)
try:
    if was_minimized:
        # Restore pixels for PrintWindow without activating or focusing the chat.
        win32gui.ShowWindow(hwnd,win32con.SW_SHOWNOACTIVATE)
        if win32gui.IsIconic(hwnd):
            raise RuntimeError('Cannot restore self-chat for capture without activation')
    listing = scroll_to_latest(hwnd, settings['process_id'], settings['room_name'])
    left,top,right,bottom = win32gui.GetWindowRect(hwnd)
    window_dc = win32gui.GetWindowDC(hwnd)
    source = win32ui.CreateDCFromHandle(window_dc)
    memory = source.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(source,right-left,bottom-top)
    memory.SelectObject(bitmap)
    try:
        assert ctypes.windll.user32.PrintWindow(hwnd,memory.GetSafeHdc(),2)
        area=win32gui.GetWindowRect(listing)
        with tempfile.TemporaryDirectory(prefix='talkturn-capture-') as temporary:
            path=Path(temporary)/'frame.bmp'
            bitmap.SaveBitmapFile(memory,str(path))
            with Image.open(path) as frame:
                x1=max(0,area[0]-left); x2=min(frame.width,area[2]-left)
                y2=min(frame.height,area[3]-top); y1=max(0,area[1]-top,y2-700)
                if x2<=x1 or y2<=y1:
                    raise RuntimeError('Reply area is outside the captured window')
                with frame.crop((x1,y1,x2,y2)) as cropped:
                    cropped.save(root/'reply-view.png')
    finally:
        win32gui.DeleteObject(bitmap.GetHandle())
        memory.DeleteDC()
        source.DeleteDC()
        win32gui.ReleaseDC(hwnd,window_dc)
finally:
    if was_minimized and win32gui.IsWindow(hwnd):
        try:
            if (win32process.GetWindowThreadProcessId(hwnd)[1] == settings['process_id']
                    and win32gui.GetWindowText(hwnd) == settings['room_name']):
                win32gui.ShowWindow(hwnd,win32con.SW_SHOWMINNOACTIVE)
        except Exception:
            pass  # A closed/replaced window must not affect another window or hide a capture error.
