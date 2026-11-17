"""Metadata-only WinEvent gate for one paired Kakao chat; never polls its text.

Hook installation alone does not prove that a Kakao version emits message-change
events. No focus, selection, scrolling, screenshot or clipboard fallback is used.
"""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import time

import pythoncom
import win32gui
import win32process


LIST_CLASS='EVA_VH_ListControl_Dblclk'
CHANGE_EVENTS=frozenset({0x8000,0x8004,0x800C,0x800E,0x8019})
CALLBACK=ctypes.WINFUNCTYPE(None,wintypes.HANDLE,wintypes.DWORD,wintypes.HWND,
                          wintypes.LONG,wintypes.LONG,wintypes.DWORD,wintypes.DWORD)


class ChatWindowChanged(RuntimeError):
    pass


class ChatEvents:
    def __init__(self,settings,debounce=.8):
        self.hwnd=settings.get('window_id')
        self.pid=settings.get('process_id')
        self.title=settings.get('room_name')
        self.list_hwnd=None
        self.debounce=debounce
        self.event_count=0
        self._pending=None
        self._muted=False
        self._ignore_through=None
        self._invalid=False
        self._hook=None
        self._callback=None

    def _validate(self):
        try:
            valid=(win32gui.IsWindow(self.hwnd)
                   and win32process.GetWindowThreadProcessId(self.hwnd)[1]==self.pid
                   and win32gui.GetWindowText(self.hwnd)==self.title)
            if self.list_hwnd:
                valid=valid and win32gui.IsChild(self.hwnd,self.list_hwnd) and (
                    win32process.GetWindowThreadProcessId(self.list_hwnd)[1]==self.pid
                    and win32gui.GetClassName(self.list_hwnd)==LIST_CLASS)
        except Exception:
            valid=False
        if self._invalid or not valid:
            raise ChatWindowChanged('연결한 카톡 창이 바뀌었습니다. 수신 확인을 멈췄습니다. setup으로 다시 연결하세요.')

    def _on_event(self,hook,event,hwnd,object_id,child_id,event_thread,event_time):
        if hwnd==self.hwnd or hwnd==self.list_hwnd:
            try:
                self._validate()
            except ChatWindowChanged:
                self._invalid=True
                return
        if self._muted or event not in CHANGE_EVENTS:
            return
        try:
            if not (hwnd==self.list_hwnd or win32gui.IsChild(self.list_hwnd,hwnd)):
                return
            if win32process.GetWindowThreadProcessId(hwnd)[1]!=self.pid:
                return
        except Exception:
            return
        if self._ignore_through is not None and ctypes.c_int32(event_time-self._ignore_through).value<=0:
            return
        self.event_count+=1
        self._pending=time.monotonic()

    def __enter__(self):
        self._validate()
        matches=[]
        def collect(hwnd,_):
            if win32gui.GetClassName(hwnd)==LIST_CLASS:
                matches.append(hwnd)
        win32gui.EnumChildWindows(self.hwnd,collect,None)
        if len(matches)!=1:
            raise ChatWindowChanged('연결한 카톡 메시지 목록을 하나로 확인할 수 없습니다.')
        self.list_hwnd=matches[0]
        self._validate()
        self._user=ctypes.WinDLL('user32',use_last_error=True)
        self._user.SetWinEventHook.argtypes=[wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE,
                                           CALLBACK,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD]
        self._user.SetWinEventHook.restype=wintypes.HANDLE
        self._user.UnhookWinEvent.argtypes=[wintypes.HANDLE]
        self._callback=CALLBACK(self._on_event)
        self._hook=self._user.SetWinEventHook(0x8000,0x8019,None,self._callback,self.pid,0,0)
        if not self._hook:
            raise RuntimeError('카톡 변경 이벤트를 구독하지 못했습니다. 주기적 내용 읽기로 대체하지 않습니다.')
        return self

    def pump(self):
        self._validate()
        pythoncom.PumpWaitingMessages()
        self._validate()

    def take_change(self):
        if self._pending is None or time.monotonic()-self._pending<self.debounce:
            return False
        self._pending=None
        return True

    @contextmanager
    def suppress(self):
        self._muted=True
        self._pending=None
        try:
            yield
        finally:
            # Delayed WinEvents retain their generation time; discard our own reads/sends.
            self._ignore_through=ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
            self._pending=None
            try:
                self.pump()
            finally:
                self._muted=False

    def __exit__(self,*_):
        if self._hook:
            self._user.UnhookWinEvent(self._hook)
            self._hook=None
