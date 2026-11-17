import copy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from rich.console import Console
from talkturn import cli,runtime,sessions,terminal,account_menu,service


class InteractiveFlowTest(unittest.TestCase):
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
                 patch.object(service,'delivery_issues',side_effect=([['missing'],['missing']] if state=='cancelled' else [['missing'],[]] if state=='first' else [[]])) as issues, \
                 patch.object(cli,'setup',return_value='설정 취소.' if state=='cancelled' else '연결 완료') as setup:
                runtime.write_json('delivery.json',{'model':'gpt-6-luna'})
                cli.interactive()
                view=load.return_value
                self.assertEqual(len(sessions.watched()),0 if state=='empty' else 1)
                if state in ('first','cancelled'):
                    setup.assert_called_once_with(['--relay-model','gpt-6-luna'])
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
                        self.assertEqual(sessions.watched(),[row])
                        self.assertNotIn('action_notice',current)
                        self.assertEqual(setup.call_count,1 if first=='first-setup' else 0)
                        return [row['code']]  # The central alert's Enter retries the preserved checks.
                    return ''
                issues=([['나와의 채팅 미연결'],['나와의 채팅 미연결']] if first=='first-setup' else [[first]])
                issues += [['카톡 창이 바뀜 · setup 필요'],[]]
                results=(['나와의 채팅 창이 없습니다. 별도 창을 연 뒤 setup을 다시 실행하세요.']
                         if first=='first-setup' else [])+['카톡 연결 완료']
                with patch.object(runtime,'ROOT',Path(folder)), \
                     patch.object(cli,'console',Console(file=io.StringIO())), \
                     patch.object(terminal,'show_dashboard',return_value=view), \
                     patch.object(terminal,'keyboard_pick',side_effect=pick), \
                     patch.object(cli.console,'input',return_value='quit'), \
                     patch.object(service,'delivery_issues',side_effect=issues), \
                     patch.object(cli,'setup',side_effect=results) as setup:
                    runtime.write_json('delivery.json',{'model':'gpt-6-luna'})
                    cli.interactive()
                    self.assertEqual(sessions.watched(),[row])
                self.assertEqual(len(snapshots),3)
                self.assertIn('카톡 연결됨',snapshots[-1]['action_notice'])
                self.assertNotIn('connection_alert',snapshots[-1])
                self.assertEqual(setup.call_count,2 if first=='first-setup' else 1)


if __name__=='__main__':
    unittest.main()
