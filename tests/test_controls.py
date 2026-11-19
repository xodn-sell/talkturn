import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from contextlib import ExitStack

from talkturn import commands, providers, runtime, service, plain_delivery, tasks, reply_bridge as bridge
from talkturn.notify import notice


class ControlsTest(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack()
        self.addCleanup(self.stack.close)
        self.root=Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for module in (runtime, commands, providers, service, plain_delivery, bridge):
            self.stack.enter_context(patch.object(module,'ROOT',self.root))
        self.stack.enter_context(patch.dict(os.environ,{'CODEX_HOME':str(self.root/'codex'),'CLAUDE_CONFIG_DIR':str(self.root/'claude')}))

    def test_phone_receipt_only_reports_current_connection_and_never_echoes_logs(self):
        command=commands.parse('톡턴 접수',remote=True)
        runtime.write_json('connection.json',{'at':10,'verified':True})
        runtime.write_json('delivery.json',{})  # Paired installations can predate reply_enabled_at.
        epoch=bridge._connection_identity(runtime.read_json('delivery.json'))
        self.assertIn('기록이 없습니다',commands.execute(command,remote=True))
        for stage,label in [('accepted','접수됐습니다'),('not_sent','전달되지 않았습니다'),
                            ('forward_failed','다시 보내기 전에'),('unmatched_reply','받을 작업을 찾지 못했습니다')]:
            bridge._reply_log(stage,'PRIVATE_BODY','PRIVATE_ID',connection=epoch)
            reply=commands.execute(command,remote=True)
            self.assertIn(label,reply)
            self.assertNotIn('PRIVATE',reply)
        bridge._reply_log('plain_note','PRIVATE_NOTE')
        self.assertEqual(commands.execute(command,remote=True),reply)
        runtime.write_json('connection.json',{'at':20,'verified':True})
        self.assertIn('기록이 없습니다',commands.execute(command,remote=True))

    def test_codex_queue_warning_reaches_ack_and_receipt_without_resubmitting(self):
        target={'id':'1234abcde','text':'결과','origin':{'source':'codex','session_id':'original','name':'테스트 작업'}}
        runtime.write_json('connection.json',{'at':1,'verified':True})
        for message in ('인용 답장','톡턴 전달 T1234ABCD 번호 답장'):
            with self.subTest(message=message):
                self.baseline()
                runtime.write_json('result-origins.json',[target])
                def read():
                    return [{'text':message,'date':'','time':''}]
                with patch.object(bridge,'native_read',side_effect=read),patch.object(bridge,'screenshot',return_value='fixture.png'), \
                     patch.object(bridge,'message_kind',return_value='plain'),patch.object(bridge,'control_reply',return_value=None), \
                     patch.object(bridge,'choose_origin',return_value=target),patch.object(bridge,'executable',return_value='codex'), \
                     patch.object(bridge.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'queued','resume warning')) as queue, \
                     patch.object(plain_delivery,'deliver',return_value=True) as send:
                    bridge.poll_once()
                    bridge.poll_once()
                    queue.assert_called_once()
                    self.assertEqual(queue.call_args.args[0][2:4],['--thread','original'])
                    self.assertEqual(send.call_count,1)
                    self.assertEqual(send.call_args.args[0],'[테스트 작업]\n'+commands.QUEUED_NOTICE)
                    self.assertIn(commands.QUEUED_NOTICE,commands.receipt_text())
                    self.assertEqual(runtime.read_json('last-reply-state.json')['stage'],'queued')

    def test_repairing_during_command_read_or_execution_drops_stale_effects(self):
        for during in ('read','execute'):
            with self.subTest(during=during):
                self.baseline()
                runtime.write_json('connection.json',{'at':1})
                def read():
                    if during=='read':
                        runtime.write_json('connection.json',{'at':2})
                    return [{'text':'톡턴 접수','date':'','time':''}]
                def execute(*args,**kwargs):
                    runtime.write_json('connection.json',{'at':2})
                    return 'old connection response'
                with patch.object(bridge,'native_read',side_effect=read), \
                     patch.object(bridge,'screenshot',return_value='sample.png'), \
                     patch.object(bridge,'message_kind',return_value='plain'), \
                     patch.object(commands,'execute',side_effect=execute) as handle, \
                     patch.object(plain_delivery,'deliver') as send:
                    bridge.poll_once()
                    self.assertEqual(handle.call_count,int(during=='execute'))
                    send.assert_not_called()

    def test_phone_tell_uses_the_selected_provider_receipt_once(self):
        from talkturn import sessions
        row={'provider':'codex','session_id':'original','name':'작업 A'}
        for case in ('accepted','queued','changed_before_send','changed_after_send','uncertain'):
            with self.subTest(case=case):
                self.baseline()
                runtime.write_json('ui.json',{'selected_session':row})
                runtime.write_json('connection.json',{'at':1,'verified':True})
                runtime.write_json('last-selected-send.json',{'stage':'stale-PRIVATE'})
                messages=[{'text':'톡턴 보내 두  칸 유지','date':'','time':''}]
                def validate(target):
                    if case=='changed_before_send':
                        runtime.write_json('connection.json',{'at':2,'verified':True})
                    return target
                def queue(command,**kwargs):
                    if case=='changed_after_send':
                        runtime.write_json('connection.json',{'at':2,'verified':True})
                    if case=='uncertain':
                        raise RuntimeError('PRIVATE provider detail')
                    return subprocess.CompletedProcess(command,0,'PRIVATE provider output','warning' if case=='queued' else '')
                with patch.object(sessions,'validate_target',side_effect=validate), \
                     patch.object(bridge,'native_read',side_effect=lambda:copy.deepcopy(messages)), \
                     patch.object(bridge,'screenshot',return_value='fixture.png'),patch.object(bridge,'message_kind',return_value='plain'), \
                     patch.object(bridge,'executable',return_value='codex'),patch.object(bridge.subprocess,'run',side_effect=queue) as provider, \
                     patch.object(plain_delivery,'deliver',return_value=True) as send:
                    bridge.poll_once()
                    bridge.poll_once()
                    self.assertEqual(provider.call_count,int(case!='changed_before_send'))
                    if provider.called:
                        self.assertEqual(provider.call_args.args[0],['codex','queue','--thread','original','--message','두  칸 유지','--resume'])
                    stale=case.startswith('changed_')
                    self.assertEqual(send.call_count,int(not stale))
                    if not stale:
                        self.assertNotIn('PRIVATE',send.call_args.args[0])
                        self.assertTrue(send.call_args.kwargs['literal_text'])
                    if case in ('accepted','queued'):
                        self.assertEqual(runtime.read_json('last-selected-send.json')['stage'],case)
                        self.assertEqual(runtime.read_json('last-reply-state.json')['stage'],case)
                    messages.append({'text':'톡턴 접수','date':'','time':''})
                    bridge.poll_once()
                    status=send.call_args.args[0]
                    expected={'accepted':'접수됐습니다','queued':commands.QUEUED_NOTICE,'uncertain':'다시 보내기 전에'}
                    self.assertIn('기록이 없습니다' if stale else expected[case],status)
                    self.assertNotIn('PRIVATE',status)

    def test_phone_display_help_is_read_only_and_can_be_replied_to(self):
        menu=commands.execute(commands.parse('톡턴 표시',remote=True),remote=True)
        self.assertTrue(bridge.control_quote(menu))
        self.assertIn('표시 text',menu)
        self.assertIn('현재 image',menu)
        self.assertFalse((self.root/'delivery.json').exists())
        runtime.write_json('delivery.json',{'long_text':'auto'})
        self.assertIn('현재 auto',commands.execute(['display'],remote=True))

    def test_original_text_requires_verified_connection_watched_session_and_unique_result(self):
        settings={'mode':'native','window_id':3,'process_id':4,'reply_enabled_at':12}
        runtime.write_json('delivery.json',settings)
        runtime.write_json('connection.json',{'verified':True,'at':10,'window_id':3,'process_id':4})
        (self.root/'reply-enabled').touch()
        watches=[{'provider':'codex','session_id':'one','name':'결제 화면'}]
        runtime.write_json('ui.json',{'watched_sessions':watches})
        origin={'source':'codex','session_id':'one','kind':'result'}
        current={'id':'abcdef120'+'0'*55,'origin':origin,'text':'[결제 화면]\n첫 답','sent_at':14}
        other={**current,'id':'abcdef121'+'0'*55,'origin':{**origin,'source':'claude'},'text':'PRIVATE'}
        hidden=[other,{**other,'id':'b'*64,'origin':origin,'sent_at':11},
                {**other,'id':'c'*64,'origin':{**origin,'kind':'session_anchor'}},
                {**other,'id':'d'*64,'origin':{**origin,'request_id':'question'}}]
        registry=[current,*hidden]
        runtime.write_json('result-origins.json',registry)
        def original(*args):
            return commands.execute(commands.parse('톡턴 원문'+' '.join(['',*args]),remote=True),remote=True)
        self.assertEqual(original(),current['text'])
        for prefix in ('TABCDEF12','TABCDEF121','TBBBBBBBB','TCCCCCCCC','TDDDDDDDD','TFFFFFFFF'):
            self.assertIn('이 번호의 원문이 없습니다',original(prefix))
        self.assertEqual(original('tabcdef120'),current['text'])
        second={**current,'id':'agy-example','origin':{'source':'agy','session_id':'two','kind':'result'},
                'text':'[배너]\n다른 답'}
        runtime.write_json('result-origins.json',registry+[second])
        runtime.write_json('ui.json',{'watched_sessions':watches+[{'provider':'agy','session_id':'two','name':'배너'}]})
        listing=original()
        self.assertIn('TABCDEF120 · 결제 화면',listing)
        self.assertNotIn('PRIVATE',listing)
        self.assertNotIn('다른 답',listing)
        other_code=next(line.split()[0] for line in listing.splitlines() if '배너' in line)
        self.assertEqual(original(other_code),second['text'])
        # On a selection change, an already computed reply must never escape.
        epoch=bridge._connection_identity(settings)
        runtime.write_json('ui.json',{'watched_sessions':watches})
        with patch.object(plain_delivery,'deliver') as send:
            self.assertFalse(plain_delivery.deliver_original(second['text'],[other_code],'command-test',epoch))
            send.assert_not_called()
        runtime.write_json('delivery.json',{**settings,'window_id':5})
        self.assertIn('연결이 없습니다',original())
        runtime.write_json('delivery.json',settings)
        (self.root/'reply-enabled').unlink()
        self.assertIn('연결이 없습니다',original())

    def test_control_original_delivers_stored_text_without_reattaching_files(self):
        self.baseline()
        (self.root/'reply-enabled').touch()
        runtime.write_json('delivery.json',{'mode':'native','window_id':3,'process_id':4})
        runtime.write_json('connection.json',{'verified':True,'at':10,'window_id':3,'process_id':4})
        runtime.write_json('ui.json',{'watched_sessions':[{'provider':'codex','session_id':'one','name':'샘플'}]})
        body='[샘플]\n컴퓨터: 테스트\n\n'+('긴 본문입니다. '*180)+'![참고](example.png)'
        result={'id':'a'*64,'text':body,'origin':{'source':'codex','session_id':'one','kind':'result'},'sent_at':14}
        runtime.write_json('result-origins.json',[result])
        message={'sender':'self','time':'오후 1:00','text':'원문','is_photo':False,'is_file':False}
        isolated={'path':'fixture','body':'원문','quote_kind':'text','quote_text':'talkturn in'}
        with patch.object(bridge,'native_read',return_value=[message]),patch.object(bridge,'screenshot',return_value='fixture'), \
             patch.object(bridge,'isolate_reply',return_value=isolated),patch.object(bridge,'call_luna') as model, \
             patch.object(plain_delivery,'extract_images') as extract,patch('talkturn.render.text_images') as render, \
             patch.object(plain_delivery.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'{"verified":true}','')) as send:
            bridge.poll_once()
            self.assertEqual(json.loads(send.call_args.kwargs['input']),{'text':body,'images':[]})
            extract.assert_not_called()
            render.assert_not_called()
            model.assert_not_called()
            self.assertEqual(runtime.read_json('result-origins.json'),[result])
            # Export-shaped stored text keeps the same image-only safety fallback as display text.
            render.return_value=['safe-page.png']
            plain_delivery.deliver('2026년 11월 19일 목요일','export-shaped',literal_text=True,render_long=False)
            render.assert_called_once()
            packet=json.loads(send.call_args.kwargs['input'])
            self.assertEqual(packet['images'],['safe-page.png'])
            self.assertIn('원문을 사진으로',packet['text'])
            self.assertNotIn('목요일',packet['text'])

    def test_quota_missing_is_not_zero_and_stale_is_labelled(self):
        raw={'claude':{'accounts':[{'number':1,'active':True,'email':'private@example.com','usageStatus':'ok',
            'usage':{'fiveHour':{'pct':0},'sevenDay':{'pct':88}}},
            {'number':2,'usageStatus':'unavailable','usage':None},
            {'number':3,'usageStatus':'unavailable','lastGoodUsage':{'fiveHour':{'pct':50}},'lastGoodAgeSeconds':1200}]}}
        with patch.object(providers,'ccswap_accounts',return_value=raw):
            result=providers.claude_usage()
        self.assertEqual(result['accounts'][0]['limits'][0]['remaining_pct'],100)
        self.assertEqual(result['accounts'][0]['limits'][1]['remaining_pct'],12)
        self.assertEqual(result['accounts'][1]['state'],'unavailable')
        self.assertEqual(result['accounts'][2]['state'],'cached')
        self.assertNotIn('private',json.dumps(result))
        self.assertIsNone(providers.remaining(None))
        self.assertIsNone(providers.remaining(float('nan')))
        self.assertIn('마지막 성공값',commands.usage_text({'claude':result}))

    def test_codex_windows_and_usage_are_not_remaining_tokens(self):
        fake=self.stack.enter_context(patch.object(providers,'CodexReader'))
        fake.return_value.__enter__.return_value.call.return_value={
            'rateLimits':{'primary':{'usedPercent':27,'windowDurationMins':10080,'resetsAt':1794639426}}}
        result=providers.codex_usage()
        self.assertEqual(result['limits'][0]['remaining_pct'],73)
        self.assertIn('7일',result['limits'][0]['name'])
        fake.return_value.__enter__.return_value.call.assert_called_once_with('account/rateLimits/read')
        self.assertNotIn('latest_day_tokens',result)
        self.assertNotIn('lifetime_tokens',result)
        self.assertIsNone(result['exact_remaining_tokens'])

    def test_remote_commands_are_exact_and_do_not_accept_shell_or_secrets(self):
        self.assertEqual(commands.parse('톡턴 사용량 클코',remote=True),['usage','claude'])
        self.assertEqual(commands.parse('/talkturn model relay gpt-6-luna',remote=True),['model','relay','gpt-6-luna'])
        self.assertIsNone(commands.parse('이 결과로 진행해',remote=True))
        self.assertIsNone(commands.parse('톡턴 상태\n전환 claude 2',remote=True))
        # A phone keyboard breaks the line after the prefix; the command itself stays one line.
        self.assertEqual(commands.parse('Talkturn\nHelp',remote=True),['help'])
        with patch.object(providers,'switch_account') as switch:
            with self.assertRaises(ValueError):
                commands.execute(commands.parse('톡턴 switch claude 2 ; whoami',remote=True),remote=True)
            switch.assert_not_called()
        with self.assertRaises(ValueError):
            commands.execute(['stop'],remote=True)

    def test_switch_uses_only_valid_registered_number_and_verifies_active(self):
        before={'claude':{'activeAccountNumber':1,'accounts':[{'number':2,'active':False}]}}
        after={'claude':{'activeAccountNumber':2,'accounts':[{'number':2,'active':True}]}}
        with patch.object(providers,'ccswap_accounts',side_effect=[before,after]), \
             patch.object(providers,'executable',return_value='ccswap.exe'), \
             patch.object(providers,'run_json',return_value={'switched':True}) as run:
            result=providers.switch_account('claude','2')
            self.assertEqual(result['account'],2)
            self.assertEqual(run.call_args.args[0],['ccswap.exe','switch','2','--json'])
        with patch.object(providers,'run_json') as run:
            with self.assertRaises(ValueError):
                providers.switch_account('claude','2;whoami')
            run.assert_not_called()

    def test_terminal_only_entry_and_account_registration_do_not_open_a_website(self):
        from talkturn import cli,web
        with patch('webbrowser.open') as browser,patch('socket.socket.bind') as bind,patch('builtins.print'):
            self.assertIn('터미널',cli.dispatch(['ui','--shortcut']))
            web.main()
            browser.assert_not_called(); bind.assert_not_called()
        with patch.object(cli.sys.stdin,'isatty',return_value=False),patch.object(providers,'executable',return_value='ccswap.exe'), \
             patch.object(providers,'ccswap_accounts') as listing, \
             patch.object(providers.subprocess,'run',return_value=subprocess.CompletedProcess([],0,b'private',b'')) as run:
            for provider in ('claude','codex'):
                listing.return_value={provider:{'activeAccountNumber':2,'accounts':[{'number':2,'alias':'saved account'}]}}
                self.assertIn('등록 확인',cli.dispatch(['account-add',provider]))
                self.assertEqual(run.call_args.args[0],['ccswap.exe']+(['codex'] if provider=='codex' else [])+['add'])
                self.assertEqual(run.call_args.kwargs['stdin'],subprocess.DEVNULL)
            with self.assertRaises(ValueError):
                cli.dispatch(['account-add','claude;whoami'])
            self.assertEqual(run.call_count,2)

    def test_settings_and_hooks_preserve_unrelated_config_and_restore_callback(self):
        cp=providers.config_path('codex'); cp.parent.mkdir()
        cp.write_text('model = "old"\nnotify = [\n "original.exe",\n "event"\n]\n[profiles.work]\nmodel="custom"\n',encoding='utf-8')
        cl=providers.config_path('claude'); cl.parent.mkdir()
        cl.write_text(json.dumps({'hooks':{'Stop':[{'hooks':[{'type':'command','command':'keep-me'}]}]},'permissions':{'allow':['Read']}}),encoding='utf-8')
        service.install_hooks()
        service.install_hooks()
        self.assertEqual(runtime.read_json('previous-notify.json'),['original.exe','event'])
        data=providers.provider_config('claude')
        self.assertEqual(len(data['hooks']['Stop']),2)
        self.assertEqual(data['permissions'],{'allow':['Read']})
        self.assertEqual(providers.provider_config('codex')['profiles']['work']['model'],'custom')
        with patch.object(providers,'models',return_value=['gpt-6-astra']):
            providers.set_model('codex','gpt-6-astra')
        self.assertEqual(providers.provider_config('codex')['model'],'gpt-6-astra')
        self.assertEqual(providers.provider_config('codex')['profiles']['work']['model'],'custom')
        with patch.object(service,'startup'):
            service.uninstall()
        self.assertEqual(providers.provider_config('codex')['notify'],['original.exe','event'])
        self.assertEqual(providers.provider_config('claude')['hooks']['Stop'][0]['hooks'][0]['command'],'keep-me')

    def baseline(self):
        runtime.write_json('baseline-hashes.json',[])
        (self.root/'baseline-version').write_text('3')

    def test_claude_exec_hooks_upgrade_once_and_uninstall_preserves_user_hooks(self):
        from unittest.mock import Mock
        import win32com.client
        python=Path(r'C:\도구 폴더\Python 3.11\python.exe')
        state=self.root/'공백 있는 상태'
        events=('Stop','PreToolUse','PermissionRequest','Notification','SessionStart','UserPromptSubmit','SessionEnd')
        question_events={'PreToolUse','PermissionRequest','Notification'}
        user={'type':'command','command':'user-security.exe','args':[r'C:\사용자 정책\rules.json']}
        quoted={'type':'command','command':'echo "python.exe -m talkturn.notify example"'}
        hooks={}
        for event in events:
            module='tokturn.questions' if event in question_events else 'tokturn.notify'
            legacy=subprocess.list2cmdline([r'C:\이전 도구\python.exe','-m',module,'--home',str(state),'--source','claude'])
            hooks[event]=[{'matcher':'preserved','hooks':[copy.deepcopy(user),copy.deepcopy(quoted),
                {'type':'command','command':legacy},
                {'type':'command','command':r'C:\이전 도구\python.exe',
                 'args':['-m',module,'--home',str(state),'--source','claude']}]}]
        hooks['Stop'][0]['hooks'].append({'type':'command','command':subprocess.list2cmdline([
            'python.exe',r'C:\사용자 경로\kakao-completion\notify.py','--source','claude'])})
        security={'permissions':{'allow':['Read'],'deny':['Bash(rm *)']},'disableAllHooks':False}
        cl=providers.config_path('claude'); cl.parent.mkdir()
        cl.write_text(json.dumps({**security,'hooks':hooks}),encoding='utf-8')
        cp=providers.config_path('codex'); cp.parent.mkdir()
        legacy_notify=[r'C:\이전 도구\python.exe','-m','tokturn.notify','--home',str(state),'codex-notify']
        cp.write_text('notify = '+json.dumps(legacy_notify)+'\n[profiles.work]\nmodel="keep"\n',encoding='utf-8')
        codex_user={'matcher':'User','hooks':[{'type':'command','command':'codex-user-hook'}]}
        codex_legacy={'hooks':[{'type':'command','command':subprocess.list2cmdline([
            str(python),'-m','tokturn.questions','--source','codex'])}]}
        cp.with_name('hooks.json').write_text(json.dumps({'hooks':{'PreToolUse':[codex_user,codex_legacy]}}),encoding='utf-8')
        with patch.object(runtime,'PYTHON',python),patch.object(runtime,'ROOT',state),patch.object(service,'ROOT',state):
            runtime.write_json('previous-notify.json',['original.exe','event'])
            service.install_hooks()
            first=providers.provider_config('claude')
            first_codex=json.loads(cp.with_name('hooks.json').read_text(encoding='utf-8'))
            service.install_hooks()
            data=providers.provider_config('claude')
            self.assertEqual(data,first)
            self.assertEqual(runtime.read_json('previous-notify.json'),['original.exe','event'])
            self.assertEqual(json.loads(cp.with_name('hooks.json').read_text(encoding='utf-8')),first_codex)
            for event in events:
                with self.subTest(event=event):
                    entries=[entry for group in data['hooks'][event] for entry in group['hooks']]
                    ours=[entry for entry in entries if service.owned_hook(entry)]
                    self.assertEqual(len(ours),1)
                    self.assertEqual(ours[0]['command'],str(python))
                    self.assertEqual(ours[0]['args'][:2],['-m','talkturn.questions' if event in question_events else 'talkturn.notify'])
                    self.assertEqual(ours[0]['args'][ours[0]['args'].index('--home')+1],str(state))
                    self.assertIn(user,entries); self.assertIn(quoted,entries)
            metadata=runtime.read_json('installed-hooks.json')
            self.assertTrue(service.owned_hook(metadata['claude']))
            self.assertEqual(metadata['claude']['args'],first['hooks']['Stop'][-1]['hooks'][0]['args'])
            for groups in first_codex['hooks'].values():
                for group in groups:
                    for hook in group['hooks']:
                        self.assertNotIn('args',hook)  # Codex shell-form hooks stay unchanged.
                        if service.owned_hook(hook): self.assertNotIn('tokturn.',hook['command'])
            appdata=self.root/'appdata'
            startup=appdata/'Microsoft/Windows/Start Menu/Programs/Startup'; startup.mkdir(parents=True)
            old_link=startup/'TokTurn replies.lnk'; old_link.write_bytes(b'former application shortcut')
            new_link=startup/'TalkTurn replies.lnk'
            other_link=startup/'Other app.lnk'; other_link.write_bytes(b'keep')
            old=Mock(TargetPath=str(python),Arguments=subprocess.list2cmdline([
                '-m','tokturn.service','autostart','--home',str(state)]))
            new=Mock()
            def save():
                self.assertTrue(old_link.exists())
                new_link.write_bytes(b'new application shortcut')
            new.Save.side_effect=save
            shell=Mock(); shell.CreateShortcut.side_effect=lambda path:old if Path(path)==old_link else new
            with patch.dict(os.environ,{'APPDATA':str(appdata)}), \
                 patch.object(win32com.client,'Dispatch',return_value=shell):
                service.startup(True)
                self.assertTrue(new_link.exists()); self.assertFalse(old_link.exists())
                self.assertTrue(other_link.exists())
                self.assertIn('talkturn.service',new.Arguments)
                old_link.write_bytes(b'unrelated shortcut with the former name')
                old.TargetPath='unrelated.exe'; old.Arguments='--keep'
                service.startup(True)
                self.assertTrue(old_link.exists())
            with patch.object(service,'startup'):
                service.uninstall()
            restored=providers.provider_config('claude')
            for key,value in security.items(): self.assertEqual(restored[key],value)
            for event in events:
                self.assertEqual(restored['hooks'][event],[{'matcher':'preserved','hooks':[user,quoted]}])
            self.assertEqual(providers.provider_config('codex')['notify'],['original.exe','event'])
            self.assertEqual(providers.provider_config('codex')['profiles']['work']['model'],'keep')
            self.assertEqual(json.loads(cp.with_name('hooks.json').read_text(encoding='utf-8'))['hooks']['PreToolUse'],[codex_user])

    def test_remote_control_without_prior_results_never_calls_luna_and_consumes_before_send(self):
        self.baseline()
        message={'sender':'self','time':'오후 1:00','text':'톡턴 사용량','is_photo':False,'is_file':False}
        with patch.object(bridge,'native_read',side_effect=lambda:[copy.deepcopy(message)]), \
             patch.object(bridge,'screenshot',return_value='synthetic.png'), \
             patch.object(bridge,'message_kind',return_value='plain'), \
             patch.object(commands,'execute',return_value='사용량 결과') as execute, \
             patch.object(plain_delivery,'deliver',side_effect=TimeoutError) as send, \
             patch.object(bridge,'call_luna') as luna:
            with self.assertRaises(TimeoutError):
                bridge.poll_once()
            self.assertEqual(bridge.poll_once()['new'],0)
            self.assertEqual(execute.call_count,1)
            self.assertEqual(send.call_count,1)
            luna.assert_not_called()

    def test_reply_to_talkturn_in_is_a_local_control_and_other_replies_stay_data(self):
        for quote,control in (('talkturn in 컴퓨터: 작업 PC',True),('talkturn컴퓨터:작업PC',True),('taIkturn update',True),
                              ('TalkTurn 명령',True),('[talkturn in 답장 기능]',False),('Claude Code',False),
                              ('TalkTurn 결과 T1234ABCD · 이미지 2장',False)):
            self.assertEqual(bridge.control_quote(quote),control,quote)
        # The phone's help is short and still a control bubble itself; `help` is the only help command there.
        phone=commands.execute(['help'],remote=True)
        self.assertLess(len(phone.splitlines()),12)
        self.assertTrue(bridge.control_quote(' '.join(phone.splitlines()[:3])))
        self.assertRaises(ValueError,commands.execute,['help','all'],remote=True)
        self.baseline()
        runtime.write_json('result-origins.json',[{'id':'anchor','text':'[작업]','origin':{'source':'codex','session_id':'thread'}}])
        for body,quote,executed in (('Help','talkturn in',['help']),('톡턴 out','[작업]',None),('톡턴 원문','[작업]',None)):
            message={'sender':'self','time':'오후 1:00','text':body,'is_photo':False,'is_file':False}
            isolated={'path':'crop.png','body':body,'quote_kind':'text','quote_text':quote}
            with patch.object(bridge,'native_read',side_effect=lambda:[copy.deepcopy(message)]), \
                 patch.object(bridge,'screenshot',return_value='synthetic.png'), \
                 patch.object(bridge,'message_kind',return_value='reply'), \
                 patch.object(bridge,'isolate_reply',return_value=isolated), \
                 patch.object(commands,'execute',return_value='응답') as execute, \
                 patch.object(plain_delivery,'deliver') as send, \
                 patch.object(bridge,'choose_origin',return_value=None) as route, \
                 patch.object(bridge,'call_luna') as luna:
                bridge.poll_once()
                if executed:
                    execute.assert_called_once_with(executed,remote=True)
                    self.assertEqual(send.call_args.args[0],'응답')
                    route.assert_not_called()
                else:
                    execute.assert_not_called()
                    self.assertEqual(route.call_count,1)
                    # An unroutable quoted reply leaves a local log line and nothing in the chat.
                    send.assert_not_called()
                    self.assertEqual(runtime.read_json('reply-log.json')[-1]['stage'],'unmatched_reply')
                luna.assert_not_called()

    def test_phone_session_list_is_short_and_screen_reply_attaches_each_monitor(self):
        from talkturn import sessions
        rows=[{'provider':'claude','session_id':'a','code':'C-A','name':'열린 작업','state':'대기','live':True},
              {'provider':'codex','session_id':'b','code':'X-B','name':'저장된 작업','state':'저장됨','live':False},
              {'provider':'codex','session_id':'c','code':'X-C','name':'체크한 작업','state':'저장됨','live':False}]
        runtime.write_json('ui.json',{'watched_sessions':[rows[2]]})
        with patch.object(sessions,'discover',return_value={'rows':rows,'errors':{}}):
            text=commands.execute(['sessions'],remote=True)
            self.assertIn('저장된 작업',commands.execute(['sessions']))
        self.assertIn('[ ] C-A · 열린 작업',text); self.assertIn('[✓] X-C · 체크한 작업',text)
        self.assertNotIn('저장된 작업',text)
        shot=self.root/'screen-1.png'; shot.write_bytes(b'png')
        with patch.object(commands,'screen_images',return_value=[shot]):
            body,images=plain_delivery.extract_images(commands.execute(commands.parse('화면'),remote=True),'')
        self.assertEqual(images,[str(shot.resolve())])
        self.assertNotIn('screen-1',body)

    def test_consumed_identical_command_does_not_block_a_new_occurrence(self):
        self.baseline()
        message={'sender':'self','time':'오후 1:00','text':'톡턴 상태','is_photo':False,'is_file':False}
        reads=[[copy.deepcopy(message)],[copy.deepcopy(message),copy.deepcopy(message)]]
        with patch.object(bridge,'native_read',side_effect=reads), \
             patch.object(bridge,'screenshot',return_value='synthetic.png'), \
             patch.object(bridge,'message_kind',return_value='plain'), \
             patch.object(commands,'execute',return_value='상태'),patch.object(plain_delivery,'deliver') as send:
            bridge.poll_once(); bridge.poll_once()
            self.assertEqual(send.call_count,2)

    def test_position_selects_only_the_newest_message_and_only_while_the_chat_is_unchanged(self):
        self.baseline()
        result={'id':'one','text':'결과 원문','origin':{'source':'codex','session_id':'existing-thread'}}
        runtime.write_json('result-origins.json',[result])
        crop=self.root/'isolated-reply.png'; crop.write_bytes(b'isolated bubble')
        older={'sender':'self','time':'오후 1:00','text':'먼저 보낸 답장','is_photo':False,'is_file':False}
        reply={'sender':'self','time':'오후 1:01','text':'ㅇㅇ','is_photo':False,'is_file':False}
        arrived={'sender':'self','time':'오후 1:01','text':'그 사이 도착한 결과','is_photo':False,'is_file':False}
        positional=lambda message,image,cache=None,last=False,**kwargs:(
            {'path':str(crop),'body':message['text'],'quote_kind':'text'} if last else None)
        for name,reads,seen,forwarded in (
                ('unchanged chat',[[reply],[reply]],[],['ㅇㅇ']),
                ('something arrived around the capture',[[reply],[reply,arrived]],[],[]),
                ('second read failed',[[reply],RuntimeError('busy')],[],[]),
                # Only the older reply is unread: it may not borrow the lowest bubble, and no second read happens.
                ('not the newest message',[[older,reply]],[bridge.fingerprint({**reply,'_occurrence':1})],[])):
            with self.subTest(name):
                runtime.write_json('baseline-hashes.json',seen)
                with patch.object(bridge,'native_read',side_effect=[copy.deepcopy(item) for item in reads]) as read, \
                     patch.object(bridge,'screenshot',return_value='synthetic.png'), \
                     patch.object(bridge,'isolate_reply',side_effect=positional), \
                     patch.object(bridge,'message_kind',return_value=None), \
                     patch.object(bridge,'call_luna',side_effect=lambda prompt,schema,images:{'body':'ㅇㅇ','quote':'결과 원문','quote_kind':'text'}) as luna, \
                     patch.object(bridge,'forward',return_value={'accepted':True}) as forward, \
                     patch.object(plain_delivery,'deliver') as send:
                    bridge.poll_once()
                    self.assertEqual([call.args[1] for call in forward.call_args_list],forwarded)
                    self.assertEqual(read.call_count,len(reads))
                    # The crop reaches the model only after the chat is proven unchanged.
                    self.assertEqual(luna.call_count,len(forwarded))
                    # Success is confirmed in the chat; a failure is only logged.
                    self.assertEqual([call.args[0].splitlines()[-1] for call in send.call_args_list],
                                     ['전달되었습니다.'] if forwarded else [])

    def test_model_correcting_a_typo_passes_only_for_the_position_proven_newest_message(self):
        self.baseline()
        runtime.write_json('result-origins.json',[{'id':'one','text':'결과 원문','origin':{'source':'codex','session_id':'existing-thread'}}])
        crop=self.root/'isolated-reply.png'; crop.write_bytes(b'isolated bubble')
        typo='1시간도 안되옸눈데 300회가 넘었어 오...'
        reply={'sender':'self','time':'오후 1:01','text':typo,'is_photo':False,'is_file':False}
        later={'sender':'self','time':'오후 1:02','text':'그 뒤에 온 메시지','is_photo':False,'is_file':False}
        isolate=lambda message,image,cache=None,last=False,**kwargs:{'path':str(crop),'body':message['text'],'quote_kind':'text'}
        for name,reads,seen,read_body,gate in (
                ('the model corrected the typo',[[reply],[reply]],[],'1시간도 안되었는데 300회가 넘었어 오...',None),
                ('not the newest message',[[reply,later]],[bridge.fingerprint({**later,'_occurrence':1})],
                 '1시간도 안되었는데 300회가 넘었어 오...','model_read_another_body'),
                ('the model read another text',[[reply],[reply]],[],'전혀 다른 문장이 이 말풍선에 적혀 있습니다',
                 'model_read_another_body>model_read_another_body')):
            with self.subTest(name):
                runtime.write_json('baseline-hashes.json',seen)
                with patch.object(bridge,'native_read',side_effect=[copy.deepcopy(item) for item in reads]) as read, \
                     patch.object(bridge,'screenshot',return_value='synthetic.png'), \
                     patch.object(bridge,'isolate_reply',side_effect=isolate), \
                     patch.object(bridge,'message_kind',return_value=None), \
                     patch.object(bridge,'call_luna',return_value={'body':read_body,'quote':'결과 원문','quote_kind':'text'}), \
                     patch.object(bridge,'forward',return_value={'accepted':True}) as forward, \
                     patch.object(plain_delivery,'deliver') as send:
                    bridge.poll_once()
                    # What is forwarded is the clipboard original, typo included, never the model's reading.
                    self.assertEqual([call.args[1] for call in forward.call_args_list],[] if gate else [typo])
                    self.assertEqual(read.call_count,len(reads))
                    self.assertEqual([call.args[0].splitlines()[-1] for call in send.call_args_list],[] if gate else ['전달되었습니다.'])
                    if gate:
                        self.assertEqual(runtime.read_json('reply-log.json')[-1]['gate'],gate)

    def test_unreadable_screen_leaves_the_reply_for_the_next_check(self):
        self.baseline()
        runtime.write_json('result-origins.json',[{'id':'one','text':'결과 원문','origin':{'source':'codex','session_id':'existing-thread'}}])
        reply={'sender':'self','time':'오후 1:01','text':'진행해 주세요','is_photo':False,'is_file':False}
        def unreadable(message,image,cache=None,last=False,**kwargs):
            cache['isolate_stop']='ocr_unavailable'
        with patch.object(bridge,'native_read',side_effect=lambda:[copy.deepcopy(reply)]), \
             patch.object(bridge,'screenshot',return_value='synthetic.png'), \
             patch.object(bridge,'isolate_reply',side_effect=unreadable), \
             patch.object(bridge,'forward') as forward, patch.object(plain_delivery,'deliver') as send:
            for _ in range(5):
                bridge.poll_once()
            # Not consumed, nothing sent, and one log line however many checks repeat it.
            self.assertEqual(runtime.read_json('baseline-hashes.json'),[])
            self.assertEqual([(entry['stage'],entry['gate']) for entry in runtime.read_json('reply-log.json')],
                             [('screen_unreadable_retrying','ocr_unavailable')])
            # The sixth check gives up: consumed and logged as a failure, still nothing sent.
            bridge.poll_once()
        self.assertEqual(len(runtime.read_json('baseline-hashes.json')),1)
        forward.assert_not_called(); send.assert_not_called()
        self.assertEqual(runtime.read_json('reply-log.json')[-1]['gate'],'screen:ocr_unavailable>position:ocr_unavailable')

    def test_reply_to_a_quota_alert_is_its_answer_and_a_misread_command_is_logged(self):
        self.baseline()
        runtime.write_json('quota-alerts.json',{'offers':{'claude':[2,5]}})
        crop=self.root/'isolated-reply.png'; crop.write_bytes(b'isolated bubble')
        reply={'sender':'self','time':'오후 12:58','text':'어 해줘','is_photo':False,'is_file':False}
        isolate=lambda message,image,cache=None,last=False,**kwargs:{'path':str(crop),'body':message['text'],'quote_kind':'text',
                                                            'quote_text':'Claude Code · 잔여 한도 0%...'}
        with patch.object(bridge,'native_read',return_value=[copy.deepcopy(reply)]), \
             patch.object(bridge,'screenshot',return_value='synthetic.png'), \
             patch.object(bridge,'isolate_reply',side_effect=isolate), \
             patch.object(providers,'switch_account',return_value={'provider':'claude','account':2,'scope':'범위'}) as switch, \
             patch.object(bridge,'call_luna') as luna, patch.object(bridge,'forward') as forward, \
             patch.object(plain_delivery,'deliver') as send:
            bridge.poll_once()
        switch.assert_called_once_with('claude','2')
        luna.assert_not_called(); forward.assert_not_called()
        self.assertIn('claude 계정 2 활성 확인',send.call_args.args[0])
        # A command whose bubble cannot be proven plain is not run, and now leaves a log line.
        command={'sender':'self','time':'오후 12:59','text':'Talkturn help','is_photo':False,'is_file':False}
        (self.root/'view.png').write_bytes(b'capture')
        with patch.object(bridge,'native_read',return_value=[copy.deepcopy(command)]), \
             patch.object(bridge,'screenshot',return_value=str(self.root/'view.png')), \
             patch.object(bridge,'message_kind',return_value=None), \
             patch.object(plain_delivery,'deliver') as send:
            bridge.poll_once()
        send.assert_not_called()
        last=runtime.read_json('reply-log.json')[-1]
        self.assertEqual((last['stage'],last['gate']),('unverified_command','bubble_not_proven_plain'))
        self.assertTrue((self.root/'reply-failed'/last['detail']).exists())

    def test_codex_notify_called_back_by_the_previous_notify_stops(self):
        from talkturn import notify
        runtime.write_json('previous-notify.json',['wrapper.exe','turn-ended','--previous-notify','[talkturn.notify]'])
        event=json.dumps({'type':'agent-turn-complete','thread-id':'t','turn-id':'u','last-assistant-message':'끝'})
        with patch.object(notify.sys,'argv',['notify','--home',str(self.root),'codex-notify',event]), \
             patch.object(notify.subprocess,'Popen') as launch, patch.object(notify,'notice') as noticed:
            with patch.dict(os.environ,{'TALKTURN_NOTIFY_CHAINED':''}):
                notify.main()
            self.assertEqual(launch.call_args.kwargs['env']['TALKTURN_NOTIFY_CHAINED'],'1')
            with patch.dict(os.environ,{'TALKTURN_NOTIFY_CHAINED':'1'}):
                notify.main()
            self.assertEqual(launch.call_count,1)
            noticed.assert_not_called()  # 'enabled' is absent, and the chained call returns before it anyway.

    def test_same_session_forward_and_ambiguous_image(self):
        image=self.root/'result.png'; image.write_bytes(b'image')
        crop=self.root/'isolated-reply.png'; crop.write_bytes(b'isolated bubble')
        result={'id':'one','text':'결과 원문','images':[str(image)],'origin':{'source':'codex','session_id':'existing-thread'}}
        choice={'body':'진행','quote':'','quote_kind':'image'}
        isolated={'path':str(crop),'body':'진행','quote_thumbnail':str(image)}
        with patch.object(bridge,'call_luna',return_value=choice), \
             patch.object(bridge,'isolate_reply',return_value=isolated), \
             patch('talkturn.reply_photo.match_photos',return_value=[str(image)]):
            self.assertEqual(bridge.choose_origin({'text':'진행'},[result],'shot.png'),result)
            duplicate=dict(result,id='two',origin={'source':'codex','session_id':'other-thread'})
            self.assertIsNone(bridge.choose_origin({'text':'진행'},[duplicate,result],'shot.png'))
        with patch.object(bridge,'executable',return_value='codex.exe'),patch.object(bridge.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'Queued','')) as run:
            bridge.forward(result['origin'],'원문 $() ; 그대로')
            self.assertEqual(run.call_args.args[0],['codex.exe','queue','--thread','existing-thread','--message','원문 $() ; 그대로','--resume'])

    def test_reply_matches_since_connection_locally_without_sending_result_history(self):
        runtime.write_json('connection.json',{'at':100})
        old={'id':'old','text':'같은 인용 문장','sent_at':99,'origin':{'source':'codex','session_id':'old-thread'}}
        current={'id':'new','text':'같은 인용 문장 · 결과 본문은 모델에 보내지 않음','sent_at':101,'origin':{'source':'codex','session_id':'current-thread'}}
        crop=self.root/'isolated-reply.png'; crop.write_bytes(b'isolated bubble')
        with patch.object(bridge,'isolate_reply',return_value={'path':str(crop),'body':'이어가','quote_thumbnail':None}), \
             patch.object(bridge,'call_luna',return_value={'body':'이어가','quote':'같은 인용 문장','quote_kind':'text'}) as luna:
            self.assertEqual(bridge.choose_origin({'text':'이어가'},[old,current],'last-reply.png'),current)
            self.assertEqual(luna.call_count,1)
            self.assertEqual(luna.call_args.args[2],[str(crop.resolve())])
            self.assertNotIn(current['text'],luna.call_args.args[0])
            self.assertNotIn('old-thread',luna.call_args.args[0])
        with patch.object(bridge,'call_luna') as luna:
            self.assertIsNone(bridge.choose_origin({'text':'이어가'},[old],'last-reply.png'))
            luna.assert_not_called()
        # A session card carries the app connection reference and its results do not; both are one session.
        card={'id':'card','text':'[작업 제목]','sent_at':101,
              'origin':{'source':'claude','session_id':'S','peer_ref':'abc123','kind':'session_anchor'}}
        result={'id':'result','text':'[작업 제목]\n결과 본문','sent_at':102,'origin':{'source':'claude','session_id':'S','kind':'result'}}
        other={'id':'other','text':'[작업 제목]','sent_at':103,'origin':{'source':'claude','session_id':'T'}}
        with patch.object(bridge,'isolate_reply',return_value={'path':str(crop),'body':'이어가','quote_thumbnail':None}), \
             patch.object(bridge,'call_luna',return_value={'body':'이어가','quote':'[작업 제목]','quote_kind':'text'}):
            self.assertEqual(bridge.choose_origin({'text':'이어가'},[card,result],'last-reply.png'),result)
            self.assertIsNone(bridge.choose_origin({'text':'이어가'},[card,result,other],'last-reply.png'))
        # Another session's result that only mentions the card title in its body is not a second target.
        mention={'id':'mention','text':'[다른 작업]\n카드 이름: 작업 제목 외 3개','sent_at':104,'origin':{'source':'claude','session_id':'U'}}
        with patch.object(bridge,'isolate_reply',return_value={'path':str(crop),'body':'이어가','quote_thumbnail':None}), \
             patch.object(bridge,'call_luna',return_value={'body':'이어가','quote':'작업 제목','quote_kind':'text'}):
            self.assertEqual(bridge.choose_origin({'text':'이어가'},[card,result,mention],'last-reply.png'),result)

    def test_photo_upload_verification_waits_without_resending(self):
        from talkturn.native_send import wait_for_photo_count
        with patch('talkturn.native_send.time.sleep'),patch('talkturn.native_send.time.monotonic',side_effect=[0,1,2]):
            from unittest.mock import Mock
            read=Mock(side_effect=['[me] [오전 1:00] before','[me] [오전 1:00] before','[me] [오전 1:00] before\n[me] [오전 1:01] 사진'])
            self.assertTrue(wait_for_photo_count(read,0,timeout=12))
            self.assertEqual(read.call_count,3)

    def test_uncertain_delivery_never_retries_and_final_text_is_not_rewritten(self):
        runtime.write_json('delivery.json',{'mode':'native'})
        with patch.object(plain_delivery.subprocess,'run',side_effect=subprocess.TimeoutExpired('native',90)) as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                plain_delivery.deliver('원문 그대로','event')
            with self.assertRaisesRegex(RuntimeError,'uncertain'):
                plain_delivery.deliver('원문 그대로','event')
            self.assertEqual(run.call_count,1)
            self.assertEqual(json.loads(run.call_args.kwargs['input'])['text'],'원문 그대로')
        self.assertEqual(notice('claude',{'hook_event_name':'Stop','session_id':'s','last_assistant_message':'  결과  '})[0],'결과')
        self.assertIsNone(notice('claude',{'hook_event_name':'Stop','agent_id':'sub','last_assistant_message':'skip'}))

    def test_windows_worker_child_pid_is_accepted(self):
        runtime.write_json('delivery.json',{'window_id':123})
        with patch.object(service,'worker_pid',side_effect=[0,4321]), \
             patch.object(service,'process_running',side_effect=[False,True]), \
             patch.object(service.subprocess,'Popen') as spawn:
            spawn.return_value.pid=1234
            self.assertIn('시작됨',service.start(connect=False))
            self.assertTrue((self.root/'reply-enabled').exists())
            self.assertEqual(spawn.call_args.args[0][-1],'talkturn.reply_bridge')

    def test_status_focuses_on_ai_work_not_transport(self):
        tasks.record('codex',{'session_id':'thread','model':'gpt-6-astra','cwd':'project'},'진행 중')
        with patch.object(providers,'usage',return_value={'codex':{'limits':[]}}), \
             patch.object(providers,'login_state',return_value=False), \
             patch.object(providers,'model_settings',return_value={'codex':{'model':'gpt-6-astra'},'claude':{'model':'opus'}}), \
             patch('talkturn.sessions.discover',return_value={'rows':[{'provider':'codex','session_id':'thread','name':'project','code':'X-TEST','state':'진행 중','model':'gpt-6-astra'}],'errors':{}}):
            text=commands.execute(['status'])
        self.assertIn('진행 중',text)
        self.assertIn('gpt-6-astra',text)
        self.assertNotIn('수신기',text)
        self.assertNotIn('PC 사용 중',text)


if __name__=='__main__':
    unittest.main()
