from contextlib import ExitStack
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from rich.console import Console
from talkturn import computer, commands, runtime, cli, terminal, service


class ComputerNameTest(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack()
        self.addCleanup(self.stack.close)
        self.root=Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(runtime,'ROOT',self.root))
        self.console=Console(file=io.StringIO(),force_terminal=True,legacy_windows=False,width=80,height=24)
        self.stack.enter_context(patch.object(cli,'console',self.console))

    def test_default_validation_and_update_preserve_other_settings(self):
        self.assertEqual(computer.computer_label(),'컴퓨터: 내 PC')
        runtime.write_json('delivery.json',{'model':'gpt-6-luna','window_id':42})
        self.assertEqual(computer.set_computer_name('  작업용 노트북  '),'작업용 노트북')
        saved=runtime.read_json('delivery.json')
        self.assertEqual(saved,{'model':'gpt-6-luna','window_id':42,'computer_name':'작업용 노트북'})
        for value in ('', '   ', 'x'*41, 'PC\n명령', '\tPC', 'PC\r', 'PC\x1b', '\u200b', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                computer.set_computer_name(value)
        self.assertEqual(runtime.read_json('delivery.json'),saved)
        runtime.write_json('delivery.json',{'computer_name':'unsafe\nname'})
        self.assertEqual(computer.computer_name(),'내 PC')

    def test_commands_and_setup_reuse_the_saved_name(self):
        parts=commands.parse('컴퓨터이름 작업  PC')
        self.assertEqual(parts,['computer-name','작업  PC'])
        self.assertEqual(commands.execute(parts),'컴퓨터: 작업  PC')
        self.assertEqual(cli.dispatch(['computer-name']),'컴퓨터: 작업  PC')
        with patch.object(service,'pair'),patch.object(service,'install_hooks'),patch.object(service,'startup'), \
             patch.object(service,'start',return_value='connected'),patch.object(cli.sys.stdin,'isatty',return_value=False):
            cli.setup(['--self-chat','Self','--relay-model','luna','--computer-name','집 PC'])
        self.assertEqual(computer.computer_name(),'집 PC')
        runtime.write_json('delivery.json',{'window_id':42,'room_name':'Self','model':'gpt-6-luna'})
        with patch.object(service,'delivery_issues',return_value=[]),patch.object(service,'pair'), \
             patch.object(service,'install_hooks'),patch.object(service,'startup'), \
             patch.object(service,'start',return_value='connected'),patch.object(cli.sys.stdin,'isatty',return_value=True), \
             patch.object(cli.console,'input') as prompt:
            cli.setup(['--relay-model','luna'])
            prompt.assert_not_called()
        (self.root/'delivery.json').unlink()
        def migrate():
            self.assertFalse((self.root/'delivery.json').exists())
            runtime.write_json('delivery.json',{'room_name':'Self'})
        with patch.object(service,'migrate_legacy',side_effect=migrate),patch.object(service,'bridge_legacy_hook'), \
             patch.object(service,'pair'),patch.object(service,'install_hooks'),patch.object(service,'startup'), \
             patch.object(service,'start',return_value='connected'),patch.object(cli.sys.stdin,'isatty',return_value=False):
            cli.setup(['--migrate','--computer-name','이전 PC','--relay-model','luna'])
        self.assertEqual(computer.computer_name(),'이전 PC')
        with patch.object(cli.console,'input',side_effect=KeyboardInterrupt):
            self.assertIn('취소',cli.edit_computer_name())
        self.assertEqual(computer.computer_name(),'이전 PC')

    def test_fresh_interactive_setup_asks_once_and_enter_keeps_default(self):
        def pair(title):
            data=runtime.read_json('delivery.json',{})
            runtime.write_json('delivery.json',{**data,'window_id':42,'room_name':title})
        with patch.object(service,'pair',side_effect=pair),patch.object(service,'install_hooks'),patch.object(service,'startup'), \
             patch.object(service,'start',return_value='connected'),patch.object(service,'delivery_issues',return_value=[]), \
             patch.object(service,'chat_windows',return_value=[{'title':'Self'}]), \
             patch.object(cli.sys.stdin,'isatty',return_value=True),patch('builtins.input',return_value='1'), \
             patch.object(cli.console,'input',return_value='') as prompt:
            cli.setup(['--relay-model','luna'])
            cli.setup(['--relay-model','luna'])
            prompt.assert_called_once()
        self.assertEqual(runtime.read_json('delivery.json')['computer_name'],'내 PC')

    def test_n_key_edit_retains_draft_checks_and_ten_row_layout(self):
        computer.set_computer_name('작업 PC')
        runtime.write_json('delivery.json',{**runtime.read_json('delivery.json'),'model':'gpt-6-luna'})
        rows=[{'provider':'codex','code':f'X-{i:08X}','name':f'task-{i:02d}','state':'대기','model':None} for i in range(1,12)]
        view={'rows':rows,'active_provider':'codex','selected':None,'watched':[],'usage':{},'waiting':[],
              'compact':True,'focused_code':rows[0]['code'],'kakao_issues':[]}
        keys=[' ','n']
        keyboard=SimpleNamespace(getwch=lambda:keys.pop(0),kbhit=lambda:bool(keys))
        with patch.object(terminal.sys.stdin,'isatty',return_value=True),patch.dict('sys.modules',{'msvcrt':keyboard}):
            action=terminal.keyboard_pick(self.console,view)
        self.assertEqual(action,{'action':'computer-name','provider':'codex','checked_codes':[rows[0]['code']]})
        with patch.object(terminal,'show_dashboard',return_value=view) as load, \
             patch.object(terminal,'keyboard_pick',side_effect=[action,'']),patch.object(terminal,'response'), \
             patch.object(cli.console,'input',side_effect=['회사 PC','quit']):
            cli.interactive()
        self.assertEqual(computer.computer_name(),'회사 PC')
        load.assert_called_once()
        self.assertEqual(view['draft_checked_codes'],[rows[0]['code']])
        lines=self.console.render_lines(terminal.dashboard(self.console,view,render_only=True,max_rows=10),self.console.options)
        text='\n'.join(''.join(segment.text for segment in line) for line in lines)
        self.assertIn('컴퓨터: 회사 PC',text)
        self.assertIn('task-10',text)
        self.assertNotIn('task-11',text)
        self.assertLessEqual(len(lines),24)


if __name__=='__main__':
    unittest.main()
