from contextlib import ExitStack
import ctypes
import hashlib
import os
from pathlib import Path
import runpy
import tempfile
import time
import unittest
from unittest.mock import patch,Mock
from PIL import Image
from talkturn import cache,render,runtime


class ResourceTest(unittest.TestCase):
    def test_environment_rename_prefers_new_names_and_preserves_legacy_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            legacy=root/'TokTurn custom';current=root/'new custom'
            legacy_bin=root/'legacy.exe';legacy_bin.touch()
            current_bin=root/'current.exe';current_bin.touch()
            legacy_font=root/'legacy.ttf';legacy_font.touch()
            current_font=root/'current.ttf';current_font.touch()
            for both in (False,True):
                environment={'USERPROFILE':str(root),'LOCALAPPDATA':str(root/'state'),'TOKTURN_HOME':str(legacy),
                             'TOKTURN_FONT':str(legacy_font)}
                for provider in ('CODEX','CLAUDE'):
                    environment['TOKTURN_'+provider+'_BIN']=str(legacy_bin)
                    if both:environment['TALKTURN_'+provider+'_BIN']=str(current_bin)
                if both:environment.update(TALKTURN_HOME=str(current),TALKTURN_FONT=str(current_font))
                with self.subTest(new_names=both),patch.dict(os.environ,environment,clear=True), \
                     patch.object(runtime.shutil,'which',return_value=None),patch.object(Path,'glob',return_value=[]):
                    isolated=runpy.run_path(runtime.__file__)
                    self.assertEqual(isolated['ROOT'],(current if both else legacy).resolve())
                    for provider in ('codex','claude'):
                        self.assertEqual(isolated['executable'](provider),str(current_bin if both else legacy_bin))
                    with patch.object(render.ImageFont,'truetype',side_effect=RuntimeError('font probe')) as font:
                        with self.assertRaisesRegex(RuntimeError,'font probe'):render.text_images('sample','unused')
                        font.assert_called_once_with(str(current_font if both else legacy_font),42)
                    with patch.object(Path,'is_file',return_value=False):
                        with self.assertRaises(RuntimeError) as error:render.text_images('sample','unused')
                        self.assertIn('TALKTURN_FONT',str(error.exception))
                        self.assertNotIn('TOKTURN_FONT',str(error.exception))
                        self.assertNotIn(str(root),str(error.exception))
                        with self.assertRaises(RuntimeError) as error:isolated['executable']('codex')
                        self.assertIn('TALKTURN_CODEX_BIN',str(error.exception))
                        self.assertNotIn(str(root),str(error.exception))
            with patch.dict(os.environ,{'USERPROFILE':str(root),'LOCALAPPDATA':str(root/'state')},clear=True):
                self.assertEqual(runpy.run_path(runtime.__file__)['ROOT'],(root/'state'/'TalkTurn').resolve())

    def test_only_verified_generated_images_and_old_orphans_are_trimmed(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(runtime,'ROOT',Path(folder)),patch.object(cache,'ROOT',Path(folder)):
            root=Path(folder); (root/'rendered').mkdir(); (root/'sent-images').mkdir()
            prefix=hashlib.sha256(b'verified').hexdigest()[:20]
            sent=root/'rendered'/f'{prefix}-1.png'; sent.write_bytes(b'generated')
            pending=root/'rendered'/('1'*20+'-1.png'); pending.write_bytes(b'pending')
            source=root/'rendered'/'my-photo.png'; source.write_bytes(b'original')
            retained=root/'sent-images'/('2'*64+'.png'); retained.write_bytes(b'retained')
            orphan=root/'sent-images'/('3'*64+'.png'); orphan.write_bytes(b'orphan')
            old=time.time()-2*86400
            for path in (retained,orphan): os.utime(path,(old,old))
            runtime.write_json('native-delivered.json',['verified'])
            runtime.write_json('result-origins.json',[{'images':[str(retained)]}])
            result=cache.trim_generated_images()
            self.assertEqual(result,{'removed_files':2,'freed_bytes':len(b'generatedorphan')})
            self.assertFalse(sent.exists()); self.assertFalse(orphan.exists())
            self.assertTrue(all(path.exists() for path in (pending,source,retained)))

    def test_capture_preserves_crop_and_releases_bitmap_without_persisting_bmp(self):
        import win32gui,win32ui,win32process
        from talkturn._kakao import controller
        with ExitStack() as stack:
            root=Path(stack.enter_context(tempfile.TemporaryDirectory()))
            stack.enter_context(patch.object(runtime,'ROOT',root))
            runtime.write_json('delivery.json',{'window_id':1,'process_id':123,'room_name':'Self'})
            stack.enter_context(patch.object(win32gui,'IsWindow',return_value=True))
            stack.enter_context(patch.object(win32process,'GetWindowThreadProcessId',return_value=(0,123)))
            stack.enter_context(patch.object(win32gui,'GetWindowText',return_value='Self'))
            stack.enter_context(patch.object(win32gui,'GetWindowRect',side_effect=lambda hwnd:(100,100,900,1000) if hwnd==1 else (110,150,890,950)))
            stack.enter_context(patch.object(win32gui,'GetWindowDC',return_value=99))
            dc=Mock(); bitmap=Mock()
            stack.enter_context(patch.object(win32ui,'CreateDCFromHandle',return_value=dc))
            stack.enter_context(patch.object(win32ui,'CreateBitmap',return_value=bitmap))
            deleted=stack.enter_context(patch.object(win32gui,'DeleteObject'))
            released=stack.enter_context(patch.object(win32gui,'ReleaseDC'))
            stack.enter_context(patch.object(controller,'find_child_window_recursive',return_value=2))
            stack.enter_context(patch.object(ctypes.windll.user32,'SetProcessDPIAware',return_value=1))
            order=[]
            scroll=stack.enter_context(patch('talkturn.native_send.scroll_to_latest',side_effect=lambda *args:order.append('scroll') or 2))
            stack.enter_context(patch.object(ctypes.windll.user32,'PrintWindow',side_effect=lambda *args:order.append('capture') or 1))
            def save(_,path):
                with Image.new('RGB',(800,900),'white') as frame:
                    frame.putpixel((10,150),(255,0,0)); frame.save(path)
            bitmap.SaveBitmapFile.side_effect=save
            runpy.run_module('talkturn.capture_self',run_name='__main__')
            with Image.open(root/'reply-view.png') as image:
                self.assertEqual(image.size,(780,700)); self.assertEqual(image.getpixel((0,0)),(255,0,0))
            self.assertFalse((root/'reply-view.bmp').exists())
            scroll.assert_called_once_with(1,123,'Self')
            self.assertEqual(order,['scroll','capture'])
            deleted.assert_called_once(); released.assert_called_once_with(1,99)

    def test_scroll_targets_only_the_verified_message_list_without_input(self):
        import win32gui,win32process,win32con
        from talkturn import native_send
        from talkturn._kakao import controller
        with patch.object(win32gui,'IsWindow',return_value=True), \
             patch.object(win32gui,'GetWindowText',return_value='Self'), \
             patch.object(win32process,'GetWindowThreadProcessId',return_value=(0,123)) as process, \
             patch.object(controller,'find_child_window_recursive',return_value=2), \
             patch.object(win32gui,'IsChild',return_value=True) as child, \
             patch.object(win32gui,'SendMessageTimeout') as scroll, \
             patch.object(controller,'_send_ctrl_key_combo') as keys, \
             patch.object(controller,'bring_window_to_front') as focus, \
             patch.object(native_send.time,'sleep'):
            self.assertEqual(native_send.scroll_to_latest(1,123,'Self'),2)
            scroll.assert_called_once_with(2,win32con.WM_VSCROLL,win32con.SB_BOTTOM,0,
                                           win32con.SMTO_ABORTIFHUNG,1000)
            scroll.reset_mock()
            with self.assertRaises(RuntimeError):
                native_send.scroll_to_latest(1,123,'Other')
            process.return_value=(0,456)
            with self.assertRaises(RuntimeError):
                native_send.scroll_to_latest(1,123,'Self')
            process.return_value=(0,123)
            child.return_value=False
            with self.assertRaises(RuntimeError):
                native_send.scroll_to_latest(1,123,'Self')
            scroll.assert_not_called(); keys.assert_not_called(); focus.assert_not_called()


if __name__=='__main__':
    unittest.main()
