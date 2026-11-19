from contextlib import ExitStack, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from talkturn import cli, commands, providers, runtime, service, tasks, questions


class SectionsAndSetupTest(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack()
        self.addCleanup(self.stack.close)
        self.root=Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(runtime,'ROOT',self.root))

    def test_kakao_section_filters_work_and_queries_without_changing_reply_target(self):
        origin={'source':'claude','session_id':'original-claude','model':'claude-work'}
        original=[{'id':'abc12345','origin':origin,'text':'result','sent_at':1}]
        runtime.write_json('result-origins.json',original)
        runtime.write_json('delivery.json',{'model':'gpt-6-luna','room_name':'self'})
        tasks.record('claude',{'session_id':'original-claude','model':'claude-work','cwd':'claude-project'},'진행 중')
        tasks.record('codex',{'session_id':'codex-work','model':'codex-work','cwd':'codex-project'},'진행 중')
        settings={'claude':{'model':'claude-default'},'codex':{'model':'codex-default'},
                  'relay':{'model':'gpt-6-luna'},'scope':'새 작업 기본값'}
        with patch.object(providers,'usage',side_effect=lambda target:{target:{'limits':[]}}) as usage, \
             patch.object(providers,'login_state',return_value=False), \
             patch.object(providers,'model_settings',return_value=settings), \
             patch.object(providers,'switch_account') as switch, patch.object(providers,'set_model') as model, \
             patch('talkturn.sessions.discover',return_value={'rows':[
                 {'provider':'claude','session_id':'original-claude','name':'claude-project','code':'C-TEST','model':'claude-work','state':'진행 중'},
                 {'provider':'codex','session_id':'codex-work','name':'codex-project','code':'X-TEST','model':'codex-work','state':'진행 중'}],'errors':{}}):
            response=commands.execute(commands.parse('톡턴 section ChatGPT',remote=True),remote=True)
            self.assertIn('codex-project',response)
            self.assertNotIn('claude-project',response)
            self.assertNotIn('claude-default',response)
            self.assertEqual(runtime.read_json('ui.json')['section'],'codex')
            usage.assert_called_once_with('codex')
            commands.execute(['usage'])
            usage.assert_called_with('codex')
            commands.execute(['usage','claude'])
            usage.assert_called_with('claude')
            self.assertEqual(commands.selected_section(),'codex')
            self.assertNotIn('claude-default',commands.execute(['model']))
            switch.assert_not_called(); model.assert_not_called()
        self.assertEqual(runtime.read_json('result-origins.json'),original)
        self.assertEqual(runtime.read_json('delivery.json'),{'model':'gpt-6-luna','room_name':'self'})
        with self.assertRaises(ValueError):
            commands.select_section('unknown')
        self.assertEqual(commands.selected_section(),'codex')
        self.assertEqual(commands.parse('claude UPPERCASE'),['claude','UPPERCASE'])

    def test_initial_setup_defaults_to_luna_and_can_choose_haiku(self):
        with patch.object(cli,'confirm_connection',return_value=True), \
             patch.object(service,'pair') as pair, patch.object(service,'install_hooks'), \
             patch.object(service,'startup'),patch.object(service,'start',return_value='connected'), \
             patch.object(cli.sys.stdin,'isatty',return_value=True), \
             patch('builtins.input',side_effect=['','2']) as choose, redirect_stdout(io.StringIO()):
            first=cli.setup(['--self-chat','Self','--section','chatgpt','--no-startup'])
            self.assertEqual(runtime.read_json('delivery.json')['model'],'gpt-6-luna')
            self.assertIn('gpt-6-luna',first)
            self.assertEqual(commands.selected_section(),'codex')
            second=cli.setup(['--self-chat','Self','--no-startup'])
            self.assertIn('gpt-6-luna',second)
            self.assertEqual(choose.call_count,1)
            runtime.write_json('delivery.json',{})
            second=cli.setup(['--self-chat','Self','--no-startup'])
            self.assertEqual(runtime.read_json('delivery.json')['model'],'claude-haiku-5-5')
            self.assertIn('claude-haiku-5-5',second)
            third=cli.setup(['--self-chat','Self','--relay-model','luna','--no-startup'])
            self.assertIn('gpt-6-luna',third)
            self.assertEqual(runtime.read_json('delivery.json')['model'],'gpt-6-luna')
            self.assertEqual(pair.call_count,4)


if __name__=='__main__':
    unittest.main()
