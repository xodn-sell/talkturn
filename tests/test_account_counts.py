import io
from threading import Barrier
import unittest
from unittest.mock import patch
from rich.console import Console
from talkturn import providers,sessions,tasks,terminal,questions


class AccountCountsTest(unittest.TestCase):
    def test_current_login_counts_once_in_parallel_and_render_never_queries(self):
        metadata={'claude':{'accounts':[
            {'number':1,'active':True,'usageStatus':'ok','usage':{}},
            {'number':2,'active':False,'usageStatus':'ok','usage':{}},
            {'number':3,'disabled':True,'usageStatus':'ok'},
            {'number':4,'usageStatus':'no_credentials'},
            {'number':5,'usageStatus':'token_expired'},
            {'number':6,'usageStatus':'relogin_required'},
            {'number':7,'usageStatus':'foreign_credential'}]},
            'codex':{'accounts':[{'number':1,'usageStatus':'ok'}]}}
        rows=[{'provider':'claude','session_id':str(i),'code':f'C-{i:08X}',
               'name':f'task-{i}','state':'대기','live':True,'reachable':True} for i in range(3)]
        codex={'source':'Codex account API','limits':[{'name':'codex 7일','remaining_pct':80}]}
        both_started=Barrier(2)
        def login(provider):
            both_started.wait(timeout=1)
            return provider=='claude'
        with patch.object(providers,'ccswap_accounts',return_value=metadata) as fetch, \
             patch.object(providers,'login_state',side_effect=login) as auth, \
             patch.object(providers,'codex_usage',return_value=codex), \
             patch.object(providers,'model_settings',return_value={}), \
             patch.object(sessions,'discover',return_value={'rows':rows,'errors':{}}), \
             patch.object(sessions,'selected',return_value=None),patch.object(sessions,'watched',return_value=[]), \
             patch.object(questions,'pending',return_value=[]):
            view=tasks.snapshot()
            self.assertEqual(view['account_counts'],{'claude':1,'codex':0})
            fetch.assert_called_once()
            self.assertCountEqual([call.args for call in auth.call_args_list],[('claude',),('codex',)])
            self.assertEqual(len(view['usage']['claude']['accounts']),1)
            view.update(compact=True,kakao_issues=[])
            console=Console(file=io.StringIO(),width=80,height=24)
            with patch.object(terminal,'read_json',return_value={}),patch('talkturn.computer.computer_label',return_value='컴퓨터: 테스트 PC'):
                for _ in range(2):
                    rendered=terminal.dashboard(console,view,render_only=True,max_rows=10)
                    text=''.join(segment.text for segment in console.render(rendered))
                    self.assertIn('현재 로그인 계정',text)
                    self.assertIn('Claude 1개',text)
                    self.assertIn('Codex 0개',text)
                    self.assertIn('← Claude Code',text)
                    self.assertNotIn('Claude Code (3)',text)
                    self.assertNotIn('Codex (',text)
            fetch.assert_called_once()
            self.assertEqual(auth.call_count,2)
        for unavailable in (None,RuntimeError('private auth detail'),TimeoutError('timeout')):
            def status(provider):
                if provider=='codex':
                    return False
                if isinstance(unavailable,Exception):
                    raise unavailable
                return unavailable
            with self.subTest(unavailable=type(unavailable).__name__),patch.object(providers,'login_state',side_effect=status):
                self.assertEqual(tasks.account_counts(),{'claude':None,'codex':0})
        # Original usage(provider) contract still performs one account lookup.
        with patch.object(providers,'ccswap_accounts',return_value=metadata) as fetch:
            self.assertIn('accounts',providers.usage('claude')['claude'])
            fetch.assert_called_once()


if __name__=='__main__':
    unittest.main()
