import copy
import io
import time
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from rich.console import Console

from talkturn import cli, commands, plain_delivery, runtime, service, sessions, terminal


class ConnectionSummaryTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for module in (runtime, service):
            self.stack.enter_context(patch.object(module, 'ROOT', self.root))
        self.stack.enter_context(patch('talkturn.computer.computer_label', return_value='컴퓨터: 작업 PC'))
        runtime.write_json('delivery.json', {'window_id': 123, 'process_id': 456})

    def test_groups_only_confirmed_watches_and_matches_exact_identity(self):
        watches = [
            {'provider': 'codex', 'session_id': 'shared', 'name': '오래된 Codex 제목'},
            {'provider': 'claude', 'session_id': 'shared', 'name': '오래된 Claude 제목'},
            {'provider': 'claude', 'session_id': 'app', 'name': '앱 작업'},
            {'provider': 'claude', 'target_kind': 'claude_peer', 'peer_ref': 'peer-one', 'name': '피어 이름'},
        ]
        ui = {'watched_sessions': watches + [copy.deepcopy(watches[0])],
              'selected_session': {'provider': 'codex', 'session_id': 'unwatched'}}
        runtime.write_json('ui.json', ui)
        runtime.write_json('session-catalog.json', {'rows': [
            {'provider': 'codex', 'session_id': 'shared',
             'name': 'Codex\r\n[임의] [오후 11:59]\x00\u202e끝', 'state': '진행 중', 'live': False},
            {'provider': 'claude', 'session_id': 'shared', 'name': 'Claude 최신'},
            {'provider': 'claude', 'session_id': 'app', 'name': '앱 작업',
             'state': '앱 연결 대기', 'reachable': False},
            {'provider': 'claude', 'target_kind': 'claude_peer', 'peer_ref': 'peer-one',
             'name': '피어 최신', 'state': '진행 중', 'reachable': False},
            {'provider': 'claude', 'target_kind': 'claude_peer', 'peer_ref': 'peer-other', 'name': '다른 피어'},
            {'provider': 'codex', 'session_id': 'unwatched', 'name': '선택만 한 작업'},
            {'provider': 'claude', 'session_id': 'other', 'name': '확인하지 않은 작업'},
        ]})
        with patch.object(sessions, 'discover') as discover, \
             patch.object(service.subprocess, 'run') as run, \
             patch.object(plain_delivery, 'call_luna') as model:
            message = service.connection_message()
        self.assertEqual(message.splitlines(), [
            'talkturn in', '컴퓨터: 작업 PC', '',
            'Claude Code · 결과 수신 선택 3개',
            '- [Claude 최신]', '- [앱 작업] · 앱 연결 대기', '- [피어 최신] · 수신 연결 없음', '',
            'Codex · 결과 수신 선택 1개',
            '- [Codex ［임의］ ［오후 11:59］ 끝] · 실행 상태 미확인',
        ])
        self.assertEqual(runtime.read_json('ui.json'), ui)
        discover.assert_not_called()
        run.assert_not_called()
        model.assert_not_called()

    def test_empty_groups_do_not_include_discovered_or_merely_selected_sessions(self):
        row = {'provider': 'codex', 'session_id': 'unwatched', 'name': '확정 전 작업'}
        runtime.write_json('ui.json', {'watched_sessions': [], 'selected_session': row})
        runtime.write_json('session-catalog.json', {'rows': [row]})
        self.assertEqual(service.connection_message().splitlines(), [
            'talkturn in', '컴퓨터: 작업 PC', '', 'Claude Code · 결과 수신 선택 0개', '선택한 세션 없음',
            '', 'Codex · 결과 수신 선택 0개', '선택한 세션 없음',
        ])

    def test_ping_preserves_marker_and_records_connection_only_after_delivery(self):
        previous = {'verified': False, 'at': 1}
        runtime.write_json('connection.json', previous)

        sent=[]
        def send(text, key, **kwargs):
            self.assertEqual(runtime.read_json('connection.json'), previous)
            sent.append(text)
            self.assertTrue(key.startswith('connection-'))
            self.assertEqual(kwargs, {'render_long': False})
            return True

        with patch.object(plain_delivery, 'deliver', side_effect=send) as deliver, \
             patch.object(service.time, 'time', return_value=42):
            service.ping()
        self.assertEqual(sent,['talkturn in\n컴퓨터: 작업 PC\n연결된 세션 없음\n답장: help · 세션 · out'])
        self.assertEqual(runtime.read_json('connection.json'),
                         {'verified': True, 'window_id': 123, 'process_id': 456, 'at': 42})

    def test_connection_batch_cards_preserve_origins_and_record_the_batch_start(self):
        claude={'provider':'claude','session_id':'claude-one','name':'같은 제목','cwd':r'C:\한글 작업\project'}
        codex={'provider':'codex','session_id':'codex-one','name':'같은 제목'}
        peer={'provider':'claude','session_id':None,'target_kind':'claude_peer',
              'peer_ref':'peer-one','name':'앱 작업'}
        runtime.write_json('ui.json',{'watched_sessions':[codex,claude,peer,codex]})
        previous={'verified':False,'at':1}
        runtime.write_json('connection.json',previous)
        sent=[]
        def send(text,key,**options):
            self.assertEqual(runtime.read_json('connection.json'),previous)
            sent.append((text,key,options))
            clock.return_value=99
            return True
        with patch.object(service.time,'time',return_value=42) as clock, \
             patch.object(plain_delivery,'deliver',side_effect=send):
            service.ping()
        self.assertEqual([text for text,_,_ in sent],[
            'talkturn in\n컴퓨터: 작업 PC\nClaude Code 2 · Codex 1\n답장: help · 세션 · out',
            '[같은 제목] · '+sessions.code('claude','claude-one'),'[앱 작업]',
            '[같은 제목] · '+sessions.code('codex','codex-one')])
        self.assertEqual(len({key for _,key,_ in sent}),4)
        self.assertEqual(sent[0][2],{'render_long':False})
        origins=[sent[index][2]['origin'] for index in (1,2,3)]
        self.assertEqual([origin['source'] for origin in origins],['claude','claude','codex'])
        self.assertEqual([origin['session_id'] for origin in origins],['claude-one',None,'codex-one'])
        self.assertTrue(all(origin['kind']=='session_anchor' and origin['connection_at']==42 for origin in origins))
        self.assertEqual(origins[0]['cwd'],claude['cwd'])
        self.assertEqual(origins[1]['peer_ref'],'peer-one')
        self.assertEqual(origins[1]['target_kind'],'claude_peer')
        self.assertEqual(origins[1]['name'],'앱 작업')
        self.assertEqual(runtime.read_json('connection.json'),
                         {'verified':True,'window_id':123,'process_id':456,'at':42})

    def test_unverified_delivery_never_records_connection(self):
        previous = {'verified': False, 'at': 1}
        for outcome in (False, RuntimeError('send failed')):
            with self.subTest(outcome=outcome):
                runtime.write_json('connection.json', previous)
                options = {'side_effect': outcome} if isinstance(outcome, Exception) else {'return_value': outcome}
                with patch.object(plain_delivery, 'deliver', **options), self.assertRaises(RuntimeError):
                    service.ping()
                self.assertEqual(runtime.read_json('connection.json'), previous)

    def test_selection_changes_send_update_once_without_resetting_connection(self):
        first={'provider':'claude','session_id':'first','code':'C-ONE','name':'Claude 작업'}
        second={'provider':'codex','session_id':'second','code':'X-TWO','name':'Codex 작업'}
        runtime.write_json('ui.json',{'watched_sessions':[first]})
        runtime.write_json('session-catalog.json',{'rows':[first,second],'fetched_at':time.time()})
        runtime.write_json('delivery.json',{'window_id':123,'process_id':456,'model':'gpt-6-luna'})
        receipt={'verified':True,'window_id':123,'process_id':456,'at':42}
        runtime.write_json('connection.json',receipt)
        view={'rows':[first,second],'watched':[first]}
        with patch.object(plain_delivery,'deliver',return_value=True) as send, \
             patch.object(plain_delivery,'call_luna') as model, \
             patch.object(cli,'console',Console(file=io.StringIO())) as console, \
             patch.object(terminal,'show_dashboard',return_value=view), \
             patch.object(terminal,'keyboard_pick',side_effect=[['C-ONE','X-TWO'],['X-TWO','C-ONE'],['C-ONE'],[],[],'']), \
             patch.object(console,'input',return_value='quit'), \
             patch.object(service,'delivery_issues',return_value=[]), \
             patch.object(cli,'setup') as setup:
            cli.interactive()
            self.assertEqual(send.call_count,6)
            messages=[call.args[0] for call in send.call_args_list]
            header=lambda counts:'talkturn update\n컴퓨터: 작업 PC\n'+counts+'\n답장: help · 세션 · out'
            self.assertEqual(messages,[header('Claude Code 1 · Codex 1'),'[Claude 작업]','[Codex 작업]',
                                       header('Claude Code 1'),'[Claude 작업]',header('연결된 세션 없음')])
            self.assertTrue(all(call.kwargs['origin']['connection_at']==42
                                for call in send.call_args_list if 'origin' in call.kwargs))
            self.assertEqual(runtime.read_json('connection.json'),receipt)
            setup.assert_not_called()
            commands.execute(['watch','C-ONE'])
            commands.execute(['watch','C-ONE'])
            self.assertEqual(send.call_count,8)
            remote=commands.execute(['watch','X-TWO'],remote=True)
            self.assertEqual(remote,'')  # The verified batch is the response; do not send another summary.
            self.assertEqual(send.call_count,10)
            self.assertEqual([call.args[0] for call in send.call_args_list[-2:]],
                             [header('Codex 1'),'[Codex 작업]'])
            self.assertEqual(send.call_args.kwargs['origin']['connection_at'],42)
            runtime.write_json('connection.json',{'verified':False})
            self.assertFalse(service.selection_update([]))
            before_ping=send.call_count
            service.ping()
            self.assertEqual(send.call_args_list[before_ping].args[0].splitlines()[0],'talkturn in')
            self.assertTrue(all(call.kwargs['render_long'] is False for call in send.call_args_list))
            model.assert_not_called()


if __name__ == '__main__':
    unittest.main()
