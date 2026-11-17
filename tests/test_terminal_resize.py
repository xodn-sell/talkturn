import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from rich.console import Console
from talkturn import terminal


class TerminalResizeTest(unittest.TestCase):
    def test_idle_resize_repaints_once_before_key_and_preserves_input(self):
        for surface in ('picker','choice','alert'):
            with self.subTest(surface=surface):
                console=Console(file=io.StringIO(),force_terminal=True,legacy_windows=False,width=80,height=24)
                rows=[{'provider':'codex','session_id':f'fixture-{i}','code':f'X-{i:08X}',
                       'name':f'Task {i:02d}','model':'fixture-model','effort':'high'} for i in range(1,11)]
                view={'rows':rows,'watched':[rows[0]],'usage':{},'waiting':[]}
                if surface=='alert':
                    view['connection_alert']='채팅 창이 닫혔습니다.'
                keys=[]
                read=Mock(side_effect=lambda:keys.pop(0))
                keyboard=SimpleNamespace(getwch=read,kbhit=lambda:bool(keys))
                ticks=0
                def idle(delay):
                    nonlocal ticks
                    ticks+=1
                    self.assertEqual(delay,.1)
                    read.assert_not_called()
                    # Idle time alone never redraws; resizing repaints all new rows once.
                    self.assertEqual(updates.call_count,24 if ticks<=2 else 24+32)
                    if ticks==2:
                        console.width=100
                        console.height=32
                    elif ticks==4:
                        keys.append('\r')
                    self.assertLessEqual(ticks,4,'keyboard polling did not consume input')
                with patch.object(terminal.os,'name','nt'),patch.object(terminal.sys.stdin,'isatty',return_value=True), \
                     patch.dict('sys.modules',{'msvcrt':keyboard}),patch.object(terminal.time,'sleep',side_effect=idle), \
                     patch.object(terminal,'read_json',return_value={}), \
                     patch('talkturn.computer.computer_label',return_value='컴퓨터: 테스트 PC'), \
                     patch.object(console,'update_screen_lines') as updates:
                    if surface=='choice':
                        result=terminal.choice_menu(console,'모델 선택',[('first','첫째'),('second','둘째')],selected=1)
                        self.assertEqual(result,'second')
                    else:
                        result=terminal.keyboard_pick(console,view)
                        self.assertEqual(result,[rows[0]['code']])
                self.assertEqual(read.call_count,1)
                self.assertEqual(updates.call_count,56)
                lines={call.kwargs['y']:''.join(segment.text for line in call.args[0] for segment in line)
                       for call in updates.call_args_list[-32:]}
                if surface=='picker':
                    self.assertIn('입력:',lines[30])
                    self.assertTrue(any('Task 10' in line for line in lines.values()))
                    bottom=next(y for y,line in lines.items() if '╰' in line and '페이지' in line)
                    shortcut=next(y for y,line in lines.items() if '[↑↓]' in line)
                    self.assertEqual(shortcut,bottom+2)
                    self.assertFalse(lines[bottom+1].strip())
                elif surface=='alert':
                    self.assertTrue(any('[Enter]' in line and '[Esc]' in line for line in lines.values()))


if __name__=='__main__':
    unittest.main()
