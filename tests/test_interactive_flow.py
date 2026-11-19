import copy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from rich.console import Console
from talkturn import cli,runtime,sessions,terminal,account_menu,service


class InteractiveFlowTest(unittest.TestCase):
    def setUp(self):
        lock=patch('talkturn.mouse_lock.engage',return_value=True)
        self.lock=lock.start()
        self.addCleanup(lock.stop)
        tty=patch.object(cli.sys.stdin,'isatty',return_value=False)
        tty.start()
        self.addCleanup(tty.stop)

    def test_confirm_stays_in_menu_and_connect_refresh_and_legacy_keys_work(self):
        row={'provider':'claude','session_id':'same-task','code':'C-12345678','name':'Existing work'}
        view={'rows':[row],'watched':[],'selected':None,'active_provider':'claude'}
        actions=iter([[row['code']],{'action':'setup','provider':'claude','checked_codes':[row['code']]},
                      {'action':'refresh','provider':'claude','checked_codes':[row['code']]},'', ''])
        seen=[]
        def pick(console,current):
            seen.append(copy.deepcopy(current))
            return next(actions)
        with tempfile.TemporaryDirectory() as folder,patch.object(runtime,'ROOT',Path(folder)), \
             patch.object(cli,'console',Console(file=io.StringIO())), \
             patch.object(terminal,'show_dashboard',return_value=view) as load, \
             patch.object(terminal,'keyboard_pick',side_effect=pick),patch.object(terminal,'response'), \
             patch.object(cli.console,'input',side_effect=['a','quit']) as read, \
             patch.object(cli,'setup',return_value='카톡 연결 완료') as setup, \
             patch.object(service,'delivery_issues',return_value=[]), \
             patch.object(account_menu,'add',return_value='계정 추가 취소.') as add:
            runtime.write_json('delivery.json',{'model':'gpt-6-luna'})
            cli.interactive()
            self.assertEqual(sessions.watched(),[row])
        self.assertEqual(len(seen),5)
        self.assertEqual(seen[1]['watched'],[row])
        self.assertIn('확정',seen[1]['action_notice'])
        setup.assert_called_once_with(['--relay-model','gpt-6-luna'])
        self.assertEqual(load.call_count,3)
        add.assert_called_once()
        self.assertEqual(add.call_args.args[1],'claude')
        self.assertEqual(read.call_count,2)

    def test_enter_connects_selected_work_once_and_keeps_cancelled_setup_honest(self):
        row={'provider':'codex','session_id':'work','code':'X-12345678','name':'Work'}
        for state in ('connected','first','cancelled','empty'):
            with self.subTest(state=state),tempfile.TemporaryDirectory() as folder, \
                 patch.object(runtime,'ROOT',Path(folder)), \
                 patch.object(cli,'console',Console(file=io.StringIO())), \
                 patch.object(terminal,'show_dashboard',return_value={'rows':[row],'watched':[]}) as load, \
                 patch.object(terminal,'keyboard_pick',side_effect=[[row['code']] if state!='empty' else [],'']), \
                 patch.object(cli.console,'input',return_value='quit'), \
                 patch.object(service,'delivery_issues',side_effect=([['missing']] if state=='cancelled' else [['missing'],[]] if state=='first' else [[]])) as issues, \
                 patch.object(cli,'setup') as setup:
                def run_setup(args,*,commit_selection):
                    if state=='cancelled':
                        return '설정 취소.'
                    commit_selection()
                    return '연결 완료'
                setup.side_effect=run_setup
                runtime.write_json('delivery.json',{'model':'gpt-6-luna'})
                cli.interactive()
                view=load.return_value
                self.assertEqual(len(sessions.watched()),0 if state in ('empty','cancelled') else 1)
                if state in ('first','cancelled'):
                    setup.assert_called_once()
                else:
                    setup.assert_not_called()
                if state=='empty': issues.assert_not_called()
                if state in ('connected','first'):
                    self.assertIn('카톡 연결됨',view['action_notice'])
                if state=='cancelled':
                    self.assertIn('취소',view['action_notice'])
                    self.assertNotIn('카톡 연결됨',view['action_notice'])

    def test_closed_self_chat_keeps_checks_and_enter_retries_connection_after_opening(self):
        row={'provider':'codex','session_id':'work','code':'X-12345678','name':'Work'}
        for first in ('나와의 채팅 닫힘','카카오톡 꺼짐','first-setup'):
            with self.subTest(first=first),tempfile.TemporaryDirectory() as folder:
                view={'rows':[row],'watched':[],'action_notice':'이전 알림'}
                snapshots=[]
                def pick(console,current):
                    snapshots.append(copy.deepcopy(current))
                    if len(snapshots)==1:
                        return [row['code']]
                    if len(snapshots)==2:
                        self.assertEqual(current.pop('connection_alert'),'카톡 ‘나와의 채팅’을 열어주세요')
                        self.assertEqual(sessions.watched(),[])
                        self.assertEqual(current['draft_checked_codes'],[row['code']])
                        self.assertNotIn('action_notice',current)
                        self.assertEqual(setup.call_count,1 if first=='first-setup' else 0)
                        return [row['code']]  # The central alert's Enter retries the preserved checks.
                    return ''
                issues=([['나와의 채팅 미연결']] if first=='first-setup' else [[first]])
                issues += [['카톡 창이 바뀜 · setup 필요'],[]]
                def run_setup(args,*,commit_selection):
                    if first=='first-setup' and setup.call_count==1:
                        return '나와의 채팅 창이 없습니다. 별도 창을 연 뒤 setup을 다시 실행하세요.'
                    commit_selection()
                    return '카톡 연결 완료'
                with patch.object(runtime,'ROOT',Path(folder)), \
                     patch.object(cli,'console',Console(file=io.StringIO())), \
                     patch.object(terminal,'show_dashboard',return_value=view), \
                     patch.object(terminal,'keyboard_pick',side_effect=pick), \
                     patch.object(cli.console,'input',return_value='quit'), \
                     patch.object(service,'delivery_issues',side_effect=issues), \
                     patch.object(cli,'setup',side_effect=run_setup) as setup:
                    runtime.write_json('delivery.json',{'model':'gpt-6-luna'})
                    cli.interactive()
                    self.assertEqual(sessions.watched(),[row])
                self.assertEqual(len(snapshots),3)
                self.assertIn('카톡 연결됨',snapshots[-1]['action_notice'])
                self.assertNotIn('connection_alert',snapshots[-1])
                self.assertEqual(setup.call_count,2 if first=='first-setup' else 1)

    def test_first_visit_and_cancelled_connection_preserve_saved_selection_and_model(self):
        old={'provider':'codex','session_id':'old','code':'X-OLD','name':'기존 작업'}
        new={'provider':'codex','session_id':'new','code':'X-NEW','name':'새 작업'}
        view={'rows':[old,new],'watched':[old]}
        with tempfile.TemporaryDirectory() as folder,patch.object(runtime,'ROOT',Path(folder)), \
             patch.object(cli,'console',Console(file=io.StringIO())),patch.object(cli.console,'input',return_value='quit'), \
             patch.object(terminal,'show_dashboard',return_value=view), \
             patch.object(terminal,'keyboard_pick',side_effect=['',[new['code']],'']) as pick, \
             patch.object(service,'delivery_issues',return_value=[]),patch.object(service,'selection_update') as update, \
             patch.object(cli,'choose_relay_model',return_value='claude-haiku-5-5') as relay, \
             patch.object(cli,'confirm_connection',return_value=False):
            runtime.write_json('ui.json',{'watched_sessions':[old],'selected_session':old})
            cli.interactive()
            relay.assert_not_called()
            cli.interactive()
            relay.assert_called_once()
            self.assertEqual(view['draft_checked_codes'],[new['code']])
            self.assertEqual(runtime.read_json('ui.json'),{'watched_sessions':[old],'selected_session':old})
            self.assertEqual(runtime.read_json('delivery.json',{}),{})
            self.assertEqual(pick.call_count,3)
            self.lock.assert_not_called()
            update.assert_not_called()

    def test_launch_starts_unchecked_and_enter_connects_only_new_checks(self):
        # 2026-11-19: four old, no-longer-listed checks were connected along with three new ones.
        old=[{'provider':'claude','session_id':f'old-{i}','code':f'C-OLD{i}','name':f'옛 작업 {i}'} for i in range(4)]
        new={'provider':'claude','session_id':'new','code':'C-NEW','name':'새 작업'}
        seen=[]
        def pick(console,current):
            seen.append(copy.deepcopy(current))
            return [new['code']] if len(seen)==1 else ''
        with tempfile.TemporaryDirectory() as folder,patch.object(runtime,'ROOT',Path(folder)), \
             patch.object(cli,'console',Console(file=io.StringIO())),patch.object(cli.console,'input',return_value='quit'), \
             patch.object(terminal,'show_dashboard',side_effect=lambda *a,**k:{'rows':[new],'watched':sessions.watched()}), \
             patch.object(terminal,'keyboard_pick',side_effect=pick), \
             patch.object(service,'delivery_issues',return_value=[]),patch.object(service,'selection_update'):
            runtime.write_json('delivery.json',{'model':'gpt-6-luna'})
            runtime.write_json('ui.json',{'watched_sessions':old})
            cli.interactive()
            self.assertEqual(sessions.watched(),[new])
        self.assertEqual(seen[0]['watched'],[])
        self.assertIn('연결 4개',seen[0]['action_notice'])
        self.assertEqual(seen[1]['watched'],[new])

    def test_confirmation_precedes_setup_changes_and_start_and_skips_an_existing_lock(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(runtime,'ROOT',Path(folder)), \
             patch.object(cli.sys.stdin,'isatty',return_value=True), \
             patch('talkturn.mouse_lock.active',return_value=False) as active, \
             patch.object(terminal,'choice_menu',return_value=None) as prompt, \
             patch.object(service,'pair') as pair,patch.object(service,'install_hooks') as hooks, \
             patch.object(service,'startup') as startup,patch.object(service,'start',return_value='연결됨') as start:
            setup_args=['--self-chat','Self','--computer-name','작업 PC','--relay-model','luna']
            self.assertIn('취소',cli.setup(setup_args))
            self.assertEqual(runtime.read_json('delivery.json',{}),{})
            self.assertIn('취소',cli.dispatch(['start']))
            for operation in (pair,hooks,startup,start,self.lock):
                operation.assert_not_called()
            _,title,rows=prompt.call_args.args
            self.assertIn('연결',title)
            self.assertIn('마우스와 터치패드',rows[0][1])
            self.assertIn('2.5초',rows[0][1])
            self.assertIn('연결이 종료',rows[0][1])
            self.assertIn('Esc 취소',prompt.call_args.kwargs['subtitle'])
            prompt.return_value=True
            self.assertIn('연결됨',cli.setup(setup_args))
            self.assertEqual(prompt.call_count,3)
            active.return_value=True
            self.assertEqual(cli.dispatch(['start']),'연결됨')
            self.assertEqual(prompt.call_count,3)
            self.assertEqual(start.call_count,2)

    def test_first_setup_uses_checked_tasks_for_connection_and_confirms_only_once(self):
        row={'provider':'codex','session_id':'original','code':'X-NEW','name':'새 작업'}
        with tempfile.TemporaryDirectory() as folder,patch.object(runtime,'ROOT',Path(folder)), \
             patch.object(cli.sys.stdin,'isatty',return_value=True), \
             patch.object(cli,'console',Console(file=io.StringIO())),patch.object(cli.console,'input',return_value='quit'), \
             patch.object(terminal,'show_dashboard',return_value={'rows':[row],'watched':[]}), \
             patch.object(terminal,'keyboard_pick',side_effect=[[row['code']],'']), \
             patch.object(service,'delivery_issues',side_effect=[['미연결'],[]]), \
             patch.object(service,'chat_windows',return_value=[{'title':'나'}]),patch('builtins.input',return_value='1'), \
             patch.object(cli,'ask_computer_name',return_value='작업 PC'),patch.object(cli,'choose_relay_model') as model, \
             patch.object(cli,'confirm_connection',return_value=True) as confirm,patch.object(service,'pair'), \
             patch.object(service,'install_hooks'),patch.object(service,'startup'), \
             patch.object(service,'start') as start,patch.object(service,'selection_update') as update:
            runtime.write_json('delivery.json',{'reply_mode':'manual'})
            def connect():
                self.assertEqual(sessions.watched(),[row])
                return '연결 완료'
            start.side_effect=connect
            cli.interactive()
            confirm.assert_called_once()
            start.assert_called_once()
            model.assert_not_called()
            self.lock.assert_not_called()
            update.assert_not_called()


if __name__=='__main__':
    unittest.main()
