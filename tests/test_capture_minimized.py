from contextlib import ExitStack
import ctypes
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image
import win32con
import win32gui
import win32process
import win32ui

from talkturn import runtime


class MinimizedCaptureTest(unittest.TestCase):
    def test_capture_restores_only_minimized_window_without_activation_even_on_failure(self):
        for minimized,success in ((True,True),(True,False),(False,True)):
            with self.subTest(minimized=minimized,success=success),ExitStack() as stack:
                root=Path(stack.enter_context(tempfile.TemporaryDirectory()))
                stack.enter_context(patch.object(runtime,'ROOT',root))
                runtime.write_json('delivery.json',{'window_id':1,'process_id':123,'room_name':'Self'})
                stack.enter_context(patch.object(win32gui,'IsWindow',return_value=True))
                stack.enter_context(patch.object(win32process,'GetWindowThreadProcessId',return_value=(9,123)))
                stack.enter_context(patch.object(win32gui,'GetWindowText',return_value='Self'))
                stack.enter_context(patch.object(win32gui,'IsIconic',side_effect=[True,False] if minimized else [False]))
                order=[]
                show=stack.enter_context(patch.object(win32gui,'ShowWindow',side_effect=lambda hwnd,flag:order.append(flag)))
                foreground=stack.enter_context(patch.object(win32gui,'SetForegroundWindow'))
                focus=stack.enter_context(patch.object(win32gui,'SetFocus'))
                stack.enter_context(patch.object(ctypes.windll.user32,'SetProcessDPIAware',return_value=1))
                stack.enter_context(patch('talkturn.native_send.scroll_to_latest',side_effect=lambda *args:order.append('scroll') or 2))
                stack.enter_context(patch.object(win32gui,'GetWindowRect',return_value=(0,0,80,90)))
                stack.enter_context(patch.object(win32gui,'GetWindowDC',return_value=99))
                dc=Mock(); bitmap=Mock()
                stack.enter_context(patch.object(win32ui,'CreateDCFromHandle',return_value=dc))
                stack.enter_context(patch.object(win32ui,'CreateBitmap',return_value=bitmap))
                deleted=stack.enter_context(patch.object(win32gui,'DeleteObject'))
                released=stack.enter_context(patch.object(win32gui,'ReleaseDC',side_effect=lambda *args:order.append('released')))
                stack.enter_context(patch.object(ctypes.windll.user32,'PrintWindow',side_effect=lambda *args:order.append('capture') or success))
                def save(_,path):
                    with Image.new('RGB',(80,90),'white') as image:
                        image.save(path)
                bitmap.SaveBitmapFile.side_effect=save
                if success:
                    runpy.run_module('talkturn.capture_self',run_name='__main__')
                    self.assertTrue((root/'reply-view.png').is_file())
                else:
                    with self.assertRaises(AssertionError):
                        runpy.run_module('talkturn.capture_self',run_name='__main__')
                expected=['scroll','capture','released']
                if minimized:
                    expected=[win32con.SW_SHOWNOACTIVATE,*expected,win32con.SW_SHOWMINNOACTIVE]
                else:
                    show.assert_not_called()
                self.assertEqual(order,expected)
                foreground.assert_not_called(); focus.assert_not_called()
                deleted.assert_called_once(); released.assert_called_once_with(1,99)
                dc.DeleteDC.assert_called_once(); dc.CreateCompatibleDC.return_value.DeleteDC.assert_called_once()


if __name__=='__main__':
    unittest.main()
