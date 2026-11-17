from contextlib import ExitStack
import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch,Mock
from talkturn import sessions,runtime,commands,reply_bridge,cli
from talkturn.codex_rpc import CodexReader


FIRST='11111111-1111-4111-8111-111111111111'
SECOND='22222222-2222-4222-8222-222222222222'


def row(provider,session,name='동일한 작업 제목'):
    return {'provider':provider,'session_id':session,'name':name,'code':sessions.code(provider,session),
            'state':'진행 중','model':'gpt-6-astra' if provider=='codex' else None,'cwd':'project','updated_at':1,'reachable':True}


class ExistingSessionsTest(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack(); self.addCleanup(self.stack.close)
        self.root=Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(runtime,'ROOT',self.root))
        self.stack.enter_context(patch.dict(os.environ,{'APPDATA':str(self.root/'appdata')}))
        runtime.write_json('delivery.json',{'model':'gpt-6-luna'})

    def test_live_codex_metadata_excludes_subagents_without_resuming_any_thread(self):
        parent={'id':FIRST,'name':'진짜 실행 중 작업','status':{'type':'active','activeFlags':['waitingOnUserInput']},'model':'gpt-6-astra'}
        child={'id':SECOND,'parentThreadId':FIRST,'name':'하위 에이전트'}
        fake=Mock(shared=True)
        def call(method,params):
            if method=='thread/list':
                return {'data':[parent],'nextCursor':None}
            if method=='thread/loaded/list':
                return {'data':[FIRST,SECOND],'nextCursor':None}
            self.assertEqual(method,'thread/read')
            self.assertFalse(params['includeTurns'])
            return {'thread':parent if params['threadId']==FIRST else child}
        fake.call.side_effect=call
        with patch.object(sessions,'CodexReader') as reader:
            reader.return_value.__enter__.return_value=fake
            rows,note=sessions.codex_sessions()
        self.assertIsNone(note)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['name'],'진짜 실행 중 작업')
        self.assertEqual(rows[0]['state'],'질문 대기')
        self.assertTrue(rows[0]['live'])
        self.assertFalse(any('resume' in str(call) or 'start' in str(call) for call in fake.call.call_args_list))

    def test_claude_titles_use_exact_session_metadata_without_changing_routing(self):
        root=self.root/'claude'
        project=root/'projects'/'project'
        project.mkdir(parents=True)
        path=project/(FIRST+'.jsonl')
        path.write_text('\n'.join([
            json.dumps({'type':'user','message':'private conversation, not a title'}),
            json.dumps({'type':'ai-title','aiTitle':'original task','sessionId':FIRST}),
            json.dumps({'type':'custom-title','customTitle':'renamed task','sessionId':FIRST}),
            json.dumps({'type':'ai-title','aiTitle':'later generated title','sessionId':FIRST}),
            json.dumps({'type':'custom-title','customTitle':'wrong session','sessionId':SECOND}),
        ])+'\n',encoding='utf-8')
        registry=[{'sessionId':FIRST,'name':'project-d3','cwd':'project','status':'idle'},
                  {'sessionId':SECOND,'name':'project-ac','cwd':'project','status':'idle'}]
        peers=[{'name':item['name'],'ref':str(i),'status':'idle'} for i,item in enumerate(registry)]
        with patch.dict(os.environ,{'CLAUDE_CONFIG_DIR':str(root)}), \
             patch.object(sessions,'run_json',return_value=registry), \
             patch.object(sessions,'executable',return_value='claude.exe'), \
             patch.object(sessions,'claude_targets',return_value={'targets':peers}), \
             patch.object(sessions,'claude_desktop_sessions',return_value=[]):
            rows,note=sessions.claude_sessions()
            self.assertIsNone(note)
            self.assertEqual(rows[0]['name'],'renamed task')
            self.assertEqual(rows[0]['agent_name'],'project-d3')
            self.assertEqual(rows[0]['peer_ref'],'0')
            self.assertTrue(rows[0]['reachable'])
            self.assertEqual(rows[0]['session_id'],FIRST)
            self.assertEqual(rows[1]['name'],'제목 없는 작업 · '+SECOND[:8])
            with path.open('a',encoding='utf-8') as output:
                output.write(json.dumps({'type':'custom-title','customTitle':'new rename','sessionId':FIRST})+'\n')
            self.assertEqual(sessions.claude_title(FIRST),'new rename')
            self.assertNotIn('private conversation',json.dumps(rows))

    def test_saved_fallback_does_not_claim_to_be_live(self):
        fake=Mock(shared=False)
        fake.call.return_value={'data':[{'id':FIRST,'name':'저장된 작업','status':{'type':'notLoaded'}}]}
        with patch.object(sessions,'CodexReader') as reader:
            reader.return_value.__enter__.return_value=fake
            rows,note=sessions.codex_sessions()
        self.assertEqual(rows[0]['state'],'실행 상태 미확인')
        self.assertFalse(rows[0]['live'])
        self.assertIn('미확인',note)

    def test_selection_and_send_keep_exact_uuid_despite_reordering_and_duplicate_titles(self):
        first=row('codex',FIRST); second=row('codex',SECOND)
        catalog={'rows':[first,second],'fetched_at':time.time(),'errors':{}}
        runtime.write_json('session-catalog.json',catalog)
        runtime.write_json('delivery.json',{'model':'gpt-6-luna'})
        with patch.object(sessions,'validate_target',side_effect=lambda item:copy.deepcopy(item)), \
             patch.object(reply_bridge,'forward',return_value={'accepted':True}) as forward:
            picked=sessions.choose(first['code'])
            self.assertEqual(picked['session_id'],FIRST)
            catalog['rows'].reverse()
            runtime.write_json('session-catalog.json',catalog)
            original='이 작업만  계속해 $() ; 내용 그대로'
            result=commands.execute(commands.parse('톡턴 보내 '+original,remote=True),remote=True)
            self.assertIn('원래 세션',result)
            self.assertEqual(forward.call_args.args[0]['session_id'],FIRST)
            self.assertEqual(forward.call_args.args[1],original)
        self.assertEqual(runtime.read_json('delivery.json'),{'model':'gpt-6-luna'})

    def test_missing_or_ambiguous_sessions_never_fall_back_to_another_task(self):
        first=row('codex',FIRST); second=row('codex',SECOND)
        second['code']=first['code']
        with self.assertRaises(ValueError):
            sessions.choose(first['code'],{'rows':[first,second],'fetched_at':time.time()})
        runtime.write_json('ui.json',{'selected_session':first})
        with patch.object(sessions,'validate_target',side_effect=ValueError('session gone')),patch.object(reply_bridge,'forward') as forward:
            with self.assertRaises(ValueError):
                sessions.send_selected('이어가')
            forward.assert_not_called()

    def test_claude_lists_all_cli_sessions_but_identifies_message_receivers(self):
        cli=[{'sessionId':FIRST,'name':'열린 작업','state':'blocked'}, {'sessionId':SECOND,'name':'닫힌 작업','state':'blocked'}]
        with patch.object(sessions,'executable',return_value='claude.exe'),patch.object(sessions,'run_json',return_value=cli), \
             patch.object(sessions,'claude_targets',return_value={'targets':[{'name':'열린 작업','ref':'fresh','status':'idle'}]}):
            rows,note=sessions.claude_sessions()
        self.assertIsNone(note)
        self.assertEqual(len(rows),2)
        self.assertTrue(rows[0]['reachable'])
        self.assertEqual(rows[1]['state'],'수신 연결 없음')
        with patch.object(sessions,'claude_sessions',return_value=(rows,None)):
            with self.assertRaises(ValueError):
                sessions.validate_target(rows[1])
            earlier=dict(rows[0],peer_ref='different-receiver')
            with self.assertRaises(ValueError):
                sessions.validate_target(earlier)

    def test_kakao_lists_both_provider_groups_and_requires_stable_codes(self):
        catalog={'rows':[{**row('claude',FIRST,'Claude 실제 작업'),'live':True},{**row('codex',SECOND,'Codex 실제 작업'),'live':True}],
                 'errors':{},'fetched_at':time.time()}
        runtime.write_json('ui.json',{'section':'claude'})
        with patch.object(sessions,'discover',return_value=catalog):
            result=commands.execute(commands.parse('톡턴 세션',remote=True),remote=True)
        self.assertIn('Claude 실제 작업',result)
        self.assertIn('Codex 실제 작업',result)
        self.assertEqual(commands.parse('톡턴 섹션',remote=True),['sessions'])
        with self.assertRaises(ValueError):
            sessions.choose('1',catalog)
        reader=CodexReader()
        with self.assertRaises(ValueError):
            reader.call('thread/resume',{'threadId':FIRST})

    def test_arrow_space_picker_keeps_multiple_checks_until_enter(self):
        from talkturn import terminal
        from types import SimpleNamespace
        from rich.console import Console
        from rich.text import Text
        import io
        offline=dict(row('claude','offline'),reachable=False)
        items=[row('claude',FIRST),offline,row('claude',SECOND),row('codex',FIRST),row('codex',SECOND)]
        view={'rows':items,'selected':None,'watched':[]}
        keys=[' ','\xe0','P','\xe0','P',' ','\xe0','M',' ',' ',' ','\xe0','H','\r']
        seen=[]
        keyboard=SimpleNamespace(getwch=lambda:keys.pop(0),kbhit=lambda:bool(keys))
        console=Console(file=io.StringIO(),force_terminal=True,legacy_windows=False,width=120,height=40)
        with patch.object(terminal.os,'name','nt'),patch.object(terminal.sys.stdin,'isatty',return_value=True), \
             patch.dict('sys.modules',{'msvcrt':keyboard}), \
             patch.object(terminal,'dashboard',side_effect=lambda _,state,**kwargs:seen.append((state['focused_code'],set(state['checked_codes']))) or Text('picker')):
            result=terminal.keyboard_pick(console,view)
        expected={items[0]['code'],items[2]['code'],items[4]['code']}
        self.assertEqual(set(result),expected)
        self.assertNotIn(offline['code'],result)
        self.assertEqual(seen[-1],(items[3]['code'],expected))
        self.assertNotIn('checked_codes',view)
        self.assertIsNone(sessions.selected())

    def test_provider_tabs_show_only_active_rows_keep_checks_and_allow_empty_tab(self):
        from talkturn import terminal
        from types import SimpleNamespace
        from rich.console import Console
        import io
        items=[row('claude',FIRST,'claude-first'),row('claude',SECOND,'claude-second'),row('codex',FIRST,'codex-only')]
        for include_codex in (True,False):
            view={'rows':items if include_codex else items[:2],'selected':None,'watched':[],'usage':{},'waiting':[]}
            console=Console(file=io.StringIO(),force_terminal=True,legacy_windows=False,width=80,height=24)
            keys=[' ','\xe0','P',' ','\xe0','M',' ','\xe0','K','\r']
            frames=[]
            original=terminal.dashboard
            def render(console,state,**kwargs):
                result=original(console,state,**kwargs)
                segments=list(console.render(result))
                text=''.join(segment.text for segment in segments)
                active=state['active_provider']
                inactive='Codex' if active=='claude' else 'Claude Code'
                self.assertTrue(any(inactive in segment.text and segment.style and
                                    segment.style.color and segment.style.color.name=='grey70'
                                    for segment in segments))
                active_label='Claude Code' if active=='claude' else 'Codex'
                self.assertTrue(any(active_label in segment.text and segment.style and segment.style.bold and
                                    segment.style.color and segment.style.color.name=='bright_white'
                                    for segment in segments))
                tab_lines=[line for line in text.splitlines() if '← Claude Code' in line]
                self.assertEqual(len(tab_lines),1)
                self.assertTrue(tab_lines[0].lstrip().startswith('╭'))
                self.assertIn('Codex',tab_lines[0]); self.assertIn('→',tab_lines[0])
                self.assertTrue(any(line.lstrip().startswith('╰') and '페이지' in line for line in text.splitlines()))
                self.assertIn('talkturn',text); self.assertNotIn('╭─ tt ─╮',text)
                if active=='claude':
                    self.assertIn('claude-first',text); self.assertNotIn('codex-only',text)
                else:
                    self.assertNotIn('claude-first',text)
                    self.assertIn('codex-only' if include_codex else '열린 작업 세션 없음',text)
                frames.append((active,state['focused_code']))
                return result
            keyboard=SimpleNamespace(getwch=lambda:keys.pop(0),kbhit=lambda:bool(keys))
            with patch.object(terminal.sys.stdin,'isatty',return_value=True), \
                 patch.dict('sys.modules',{'msvcrt':keyboard}),patch.object(terminal,'dashboard',side_effect=render):
                checked=terminal.keyboard_pick(console,view)
            self.assertEqual(set(checked),{item['code'] for item in view['rows']})
            self.assertEqual(frames[-1],('claude',items[1]['code']))
            self.assertIn(('codex',items[2]['code'] if include_codex else None),frames)

    def test_picker_starts_once_updates_only_changed_rows_and_rejects_accidental_enter(self):
        from talkturn import terminal,tasks
        from rich.console import Console
        from types import SimpleNamespace
        import io
        output=io.StringIO()
        console=Console(file=output,force_terminal=True,legacy_windows=False,width=110,height=28)
        items=[row('claude',FIRST,'Claude app task'),row('codex',SECOND,'Codex app task')]
        view={'rows':items,'selected':None,'watched':[],'usage':{},'waiting':[]}
        # Empty Enter, invalid numbered Enter, then a valid checkbox selection.
        keys=['\r','9','\r','\b','\xe0','M',' ','\r','\x1b']
        frames=[]; previous=0
        def getwch():
            nonlocal previous
            frames.append(output.getvalue()[previous:]); previous=len(output.getvalue())
            return keys.pop(0)
        keyboard=SimpleNamespace(getwch=getwch,kbhit=lambda:bool(keys))
        with patch.object(terminal.os,'name','nt'),patch.object(terminal.sys.stdin,'isatty',return_value=True), \
             patch.dict('sys.modules',{'msvcrt':keyboard}),patch.object(tasks,'snapshot',return_value=view), \
             patch.object(cli,'console',console),patch.object(console,'status'), \
             patch.object(console,'input',return_value='quit'),patch.object(sessions,'watch_many',return_value=[]) as watch, \
             patch.object(sessions,'watch_text',return_value='selection saved'), \
             patch.object(console,'update_screen_lines',wraps=console.update_screen_lines) as updates:
            cli.interactive()
        input_rows=[call.kwargs['y'] for call in updates.call_args_list
                    if any('입력:' in segment.text for line in call.args[0] for segment in line)]
        self.assertTrue(input_rows)
        self.assertTrue(all(y==console.height-2 for y in input_rows))
        self.assertEqual(watch.call_args.args[0],[items[1]['code']])
        self.assertEqual(frames[0].count('▀█▀ ▄▀█ █   █▄▀'),1)
        self.assertNotIn('\x1b[2J',output.getvalue())
        self.assertIn('Space로 받을 작업',frames[1])
        self.assertIn('목록의 번호/코드',frames[3])
        for frame in frames[1:-1]:
            self.assertNotIn('▀█▀ ▄▀█ █   █▄▀',frame)
        self.assertFalse(console.is_alt_screen)

    def test_shortcuts_follow_box_with_one_blank_line_and_input_stays_at_bottom(self):
        from talkturn import terminal
        from rich.console import Console
        from types import SimpleNamespace
        import io
        for height in (24,32):
            with self.subTest(height=height):
                console=Console(file=io.StringIO(),force_terminal=True,legacy_windows=False,width=80,height=height)
                items=[row('claude',f'task-{i}',f'Task {i:02d}') for i in range(1,11)]
                view={'rows':items,'watched':[items[0]],'usage':{},'waiting':[]}
                keys=['\x1b']
                keyboard=SimpleNamespace(getwch=lambda:keys.pop(0),kbhit=lambda:bool(keys))
                with patch.object(terminal.os,'name','nt'),patch.object(terminal.sys.stdin,'isatty',return_value=True), \
                     patch.dict('sys.modules',{'msvcrt':keyboard}), \
                     patch.object(console,'update_screen_lines') as updates:
                    terminal.keyboard_pick(console,view)
                lines={call.kwargs['y']:''.join(segment.text for line in call.args[0] for segment in line)
                       for call in updates.call_args_list}
                box_bottom=next(y for y,text in lines.items() if '╰' in text and '페이지' in text)
                shortcuts=next(y for y,text in lines.items() if '[↑↓]' in text)
                self.assertEqual(shortcuts,box_bottom+2)
                self.assertFalse(lines[box_bottom+1].strip())
                self.assertIn('입력:',lines[height-2])
                self.assertTrue(any('Task 10' in text for text in lines.values()))
                self.assertTrue(any('컴퓨터 이름 수정' in text for text in lines.values()))

    def test_multiple_watches_filter_completion_and_questions_without_broadcasting(self):
        import io,json
        from talkturn import notify,questions
        first=row('codex',FIRST); second=row('claude',SECOND)
        catalog={'rows':[first,second],'fetched_at':time.time()}
        self.assertEqual(sessions.watched(),[])
        self.assertFalse(sessions.receives_notifications('claude',{'session_id':FIRST}))
        sessions.watch_many([first['code'],second['code']],catalog)
        self.assertIsNone(sessions.selected())
        self.assertTrue(sessions.receives_notifications('codex',{'session_id':FIRST}))
        self.assertTrue(sessions.receives_notifications('claude',{'session_id':SECOND}))
        self.assertFalse(sessions.receives_notifications('claude',{'session_id':FIRST}))
        (self.root/'enabled').touch()
        with patch.object(notify,'ROOT',self.root),patch.object(questions,'ROOT',self.root), \
             patch('talkturn.plain_delivery.deliver') as deliver,patch('sys.stdout',io.StringIO()):
            for session in (FIRST,SECOND):
                event={'hook_event_name':'Stop','session_id':session,'last_assistant_message':'exact final result'}
                with patch('sys.argv',['talkturn.notify','hook','--source','codex']),patch('sys.stdin',io.StringIO(json.dumps(event))):
                    notify.main()
            self.assertEqual(deliver.call_count,1)
            self.assertEqual(deliver.call_args.kwargs['origin']['session_id'],FIRST)
            self.assertEqual(questions.handle('claude',{'hook_event_name':'Notification','session_id':FIRST,'message':'unwatched'},wait=False),{})
            self.assertEqual(deliver.call_count,1)
            questions.handle('claude',{'hook_event_name':'Notification','session_id':SECOND,'message':'watched'},wait=False)
            self.assertEqual(deliver.call_count,1)  # Passive notifications have no answer channel.
            question={'hook_event_name':'PreToolUse','tool_name':'AskUserQuestion',
                      'tool_input':{'questions':[{'question':'Continue?','options':[{'label':'Yes'}]}]}}
            questions.handle('claude',{**question,'session_id':FIRST},wait=False)
            self.assertEqual(deliver.call_count,1)
            questions.handle('claude',{**question,'session_id':SECOND},wait=False)
            self.assertEqual(deliver.call_count,2)
        with self.assertRaises(ValueError):
            sessions.watch_many(['X-MISSING'],catalog)
        self.assertEqual(len(sessions.watched()),2)
        sessions.watch_many([],catalog)
        self.assertFalse(sessions.receives_notifications('codex',{'session_id':FIRST}))

    def test_interactive_code_selection_can_choose_from_a_newer_recent_list(self):
        old=row('codex',FIRST,'처음 표시된 작업')
        recent=row('codex',SECOND,'최근 목록에서 고른 작업')
        runtime.write_json('session-catalog.json',{'rows':[recent],'fetched_at':time.time(),'errors':{}})
        with patch('talkturn.terminal.show_dashboard',return_value={'rows':[old]}), \
             patch('talkturn.terminal.response'),patch('talkturn.terminal.keyboard_pick',return_value=''), \
             patch.object(cli.console,'input',side_effect=['select '+recent['code'],'quit']), \
             patch.object(sessions,'validate_target',side_effect=lambda value:value), \
             patch.object(reply_bridge,'forward') as forward:
            cli.interactive()
        self.assertEqual(sessions.selected()['session_id'],SECOND)
        forward.assert_not_called()

    def test_terminal_enter_preserves_selection_and_shows_number_and_session_name_only(self):
        from talkturn import terminal
        from rich.console import Console
        import io
        cli_rows=[{'sessionId':FIRST,'name':'desktop task','state':'blocked','model':'claude-opus-5-5','effort':'xhigh'}]
        peers=[{'name':'desktop task','ref':'desktop-ref','type':'desktop','status':'idle'}]
        with patch.object(sessions,'run_json',return_value=cli_rows),patch.object(sessions,'executable',return_value='claude.exe'), \
             patch.object(sessions,'claude_targets',return_value={'targets':peers}):
            rows,note=sessions.claude_sessions()
        self.assertEqual(rows[0]['client'],'Claude 앱')
        output=io.StringIO(); console=Console(file=output,width=110,color_system=None)
        view={'rows':rows,'selected':None,'watched':[],'waiting':[],'usage':{}}
        terminal.dashboard(console,view)
        self.assertIn('번호',output.getvalue()); self.assertIn('세션 이름',output.getvalue())
        self.assertIn('[ ] desktop task',output.getvalue())
        self.assertNotIn('claude-opus-5-5',output.getvalue()); self.assertNotIn('xhigh',output.getvalue())
        self.assertNotIn('추론 강도',output.getvalue()); self.assertNotIn('저장/최근 확인값',output.getvalue())
        with patch.object(cli,'console',console),patch.object(console,'input',side_effect=['','quit']), \
             patch.object(terminal,'show_dashboard',return_value=view) as load, \
             patch.object(terminal,'keyboard_pick',return_value='') as pick,patch.object(sessions,'watch_many') as watch:
            cli.interactive()
        self.assertEqual(pick.call_count,2); self.assertEqual(load.call_count,2)
        self.assertTrue(all(call.kwargs=={'render':False} for call in load.call_args_list))
        watch.assert_not_called(); self.assertIsNone(sessions.selected())

    def test_claude_code_app_peer_keeps_native_reference_without_fabricating_uuid(self):
        peer={'name':'Claude 앱의 Code 작업','ref':'desktop-live-ref','type':'desktop','status':'idle'}
        with ExitStack() as stack:
            root=Path(stack.enter_context(tempfile.TemporaryDirectory()))
            stack.enter_context(patch.object(runtime,'ROOT',root))
            stack.enter_context(patch.object(sessions,'executable',return_value='claude.exe'))
            stack.enter_context(patch.object(sessions,'run_json',return_value=[]))
            listing=stack.enter_context(patch.object(sessions,'claude_targets',return_value={'targets':[peer]}))
            rows,note=sessions.claude_sessions()
            self.assertIsNone(note); self.assertEqual(len(rows),1)
            item=rows[0]; self.assertIsNone(item['session_id']); self.assertEqual(item['client'],'Claude 앱')
            picked=sessions.choose(item['code'],{'rows':rows,'fetched_at':time.time()})
            self.assertEqual(sessions.identity(picked),('claude','peer:desktop-live-ref'))
            with patch.object(reply_bridge,'forward',return_value={'accepted':True}) as forward:
                sessions.send_selected('같은 앱 작업에서 이어서',expected=item)
            self.assertEqual(forward.call_args.args[0]['peer_ref'],peer['ref'])
            self.assertEqual(forward.call_args.args[0]['target_kind'],'claude_peer')
            listing.return_value={'targets':[{**peer,'ref':'replacement'}]}
            with self.assertRaises(ValueError):
                sessions.validate_target(item)

    def test_desktop_indexes_merge_without_reading_transcripts_or_guessing_live_state(self):
        metadata=Path(os.environ['APPDATA'])/'Claude/claude-code-sessions/account/org'
        metadata.mkdir(parents=True)
        for session,title in [(FIRST,'desktop running'),(SECOND,'desktop saved')]:
            (metadata/f'local_{session}.json').write_text(json.dumps({'sessionId':'local_'+session,'cliSessionId':session,
                'title':title,'model':'claude-opus-5-5','effort':'xhigh','lastActivityAt':1794000000000,'isArchived':False,
                'promptAppendSnapshot':{'secret':'do not expose'}}),encoding='utf-8')
        (metadata/'local_broken.json').write_text('{',encoding='utf-8')
        with patch.object(sessions,'executable',return_value='claude.exe'), \
             patch.object(sessions,'run_json',return_value=[{'sessionId':FIRST,'name':'CLI alias','state':'idle'}]), \
             patch.object(sessions,'claude_targets',return_value={'targets':[{'name':'CLI alias','ref':'original','type':'bg','status':'idle'}]}):
            rows,note=sessions.claude_sessions()
        self.assertEqual(len(rows),2)
        self.assertEqual({item['name'] for item in rows},{'CLI alias','desktop saved'})
        self.assertEqual(rows[0]['app_title'],'desktop running')
        self.assertTrue(rows[0]['reachable']); self.assertEqual(rows[0]['peer_ref'],'original')
        self.assertEqual((rows[0]['model'],rows[0]['effort']),('claude-opus-5-5','xhigh'))
        self.assertEqual((rows[0]['model_source'],rows[0]['effort_source']),('app_saved','app_saved'))
        self.assertFalse(rows[1]['reachable']); self.assertEqual(rows[1]['state'],'앱 연결 대기')
        self.assertEqual(rows[1]['effort_source'],'app_saved')
        self.assertNotIn('do not expose',json.dumps(rows))
        with patch.object(sessions,'executable',return_value='claude.exe'), \
             patch.object(sessions,'run_json',return_value=[{'sessionId':FIRST,'name':'CLI alias','state':'idle',
                 'model':'claude-sonnet-5-5','effort':'high'},{'sessionId':'33333333-3333-4333-8333-333333333333','name':'CLI only'}]), \
             patch.object(sessions,'claude_targets',return_value={'targets':[]}):
            actual,_=sessions.claude_sessions()
        self.assertEqual((actual[0]['model'],actual[0]['effort']),('claude-sonnet-5-5','high'))
        self.assertEqual((actual[0]['model_source'],actual[0]['effort_source']),('session_registry','session_registry'))
        self.assertIsNone(actual[1]['model']); self.assertIsNone(actual[1]['effort'])
        fake=Mock(shared=True)
        def call(method,params):
            if method=='thread/list':
                return {'data':[{'id':FIRST,'name':'app sidebar active','source':'vscode','status':{'type':'idle'}},
                    {'id':SECOND,'name':'app sidebar saved','source':'vscode','status':{'type':'notLoaded'},
                     'model':'gpt-6-astra','reasoningEffort':'high'}]}
            if method=='thread/loaded/list': return {'data':[FIRST]}
            return {'thread':{'id':FIRST,'name':None,'source':'vscode','status':{'type':'active'},'canAcceptDirectInput':True,
                              'model':'gpt-6.1-sol','reasoningEffort':'xhigh'}}
        fake.call.side_effect=call
        with patch.object(sessions,'CodexReader') as reader:
            reader.return_value.__enter__.return_value=fake
            rows,note=sessions.codex_sessions()
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['name'],'app sidebar active'); self.assertEqual(rows[0]['state'],'진행 중')
        self.assertEqual((rows[0]['model'],rows[0]['effort'],rows[0]['effort_source']),('gpt-6.1-sol','xhigh','thread_live'))
        self.assertFalse(rows[1]['live']); self.assertEqual(rows[1]['name'],'app sidebar saved')
        self.assertEqual((rows[1]['model'],rows[1]['effort'],rows[1]['effort_source']),('gpt-6-astra','high','thread_saved'))
        self.assertFalse(any('resume' in str(item) or 'start' in str(item) for item in fake.call.call_args_list))

    def test_existing_ai_is_auto_detected_and_missing_ai_opens_only_requested_terminal(self):
        from talkturn import connections,terminal
        live={**row('codex',FIRST),'live':True}
        with patch.object(sessions,'discover',return_value={'rows':[live]}), \
             patch.object(connections,'executable',return_value='claude.exe'),patch.object(connections.subprocess,'Popen') as launch:
            self.assertIn('자동 연결',connections.connect('codex')); launch.assert_not_called()
            self.assertIn('터미널',connections.connect('claude'))
            self.assertEqual(launch.call_args.args[0],['claude.exe'])
            self.assertEqual(launch.call_args.kwargs['creationflags'],connections.subprocess.CREATE_NEW_CONSOLE)
            with self.assertRaises(ValueError): connections.connect('claude;whoami')
            self.assertEqual(launch.call_count,1)
        with patch.object(terminal,'dashboard'),patch.object(terminal.sys.stdin,'isatty',return_value=False),patch('builtins.input',return_value='+'):
            self.assertEqual(terminal.keyboard_pick(Mock(),{'rows':[]} ),'+')


if __name__=='__main__':
    unittest.main()
