from contextlib import ExitStack
import ctypes
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from talkturn import native_send, runtime
from talkturn._kakao import controller, config


class ImagePasteGuardTest(unittest.TestCase):
    def test_pre_send_baseline_matches_receiver_occurrences_without_logging_text(self):
        from talkturn.reply_bridge import fingerprint
        raw='[me] [오전 1:00] 같은 답장\n[me] [오전 1:00] 같은 답장'
        messages=native_send.parser.parse_clipboard_messages(raw)
        hashes=native_send.baseline_fingerprints(raw)
        self.assertEqual(len(hashes),2)
        self.assertEqual(len(set(hashes)),2)
        self.assertEqual(hashes,[fingerprint({**message,'_occurrence':index}) for index,message in enumerate(messages,1)])
        receipt={'verified':True,'sent':['text'],'baseline_hashes':hashes}
        self.assertEqual(native_send.safe_status(receipt),{'verified':True,'sent':['text']})

    def test_focus_or_clipboard_change_stops_before_paste_and_enter(self):
        cases=[('focus',3,True,[10,11,20],'not focused'),
               ('sequence',2,True,[10,11,20,21],'clipboard changed'),
               ('format',2,False,[10,11,20,20],'no CF_DIB'),
               ('query',2,True,[10,11,20],'focus is unavailable')]
        for name,focused,has_dib,sequences,error in cases:
            with self.subTest(name=name),ExitStack() as stack:
                root=Path(stack.enter_context(tempfile.TemporaryDirectory()))
                stack.enter_context(patch.object(runtime,'ROOT',root))
                stack.enter_context(patch.object(native_send,'ROOT',root))
                runtime.write_json('delivery.json',{'room_name':'Self','window_id':1,'process_id':123})
                image=root/'image.png'; image.write_bytes(b'copy is mocked')
                stack.enter_context(patch.object(controller,'list_chat_windows',return_value=[{'title':'Self','hwnd':1}]))
                stack.enter_context(patch.object(controller,'find_child_window_recursive',
                                                 side_effect=lambda hwnd,kind:2 if kind==config.KAKAO_EDIT_CLASS else 3))
                stack.enter_context(patch.object(controller,'bring_window_to_front'))
                stack.enter_context(patch.object(controller,'_read_clipboard_text',return_value='[me] [오전 1:00] before'))
                stack.enter_context(patch.object(controller,'_copy_image_to_clipboard'))
                keys=Mock()
                stack.enter_context(patch.object(controller,'_user32',Mock(keybd_event=keys)))
                stack.enter_context(patch.object(native_send,'clipboard_snapshot',return_value=[]))
                restored=stack.enter_context(patch.object(native_send,'restore_clipboard'))
                stack.enter_context(patch.object(native_send,'scroll_to_latest'))
                stack.enter_context(patch.object(native_send.time,'sleep'))
                stack.enter_context(patch.object(native_send.win32process,'GetWindowThreadProcessId',return_value=(9,123)))
                stack.enter_context(patch.object(native_send.win32gui,'IsChild',return_value=True))
                stack.enter_context(patch.object(native_send.win32gui,'IsWindow',return_value=True))
                stack.enter_context(patch.object(native_send.win32gui,'IsWindowVisible',return_value=True))
                stack.enter_context(patch.object(native_send.win32gui,'IsIconic',return_value=True))
                minimize=stack.enter_context(patch.object(native_send.win32gui,'ShowWindow'))
                stack.enter_context(patch.object(native_send.win32gui,'GetForegroundWindow',return_value=1))
                stack.enter_context(patch.object(native_send.win32gui,'SetForegroundWindow'))
                stack.enter_context(patch.object(native_send.win32gui,'GetWindowRect',return_value=(0,0,300,300)))
                stack.enter_context(patch.object(native_send.win32gui,'SendMessage',return_value=0))
                stack.enter_context(patch.object(native_send.win32gui,'EnumWindows',side_effect=lambda callback,arg:callback(1,arg)))
                stack.enter_context(patch.object(native_send.win32api,'GetCursorPos',return_value=(0,0)))
                stack.enter_context(patch.object(native_send.win32api,'SetCursorPos'))
                stack.enter_context(patch.object(native_send.win32api,'mouse_event'))
                stack.enter_context(patch.object(ctypes.windll.user32,'SendMessageW',return_value=0))
                stack.enter_context(patch.object(ctypes.windll.user32,'GetClipboardSequenceNumber',side_effect=sequences))
                stack.enter_context(patch.object(native_send.win32clipboard,'IsClipboardFormatAvailable',return_value=has_dib))
                def thread_info(thread,pointer):
                    self.assertEqual(thread,9)
                    pointer._obj.hwndActive=1
                    pointer._obj.hwndFocus=focused
                    return name!='query'
                stack.enter_context(patch.object(ctypes.windll.user32,'GetGUIThreadInfo',side_effect=thread_info))
                with self.assertRaisesRegex(RuntimeError,error):
                    native_send.send({'images':[str(image)]})
                pressed=[call.args[0] for call in keys.call_args_list if not call.args[2]&config.KEYEVENTF_KEYUP]
                self.assertNotIn(0x56,pressed)
                self.assertNotIn(config.VK_RETURN,pressed)
                restored.assert_called_once_with([])
                minimize.assert_called_once_with(1,native_send.win32con.SW_SHOWMINNOACTIVE)


if __name__=='__main__':
    unittest.main()
