from contextlib import ExitStack
from pathlib import Path
import json
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock,patch

from talkturn import chat_events,commands,plain_delivery,questions,reply_bridge,runtime


class ChatEventsTest(unittest.TestCase):
    def test_verified_results_anchors_and_reply_ack_bound_local_reads(self):
        with ExitStack() as stack:
            root=Path(stack.enter_context(tempfile.TemporaryDirectory()))
            for module in (runtime,plain_delivery,reply_bridge,questions,commands):
                stack.enter_context(patch.object(module,'ROOT',root))
            runtime.write_json('delivery.json',{'mode':'native','long_text':'text','reply_enabled_at':1})
            runtime.write_json('connection.json',{'at':1})
            runtime.write_json('ui.json',{'watched_sessions':[{'provider':'codex','session_id':'A'},{'provider':'claude','session_id':'B'}]})
            (root/'reply-enabled').touch()
            source={'source':'codex','session_id':'A'}
            other={'source':'claude','session_id':'B'}
            baseline=['a'*64]; packets=[]; fail_ack=[False]
            def send(*args,**kwargs):
                payload=json.loads(kwargs['input']);packets.append(payload)
                if fail_ack[0] and payload['text'].endswith('전달되었습니다.'):
                    raise RuntimeError('Synthetic acknowledgement failure')
                return subprocess.CompletedProcess([],0,json.dumps({'verified':True,'baseline_hashes':baseline}), '')
            stack.enter_context(patch.object(plain_delivery.subprocess,'run',side_effect=send))
            stack.enter_context(patch('talkturn.cache.trim_generated_images'))
            stack.enter_context(patch('talkturn.result_format.session_heading',side_effect=lambda origin:'['+origin['session_id']+']' if origin else ''))
            read=stack.enter_context(patch.object(reply_bridge,'native_read',return_value=[]))
            model=stack.enter_context(patch.object(reply_bridge,'call_luna'))
            stack.enter_context(patch.object(reply_bridge,'screenshot',return_value='synthetic.png'))
            stack.enter_context(patch.object(reply_bridge,'message_kind',return_value='plain'))
            stack.enter_context(patch.object(reply_bridge,'recent_message',return_value=True))
            def pending_ids():return {item['id'] for item in plain_delivery.pending_replies()}
            def poll(text,target=None,forward=None):
                read.return_value=[{'text':text,'date':'','time':''}]
                def choose(message,candidates,*_):
                    if target:self.assertIn(target['id'],{item['id'] for item in candidates})
                    return target
                with patch.object(reply_bridge,'choose_origin',side_effect=choose), \
                     patch.object(reply_bridge,'forward',side_effect=forward or (lambda *_:{'accepted':True})) as dispatch:
                    result=reply_bridge.poll_once(require_enabled=True)
                    return result,dispatch.call_count

            self.assertTrue(reply_bridge.poll_once(require_enabled=True)['waiting_for_result'])
            plain_delivery.deliver('talkturn in','control')
            self.assertTrue(reply_bridge.poll_once(require_enabled=True)['waiting_for_result'])
            read.assert_not_called();model.assert_not_called()
            with patch.object(plain_delivery.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'{"verified":false}','')):
                with self.assertRaises(RuntimeError):plain_delivery.deliver('failed','failed',origin=source)
            self.assertFalse(pending_ids())

            plain_delivery.deliver('first','result-a1',origin=source)
            self.assertEqual(runtime.read_json('baseline-hashes.json'),['a'*64])
            baseline[:]=['b'*64]
            plain_delivery.deliver('latest','result-a2',origin=source)
            plain_delivery.deliver('other','result-b1',origin=other)
            self.assertEqual(pending_ids(),{'result-a2','result-b1'})
            self.assertNotIn('b'*64,runtime.read_json('baseline-hashes.json'))
            self.assertEqual(poll('ordinary or ambiguous')[1],0)
            self.assertEqual(pending_ids(),{'result-a2','result-b1'})
            target=next(item for item in plain_delivery.pending_replies() if item['id']=='result-a2')
            count=len(packets)
            def reject(*_):raise RuntimeError('Rejected by original session')
            with self.assertRaises(RuntimeError):poll('rejected',target,reject)
            # A lost reply sends nothing to the chat, neither "delivered" nor a failure notice; its reason is kept.
            self.assertEqual(len(packets),count);self.assertIn('result-a2',pending_ids())
            self.assertEqual(runtime.read_json('reply-log.json')[-1]['stage'],'forward_failed')

            def race(*_):
                plain_delivery.deliver('arrived during forwarding','result-a3',origin=source)
                return {'accepted':True}
            result,calls=poll('reply accepted',target,race)
            self.assertEqual(calls,1);self.assertTrue(result['receipts'][0]['ack_sent'])
            self.assertEqual(packets[-1],{'text':'[A]\n전달되었습니다.','images':[]})
            self.assertEqual(pending_ids(),{'result-a3','result-b1'})
            target=next(item for item in plain_delivery.pending_replies() if item['id']=='result-a3')
            fail_ack[0]=True
            result,calls=poll('accepted but ack fails',target)
            self.assertEqual(calls,1);self.assertFalse(result['receipts'][0]['ack_sent'])
            self.assertEqual(result['receipts'][0]['ack_error'],'RuntimeError')
            self.assertEqual(pending_ids(),{'result-b1'})
            # An acknowledgement failure cannot replay the already accepted user message.
            self.assertEqual(poll('accepted but ack fails',target)[1],0)
            fail_ack[0]=False
            plain_delivery.complete_reply(plain_delivery.pending_replies()[0])
            read.reset_mock()
            self.assertTrue(reply_bridge.poll_once(require_enabled=True)['waiting_for_result'])
            read.assert_not_called()

            question=questions.prepare('claude',{'hook_event_name':'PreToolUse','tool_name':'AskUserQuestion','session_id':'B',
                'tool_input':{'questions':[{'question':'Color?','options':[{'label':'Blue'}]}]}})
            runtime.write_json('questions.json',[question])
            origin={**other,'request_id':question['id'],'kind':'question_card'}
            plain_delivery.deliver(questions.describe(question),'question-id',origin=origin)
            count=len(packets)
            target={**plain_delivery.pending_replies()[0],'_quoted_request_id':question['id']}
            def answer_question(origin,text,*,quoted_request_id):
                self.assertEqual(quoted_request_id,question['id'])
                questions.answer(quoted_request_id,text)
                return {'accepted':True}
            poll('1',target,answer_question)
            self.assertEqual(len(packets),count+1)
            self.assertEqual(packets[-1]['text'],'[B]\n전달되었습니다.')
            self.assertFalse(pending_ids())
            self.assertEqual(runtime.read_json('questions.json')[0]['status'],'answered')

            runtime.write_json('ui.json',{'watched_sessions':[{'provider':'codex','session_id':'A'}]})
            anchor={**source,'kind':'session_anchor'}
            plain_delivery.deliver('anchor','anchor-a',origin=anchor)
            plain_delivery.deliver('new result','result-a4',origin=source)
            self.assertEqual(pending_ids(),{'anchor-a','result-a4'})
            target=next(item for item in plain_delivery.pending_replies() if item['id']=='anchor-a')
            for text in ('first anchor reply','second anchor reply'):
                result,calls=poll(text,target)
                self.assertEqual(calls,1);self.assertTrue(result['receipts'][0]['ack_sent'])
                self.assertIn('anchor-a',pending_ids())
            result_item=next(item for item in plain_delivery.pending_replies() if item['id']=='result-a4')
            for text in ('first image reply','second image reply'):
                result,calls=poll(text,result_item)
                self.assertEqual(calls,1);self.assertTrue(result['receipts'][0]['ack_sent'])
            self.assertEqual(pending_ids(),{'anchor-a'})
            candidate_ids=lambda:{item['id'] for item in plain_delivery.reply_candidates()}
            plain_delivery.deliver('anchor retitled','anchor-a2',origin=anchor)
            self.assertEqual(pending_ids(),{'anchor-a2'})
            self.assertLessEqual({'anchor-a','anchor-a2'},candidate_ids())  # the older card stays replyable
            plain_delivery.deliver('anchor retitled','anchor-a3',origin=anchor)
            self.assertNotIn('anchor-a2',candidate_ids())  # an unchanged card still replaces itself
            runtime.write_json('ui.json',{'watched_sessions':[]})
            self.assertFalse(pending_ids())
            self.assertFalse(plain_delivery.reply_candidates())
            read.reset_mock();self.assertEqual(poll('unselected image reply',result_item)[1],0);read.assert_not_called()
            runtime.write_json('ui.json',{'watched_sessions':[{'provider':'codex','session_id':'A'}]})
            runtime.write_json('connection.json',{'at':time.time()+1})
            self.assertFalse(pending_ids())
            read.reset_mock();reply_bridge.poll_once(require_enabled=True);read.assert_not_called()
            model.assert_not_called()
            with patch.object(plain_delivery,'deliver',return_value=True) as ack:
                reply_bridge._reply_ack(source,'specific')
                ack.assert_called_once_with('[A]\n전달되었습니다.','reply-ack-specific',render_long=False)
            runtime.write_json('connection.json',{'at':1})
            peer={'source':'claude','session_id':None,'target_kind':'claude_peer','peer_ref':'peer-1',
                  'name':'receiver-name','title':'Peer title','kind':'session_anchor'}
            runtime.write_json('ui.json',{'watched_sessions':[{**peer,'provider':'claude'}]})
            plain_delivery.deliver('[Peer title]','peer-anchor',origin=peer)
            self.assertEqual(packets[-1]['text'],'[Peer title]')
            self.assertIn('peer-anchor',pending_ids())
            with patch.object(reply_bridge.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'{}','')) as transport:
                self.assertTrue(reply_bridge.forward(peer,'reply')['accepted'])
                self.assertIn('talkturn.claude_transport',transport.call_args.args[0])
            with patch('talkturn.result_format.session_heading',return_value=''), \
                 patch.object(plain_delivery,'deliver',return_value=True) as ack:
                reply_bridge._reply_ack(peer,'peer-reply')
                ack.assert_called_once_with('[Peer title]\n전달되었습니다.','reply-ack-peer-reply',render_long=False)
            with patch.object(commands,'execute',return_value=''),patch.object(plain_delivery,'deliver') as empty:
                poll('톡턴 체크 none')
                empty.assert_not_called()
            runtime.write_json('result-origins.json',[])
            runtime.write_json('ui.json',{'watched_sessions':[{'provider':'codex','session_id':'A'},{'provider':'claude','session_id':'B'}]})
            (root/'baseline-hashes.json').unlink()
            (root/'baseline-version').unlink()
            baseline[:]=[]
            plain_delivery.deliver('first result in empty chat','empty-chat',origin=source)
            self.assertEqual(runtime.read_json('baseline-hashes.json'),[])
            target=plain_delivery.pending_replies()[0]
            def result_after_clear(*_):
                baseline[:]=['f'*64]
                plain_delivery.deliver('new result before poll ends','after-clear',origin=other)
                return {'ack_sent':True}
            with patch.object(reply_bridge,'_reply_ack',side_effect=result_after_clear):
                result,calls=poll('fast first reply',target)
            self.assertEqual(calls,1)
            self.assertEqual(pending_ids(),{'after-clear'})
            self.assertIn('f'*64,runtime.read_json('baseline-hashes.json'))

    def test_change_debounce_scope_and_own_operation_suppression(self):
        watch=chat_events.ChatEvents({'window_id':1,'process_id':10,'room_name':'paired'})
        watch.list_hwnd=2
        now=[1.0]
        with patch.object(watch,'_validate'),patch.object(chat_events.time,'monotonic',side_effect=lambda:now[0]), \
             patch.object(chat_events.win32gui,'IsChild',side_effect=lambda root,child:root==2 and child==3), \
             patch.object(chat_events.win32process,'GetWindowThreadProcessId',return_value=(0,10)), \
             patch.object(chat_events.ctypes.windll.kernel32,'GetTickCount',return_value=200), \
             patch.object(watch,'pump'):
            for event in (0x8005,0x8006,0x800A,0x800B,0x8014,0x8015):
                watch._on_event(None,event,2,-4,0,0,10)
            watch._on_event(None,0x8004,99,-4,0,0,10)
            self.assertFalse(watch.take_change());self.assertEqual(watch.event_count,0)
            watch._on_event(None,0x8004,3,-4,0,0,100)
            now[0]=1.5;self.assertFalse(watch.take_change())
            watch._on_event(None,0x8019,2,-4,0,0,110)
            now[0]=2.4;self.assertTrue(watch.take_change());self.assertFalse(watch.take_change())
            with watch.suppress():
                watch._on_event(None,0x800E,2,-4,0,0,150)
            watch._on_event(None,0x8004,2,-4,0,0,180)
            now[0]=4;self.assertFalse(watch.take_change())
            watch._on_event(None,0x800C,2,-4,0,0,220)
            now[0]=5;self.assertTrue(watch.take_change())

    def test_repaired_or_replaced_window_fails_closed(self):
        watch=chat_events.ChatEvents({'window_id':1,'process_id':10,'room_name':'paired'})
        watch.list_hwnd=2
        with patch.object(chat_events.win32gui,'IsWindow',return_value=True), \
             patch.object(chat_events.win32process,'GetWindowThreadProcessId',return_value=(0,11)), \
             patch.object(chat_events.pythoncom,'PumpWaitingMessages') as pump:
            with self.assertRaises(chat_events.ChatWindowChanged):watch.pump()
            pump.assert_not_called()

    def test_worker_polls_only_while_idle_enabled_and_still_paired(self):
        for scenario in ('idle','busy','disabled','repaired','replaced'):
            with self.subTest(scenario=scenario),ExitStack() as stack:
                root=Path(stack.enter_context(tempfile.TemporaryDirectory()))
                stack.enter_context(patch.object(reply_bridge,'ROOT',root))
                stack.enter_context(patch.object(runtime,'ROOT',root))
                if scenario!='disabled':(root/'reply-enabled').touch()
                runtime.write_json('delivery.json',{'window_id':1,'process_id':10,'room_name':'paired'})
                factory=stack.enter_context(patch.object(chat_events,'ChatEvents'))
                stack.enter_context(patch.object(chat_events.win32gui,'IsWindow',return_value=True))
                stack.enter_context(patch.object(chat_events.win32gui,'GetWindowText',return_value='paired'))
                stack.enter_context(patch.object(chat_events.win32process,'GetWindowThreadProcessId',
                                                return_value=(0,11 if scenario=='replaced' else 10)))
                kernel=Mock();kernel.CreateMutexW.return_value=1
                stack.enter_context(patch.object(reply_bridge.ctypes,'WinDLL',return_value=kernel))
                stack.enter_context(patch.object(reply_bridge.ctypes,'get_last_error',return_value=0))
                stack.enter_context(patch.object(reply_bridge.sys,'argv',['reply_bridge']))
                stack.enter_context(patch.object(reply_bridge,'idle_seconds',return_value=0 if scenario=='busy' else 20))
                poll_times=[]
                ticks=[0]
                def poll_once(**_):
                    poll_times.append(ticks[0]*5)
                    return {'new':1}
                poll=stack.enter_context(patch.object(reply_bridge,'poll_once',side_effect=poll_once))
                stack.enter_context(patch.object(plain_delivery,'pending_replies',return_value=[{}]))
                stack.enter_context(patch('talkturn.quota_alerts.maybe_prompt'))
                def sleep(seconds):
                    self.assertEqual(seconds,5)
                    ticks[0]+=1
                    if scenario=='repaired' and ticks[0]==1:
                        runtime.write_json('delivery.json',{'window_id':99})
                    if ticks[0]==3:(root/'reply-enabled').unlink()
                stack.enter_context(patch.object(reply_bridge.time,'sleep',side_effect=sleep))
                reply_bridge.main()
                self.assertEqual(poll_times,[0,5,10] if scenario=='idle' else [0] if scenario=='repaired' else [])
                for call in poll.call_args_list:self.assertEqual(call.kwargs,{'require_enabled':True})
                factory.assert_not_called()
                if scenario in ('repaired','replaced'):
                    self.assertEqual(runtime.read_json('reply-status.json')['error'],'ChatWindowChanged')
                self.assertEqual(runtime.read_json('reply-status.json')['mode'],'idle_local_poll')


if __name__=='__main__':
    unittest.main()
