from contextlib import ExitStack
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch,Mock
from rich.console import Console
from talkturn import account_menu,providers,runtime,cli,terminal,service


class AccountMenuTest(unittest.TestCase):
    def test_new_account_saves_current_login_then_logs_in_and_registers(self):
        console=Console(file=io.StringIO())
        events=[]
        with patch.object(account_menu,'choice_menu',return_value='login'), \
             patch.object(providers,'accounts',return_value={'claude':[{'number':1}]}), \
             patch.object(providers,'login_state',return_value=True), \
             patch.object(providers,'add_account',side_effect=lambda *a,**kw:events.append(('add',kw)) or {'number':2,'label':'second'}), \
             patch.object(account_menu,'executable',return_value='claude.exe'), \
             patch.object(account_menu.subprocess,'run',side_effect=lambda args,**kw:events.append(('login',args)) or subprocess.CompletedProcess(args,0)), \
             patch.object(console,'input',return_value='second'):
            result=account_menu.add(console,'claude')
        self.assertEqual(events,[('add',{}),('login',['claude.exe','auth','login']),('add',{'alias':'second'})])
        self.assertIn('계정 추가 완료',result)
        with patch.object(account_menu,'choice_menu',return_value=None),patch.object(providers,'add_account') as add:
            self.assertIn('취소',account_menu.add(console)); add.assert_not_called()
        with patch.object(account_menu,'choice_menu',return_value='login'), \
             patch.object(providers,'accounts',return_value={'claude':[]}), \
             patch.object(providers,'login_state',return_value=True), \
             patch.object(providers,'add_account',return_value={'number':1}) as add, \
             patch.object(account_menu,'executable',return_value='claude.exe'), \
             patch.object(account_menu.subprocess,'run',side_effect=KeyboardInterrupt):
            self.assertIn('취소',account_menu.add(console))
            self.assertEqual(add.call_count,1)

    def test_switch_uses_chosen_account_and_first_setup_reuses_paired_chat(self):
        row={'number':2,'label':'Work','active':False,'disabled':False,'limits':[]}
        with patch.object(providers,'claude_usage',return_value={'accounts':[row]}), \
             patch.object(account_menu,'choice_menu',return_value=row), \
             patch.object(providers,'switch_account',return_value={'scope':'existing sessions unchanged'}) as switch:
            account_menu.switch(Console(file=io.StringIO()),'claude')
        switch.assert_called_once_with('claude',2,expected_label='Work')
        with patch.object(providers,'claude_usage',return_value={'accounts':[dict(row,active=True)]}), \
             patch.object(account_menu,'choice_menu',return_value=dict(row,active=True)), \
             patch.object(providers,'switch_account') as switch:
            self.assertIn('이미 사용 중',account_menu.switch(Console(file=io.StringIO())))
            switch.assert_not_called()
        with tempfile.TemporaryDirectory() as folder,patch.object(runtime,'ROOT',Path(folder)), \
             patch.object(cli,'choose_relay_model',return_value='claude-haiku-5-5'), \
             patch.object(cli.sys.stdin,'isatty',return_value=True), \
             patch.object(service,'delivery_issues',return_value=[]),patch.object(service,'pair') as pair, \
             patch.object(service,'chat_windows') as rooms,patch.object(service,'install_hooks'),patch.object(service,'startup'), \
             patch.object(service,'start',return_value='connected'):
            runtime.write_json('delivery.json',{'window_id':1,'room_name':'Self','model':'gpt-6-luna'})
            self.assertIn('claude-haiku-5-5',cli.setup(['--no-startup']))
            self.assertEqual(runtime.read_json('delivery.json')['model'],'claude-haiku-5-5')
            pair.assert_not_called(); rooms.assert_not_called()

    def test_ten_row_pages_use_local_numbers_and_keep_the_tenth_row_visible(self):
        from types import SimpleNamespace
        rows=[{'provider':'codex','code':f'X-{i:08X}','name':f'task-{i:02d}','state':'대기','model':None} for i in range(1,24)]
        view={'rows':rows,'active_provider':'codex','selected':None,'watched':[],'usage':{},'waiting':[],
              'compact':True,'focused_code':rows[0]['code'],'kakao_issues':['카톡 연결 중지 · start를 입력하세요.']}
        console=Console(file=io.StringIO(),force_terminal=True,legacy_windows=False,width=80,height=24)
        text='\n'.join(''.join(segment.text for segment in line) for line in console.render_lines(terminal.dashboard(console,view,render_only=True,max_rows=10),console.options))
        self.assertIn('task-10',text); self.assertNotIn('task-11',text)
        keys=['+','\xe0','I','\xe0','Q']+['\xe0','P']*10+['1','0','\r']
        with patch.object(terminal.sys.stdin,'isatty',return_value=True),patch.dict('sys.modules',{'msvcrt':SimpleNamespace(getwch=lambda:keys.pop(0),kbhit=lambda:bool(keys))}):
            chosen=terminal.keyboard_pick(console,view)
        self.assertEqual(chosen,rows[19]['code'])
        self.assertNotIn('[+]',console.file.getvalue())
        self.assertNotIn('PgUp',console.file.getvalue())
        self.assertNotIn('PgDn',console.file.getvalue())


if __name__=='__main__':
    unittest.main()
