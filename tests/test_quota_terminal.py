import copy
from contextlib import ExitStack
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from rich.console import Console
from talkturn import quota_alerts, runtime, providers, plain_delivery, commands, terminal, service


class QuotaAndTerminalTest(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack(); self.addCleanup(self.stack.close)
        self.root=Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for module in (runtime,quota_alerts,service):
            self.stack.enter_context(patch.object(module,'ROOT',self.root))
        (self.root/'enabled').touch(); (self.root/'reply-enabled').touch()
        runtime.write_json('delivery.json',{'model':'gpt-6-luna','window_id':123,'process_id':456})

    def test_actionable_reasons_distinguish_selection_app_window_and_stopped_bridge(self):
        runtime.write_json('delivery.json',{'window_id':123,'process_id':456,'room_name':'Self'})
        with patch.object(service,'kakao_running',return_value=False),patch.object(service,'chat_windows') as rooms, \
             patch.object(service,'ping') as ping:
            self.assertIn('카카오톡 꺼짐',service.delivery_issues()[0])
            with self.assertRaisesRegex(RuntimeError,'카카오톡 꺼짐'): service.start()
            rooms.assert_not_called(); ping.assert_not_called()
        with patch.object(service,'kakao_running',return_value=True),patch.object(service,'chat_windows',return_value=[]) as rooms, \
             patch.object(service,'process_running',return_value=False) as running:
            self.assertIn('나와의 채팅 닫힘',service.delivery_issues()[0])
            rooms.return_value=[{'hwnd':124,'pid':456,'title':'Self'}]
            self.assertIn('카톡 창이 바뀜',service.delivery_issues()[0])
            rooms.return_value=[{'hwnd':123,'pid':456,'title':'Self'}]
            self.assertIn('start',service.delivery_issues()[0])
            running.return_value=True
            self.assertEqual(service.delivery_issues(),[])
        view={'watched':[],'kakao_issues':['카카오톡 꺼짐 · 카카오톡을 켜세요.']}
        self.assertIn('미선택',terminal.blocked_reasons(view)[0])
        view['checked_codes']={'C-ONE'}
        self.assertIn('미확정',terminal.blocked_reasons(view)[0])
        view['watched']=[{'code':'C-ONE'}]
        self.assertEqual(terminal.blocked_reasons(view),view['kakao_issues'])
        view['kakao_issues'].append('나와의 채팅 닫힘 · 별도 창을 열고 setup으로 다시 연결하세요.')
        self.assertEqual(terminal.blocked_reasons(view),['카카오톡 꺼짐 · 카카오톡을 켜세요.'])
        import json
        self.assertIn('작성 중인 글',plain_delivery.native_failure_message(json.dumps({'error':'Self-chat input contains a draft'})))
        self.assertNotIn('private secret',plain_delivery.native_failure_message(json.dumps({'error':'private secret'})))

    def fixture(self):
        return {'claude':{'accounts':[
            {'number':1,'label':'Work','active':True,'state':'ok','age_seconds':10,'limits':[
                {'name':'5시간','remaining_pct':0,'resets_at':'2026-11-09T00:00:00Z'},
                {'name':'7일','remaining_pct':50,'resets_at':'2026-11-10T00:00:00Z'}]},
            {'number':2,'label':'Other','active':False,'state':'ok','age_seconds':10,'limits':[
                {'name':'5시간','remaining_pct':80},{'name':'7일','remaining_pct':70}]}]},
            'codex':{'fetched_at':'now','limits':[]}}

    def test_exhaustion_asks_once_without_switching_and_rearms_after_recovery(self):
        data=self.fixture()
        accounts={'claude':[{'number':1,'label':'Work','active':True},{'number':2,'label':'Other','active':False}], 'codex':[]}
        with patch.object(providers,'usage',return_value=data),patch.object(providers,'accounts',return_value=accounts), \
             patch.object(providers,'switch_account') as switch,patch.object(plain_delivery,'deliver') as send, \
             patch.object(plain_delivery,'call_luna') as luna:
            self.assertEqual(quota_alerts.maybe_prompt(now=1000),1)
            self.assertIn('다른 계정으로 전환할까?',send.call_args.args[0])
            self.assertIn('톡턴 전환 claude 2',send.call_args.args[0])
            self.assertIn('어 = 2번으로 전환',send.call_args.args[0])
            # A reply to the alert bubble answers its question; the quote is Kakao's one-line preview, read by OCR.
            alert='CIaude Code · 잔여 한도 0%...'
            for body,command in (('어 해줘',['switch','claude','2']),('2',['switch','claude','2']),('유지',['keep']),
                                 ('3',True),('사용량',True)):
                self.assertEqual(quota_alerts.reply_command(alert,body),command)
            self.assertEqual(quota_alerts.reply_command('ChatGPT · Codex · 잔여 한도 0%','어'),['usage','codex'])  # nothing offered
            self.assertIsNone(quota_alerts.reply_command('[세션 제목] Claude Code 잔여 한도 0%','어 해줘'))
            self.assertEqual(quota_alerts.maybe_prompt(now=1001),0)
            # ccswap recomputes the reset time on every fetch; that alone is not a new exhaustion.
            data['claude']['accounts'][0]['limits'][0]['resets_at']='2026-11-09T00:00:00.5Z'
            self.assertEqual(quota_alerts.maybe_prompt(now=1301),0)
            data['claude']['accounts'][0]['limits'][0]['remaining_pct']=75
            self.assertEqual(quota_alerts.maybe_prompt(now=1602),0)
            self.assertEqual(quota_alerts.reply_command('Claude Code · 잔여 한도 0%','어'),['usage','claude'])  # recovered: offer gone
            data['claude']['accounts'][0]['limits'][0]['remaining_pct']=0
            self.assertEqual(quota_alerts.maybe_prompt(now=1903),1)
            self.assertEqual(send.call_count,2)
            switch.assert_not_called(); luna.assert_not_called()

    def test_missing_stale_scoped_and_disabled_alerts_never_prompt(self):
        data=self.fixture()
        data['claude']['accounts'][0]['age_seconds']=700
        self.assertEqual(quota_alerts.exhausted(data),{})
        row=data['claude']['accounts'][0]
        row['age_seconds']=1
        row['limits']=[{'name':'5시간','remaining_pct':None},{'name':'Fable','remaining_pct':0}]
        self.assertEqual(quota_alerts.exhausted(data),{})
        runtime.write_json('delivery.json',{'quota_prompt_enabled':False})
        with patch.object(providers,'usage') as fetch,patch.object(plain_delivery,'deliver') as send:
            self.assertEqual(quota_alerts.maybe_prompt(now=1000),0)
            fetch.assert_not_called(); send.assert_not_called()

    def test_connection_markers_and_dashboard_exclude_token_totals(self):
        self.assertEqual(commands.execute(['ping'],remote=True).splitlines()[0],'talkturn in')
        with patch.object(plain_delivery,'deliver') as deliver:
            service.ping()
            self.assertEqual(deliver.call_args_list[0].args[0].splitlines()[0],'talkturn in')
            service.disconnect()
            self.assertEqual(deliver.call_args.args[0],'talkturn out')
        data=self.fixture()
        data['claude']['accounts']=data['claude']['accounts'][:1]
        data['codex'].update({'limits':[{'name':'codex 7일','remaining_pct':72,'resets_at':1794639426}],
                             'latest_day_tokens':285061051,'lifetime_tokens':24690899648})
        view={'provider':'all','rows':[{'provider':'codex','project':'talkturn','name':'TalkTurn 작업','code':'X-12345678','state':'진행 중','model':'gpt-6-astra','session_id':'test'}],
              'waiting':[],'models':{'codex':{'model':'gpt-6-astra'},'claude':{'model':'opus'}},'usage':data}
        output=io.StringIO(); console=Console(file=output,width=88,color_system=None)
        terminal.dashboard(console,view)
        text=output.getvalue()
        self.assertIn('72%',text)
        self.assertIn('번호',text)
        self.assertIn('세션 이름',text)
        self.assertNotIn('gpt-6-astra',text)
        self.assertNotIn('추론 강도',text)
        self.assertNotIn('현재 상태',text)
        self.assertNotIn('285,061,051',text)
        self.assertNotIn('24,690,899,648',text)
        self.assertNotIn('최근 집계',commands.usage_text(data))
        self.assertNotIn('계정 누적',commands.usage_text(data))


if __name__=='__main__':
    unittest.main()
