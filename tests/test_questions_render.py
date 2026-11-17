import copy
import json
import io
import os
from pathlib import Path
import tempfile
import time
import unittest
from contextlib import ExitStack,redirect_stdout
from unittest.mock import patch
from PIL import Image,ImageChops
from talkturn import runtime, questions, render, commands, plain_delivery,notify,reply_bridge


class QuestionsAndRenderTest(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack(); self.addCleanup(self.stack.close)
        self.root=Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for module in (runtime,questions,render,commands,plain_delivery,reply_bridge):
            self.stack.enter_context(patch.object(module,'ROOT',self.root))

    def save(self,record):
        runtime.write_json('questions.json',[record])

    def test_question_cards_require_the_exact_live_quote_and_keep_requests_independent(self):
        import subprocess
        session='question-session'
        payload={'hook_event_name':'PreToolUse','tool_name':'AskUserQuestion','session_id':session,
                 'tool_input':{'questions':[{'question':'색을 골라 주세요.','options':[{'label':'파랑'}]}]}}
        runtime.write_json('ui.json',{'watched_sessions':[{'provider':'claude','session_id':session}]})
        runtime.write_json('session-catalog.json',{'rows':[{'provider':'claude','session_id':session,'name':'질문 작업'}]})
        runtime.write_json('delivery.json',{'mode':'native','long_text':'image'})
        (self.root/'enabled').touch();(self.root/'reply-enabled').touch()
        with patch.object(plain_delivery.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'{"verified":true,"baseline_hashes":[]}','')) as send, \
             patch('talkturn.cache.trim_generated_images'),patch('talkturn.tasks.record'):
            questions.handle('claude',payload,wait=False)
            first_packet=json.loads(send.call_args.kwargs['input'])
            questions.handle('claude',payload,wait=False)
            first,second=runtime.read_json('questions.json')
            self.assertTrue(first_packet['text'].startswith('질문 '+first['id']+'\n[질문 작업]\n'))
            self.assertEqual(first_packet['text'].count('[질문 작업]'),1)
            self.assertFalse(first_packet['images'])
            self.assertEqual({item['origin']['request_id'] for item in plain_delivery.pending_replies()},{first['id'],second['id']})
            before=send.call_count
            questions.handle('claude',{'hook_event_name':'Notification','notification_type':'idle_prompt',
                                     'session_id':session,'message':'Claude is waiting for your input'},wait=False)
            self.assertEqual(send.call_count,before)
        body=self.root/'one-body.png';body.write_bytes(b'one isolated body')
        quote=self.root/'one-quote.png';quote.write_bytes(b'one isolated quote')
        isolated={'path':str(body),'body_path':str(body),'quoted_path':str(quote),'body':'1','quote_kind':'text'}
        candidates=plain_delivery.reply_candidates()
        def choose(identifier,local_id=None):
            model={'body':'1','quote':'질문 '+identifier+'\n[질문 작업]','quote_kind':'text'}
            ocr={'words':[{'line':0,'text':'질문'},{'line':0,'text':local_id or identifier}]}
            with patch.object(reply_bridge,'isolate_reply',return_value=isolated), \
                 patch.object(reply_bridge,'call_luna',return_value=model),patch('talkturn.reply_crop._ocr',return_value=ocr):
                return reply_bridge.choose_origin({'text':'1'},candidates,'unused-viewport.png')
        self.assertIsNone(choose(first['id'],second['id']))
        target=choose(first['id'])
        self.assertEqual(target['_quoted_request_id'],first['id'])
        with self.assertRaises(ValueError):reply_bridge.forward(target['origin'],'1')
        with self.assertRaises(ValueError):reply_bridge.forward(target['origin'],'1',quoted_request_id=second['id'])
        self.assertTrue(reply_bridge.forward(target['origin'],'1',quoted_request_id=first['id'])['accepted'])
        with self.assertRaises(ValueError):reply_bridge.forward(target['origin'],'1',quoted_request_id=first['id'])
        self.assertIsNone(choose(first['id']))
        self.assertEqual({item['origin']['request_id'] for item in plain_delivery.pending_replies()},{second['id']})
        with patch.object(reply_bridge,'native_read',return_value=[{'text':'톡턴 응답 '+second['id']+' 1'}]), \
             patch.object(reply_bridge,'screenshot',return_value='synthetic.png'), \
             patch.object(reply_bridge,'message_kind',return_value='plain'), \
             patch.object(commands,'execute') as execute,patch.object(plain_delivery,'deliver') as reply, \
             patch.object(reply_bridge,'call_luna') as model:
            reply_bridge.poll_once(require_enabled=True)
            execute.assert_not_called();model.assert_not_called()
            self.assertIn('해당 질문 말풍선',reply.call_args.args[0])
        self.assertEqual(next(item for item in questions.pending() if item['id']==second['id'])['status'],'waiting')
        records=runtime.read_json('questions.json');records[1]['expires_at']=0
        runtime.write_json('questions.json',records)
        self.assertIsNone(choose(second['id']))

        permission=questions.prepare('claude',{'hook_event_name':'PermissionRequest','session_id':session,
                                               'tool_name':'Bash','tool_input':{'command':'echo test'}})
        self.save(permission)
        origin={'source':'claude','session_id':session,'request_id':permission['id']}
        with self.assertRaises(ValueError):reply_bridge.forward(origin,'아마 승인',quoted_request_id=permission['id'])
        reply_bridge.forward(origin,'허용',quoted_request_id=permission['id'])
        self.assertEqual(runtime.read_json('questions.json')[0]['response']['hookSpecificOutput']['decision']['behavior'],'allow')
        hidden=questions.prepare('claude',{'hook_event_name':'PermissionRequest','session_id':session,
            'tool_name':'Write','tool_input':{'file_path':'x','content':'not fully displayed'}})
        self.save(hidden);origin['request_id']=hidden['id']
        with self.assertRaises(ValueError):reply_bridge.forward(origin,'승인',quoted_request_id=hidden['id'])
        reply_bridge.forward(origin,'거부',quoted_request_id=hidden['id'])
        self.assertEqual(runtime.read_json('questions.json')[0]['response']['hookSpecificOutput']['decision']['behavior'],'deny')
        codex=questions.prepare('codex',{'hook_event_name':'PreToolUse','tool_name':'request_user_input',
                                        'tool_input':{'questions':[{'question':'Continue?'}]}})
        self.save(codex)
        self.assertTrue(questions.describe(codex).startswith('알림 전용'))
        with self.assertRaises(ValueError):questions.answer(codex['id'],'yes')
        for kind in ('session_anchor','result'):
            with patch.object(reply_bridge,'executable',return_value='synthetic-codex.exe'), \
                 patch.object(reply_bridge.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'{}','')), \
                 patch.object(questions,'answer') as answer:
                reply_bridge.forward({'source':'codex','session_id':'ordinary','kind':kind},'승인')
                answer.assert_not_called()

    def test_result_images_repeat_heading_and_leave_controls_as_text(self):
        import subprocess
        origin={'source':'codex','session_id':'image-session','kind':'result'}
        runtime.write_json('ui.json',{'watched_sessions':[{'provider':'codex','session_id':'image-session'}]})
        runtime.write_json('session-catalog.json',{'rows':[{'provider':'codex','session_id':'image-session','name':'결과 이미지 작업'}]})
        runtime.write_json('delivery.json',{'mode':'native','long_text':'image'})
        extra=self.root/'original.png';Image.new('RGB',(40,40),'blue').save(extra)
        receipt=subprocess.CompletedProcess([],0,'{"verified":true,"baseline_hashes":[]}','')
        with patch.object(plain_delivery.subprocess,'run',return_value=receipt) as send, \
             patch.object(plain_delivery,'call_luna') as model, \
             patch('talkturn.cache.trim_generated_images'):
            plain_delivery.deliver('짧은 결과입니다.','image-short',origin=origin,images=[str(extra)])
            payload=json.loads(send.call_args.kwargs['input'])
            self.assertEqual(payload['text'],'');self.assertEqual(payload['images'][-1],str(extra))
            with Image.open(payload['images'][0]) as image:
                self.assertEqual(image.width,1080);self.assertLess(image.height,500)
            record=runtime.read_json('result-origins.json')[-1]
            self.assertIn('[결과 이미지 작업]',record['text']);self.assertIn('짧은 결과입니다.',record['text'])
            self.assertEqual(len(record['images']),2);self.assertTrue(record['awaiting_reply'])
            text='긴 결과의 원문을 그대로 보존합니다.\n'*80
            plain_delivery.deliver(text,'image-long',origin=origin)
            payload=json.loads(send.call_args.kwargs['input'])
            self.assertEqual(payload['text'],'');self.assertGreater(len(payload['images']),1)
            header=None
            for path in payload['images']:
                with Image.open(path) as page:
                    self.assertEqual(page.width,1080);self.assertLessEqual(page.height,1540)
                    current=page.crop((64,96,1016,192))
                    if header is None:header=current
                    else:self.assertIsNone(ImageChops.difference(header,current).getbbox())
            for index,(body,metadata) in enumerate((('talkturn in',None),('talkturn update',None),
                    ('Codex',None),('[세션 카드]',{**origin,'kind':'session_anchor'}),
                    ('[세션 카드]\n전달되었습니다.',None),('질문입니다.',{**origin,'kind':'question','request_id':'Q12345678'}))):
                plain_delivery.deliver(body,'text-control-'+str(index),origin=metadata,render_long=False)
                payload=json.loads(send.call_args.kwargs['input'])
                self.assertFalse(payload['images']);self.assertIn(body,payload['text'])
            for mode,body,expect_images in (('text',text,False),('auto','짧은 결과',False),('auto',text,True)):
                runtime.write_json('delivery.json',{'mode':'native','long_text':mode})
                plain_delivery.deliver(body,'choice-'+mode+str(expect_images),origin=origin)
                payload=json.loads(send.call_args.kwargs['input'])
                self.assertEqual(bool(payload['images']),expect_images)
                self.assertTrue(payload['text'])
            record=runtime.read_json('result-origins.json')[-1]
            crop=self.root/'reply-crop.png';crop.write_bytes(b'isolated synthetic reply')
            with patch.object(reply_bridge,'isolate_reply',return_value={'path':str(crop),'body':'reply'}), \
                 patch.object(reply_bridge,'call_luna',return_value={'body':'reply','quote':record['caption'],'quote_kind':'text'}):
                self.assertEqual(reply_bridge.choose_origin({'text':'reply'},[record],'unused-viewport.png'),record)
            runtime.write_json('delivery.json',{'mode':'native'})
            plain_delivery.deliver('기본 결과','default-image',origin=origin)
            self.assertEqual(json.loads(send.call_args.kwargs['input'])['text'],'')
            model.assert_not_called()
        (self.root/'enabled').touch()
        payload={'hook_event_name':'Stop','session_id':'image-session','last_assistant_message':'완료'}
        with patch.object(notify,'ROOT',self.root),patch.object(notify.sys,'argv',['notify','hook','--source','codex']), \
             patch.object(notify.sys,'stdin',io.StringIO(json.dumps(payload))), \
             patch('talkturn.tasks.record'),patch.object(plain_delivery,'deliver') as delivered,redirect_stdout(io.StringIO()):
            notify.main()
        self.assertEqual(delivered.call_args.kwargs['origin']['kind'],'result')

    def test_claude_answers_preserve_question_input_and_do_not_create_new_session(self):
        payload={'hook_event_name':'PreToolUse','tool_name':'AskUserQuestion','session_id':'same-session',
            'tool_input':{'questions':[{'question':'어느 색?','header':'색','options':[{'label':'파랑'},{'label':'노랑'}],'multiSelect':False}]}}
        record=questions.prepare('claude',payload)
        self.save(record)
        questions.answer(record['id'],'2')
        saved=runtime.read_json('questions.json')[0]
        output=saved['response']['hookSpecificOutput']
        self.assertEqual(output['updatedInput']['questions'],payload['tool_input']['questions'])
        self.assertEqual(output['updatedInput']['answers'],{'어느 색?':'노랑'})
        self.assertEqual(output['permissionDecision'],'allow')
        self.assertNotIn('answers',payload['tool_input'])
        with self.assertRaises(ValueError):
            questions.answer(record['id'],'1')

    def test_permission_needs_explicit_exact_answer_and_complete_action(self):
        record=questions.prepare('codex',{'hook_event_name':'PermissionRequest','tool_name':'Bash','tool_input':{'command':'echo test'}})
        self.save(record)
        with self.assertRaises(ValueError):
            questions.answer(record['id'],'아마 승인')
        questions.answer(record['id'],'승인')
        self.assertEqual(runtime.read_json('questions.json')[0]['response']['hookSpecificOutput']['decision'],{'behavior':'allow'})
        incomplete=questions.prepare('claude',{'hook_event_name':'PermissionRequest','tool_name':'Write','tool_input':{'file_path':'x','content':'hidden'}})
        self.save(incomplete)
        with self.assertRaises(ValueError):
            questions.answer(incomplete['id'],'승인')
        questions.answer(incomplete['id'],'거절')
        self.assertEqual(runtime.read_json('questions.json')[0]['response']['hookSpecificOutput']['decision']['behavior'],'deny')

    def test_expiration_never_approves_and_codex_question_is_notice_only(self):
        record=questions.prepare('claude',{'hook_event_name':'PermissionRequest','tool_input':{'command':'echo test'}})
        record['expires_at']=time.time()-1
        self.save(record)
        with self.assertRaises(ValueError):
            questions.answer(record['id'],'승인')
        codex=questions.prepare('codex',{'hook_event_name':'PreToolUse','tool_name':'request_user_input','tool_input':{'questions':[{'question':'진행?'}]}})
        self.save(codex)
        with self.assertRaises(ValueError):
            questions.answer(codex['id'],'네')
        self.assertIn('알림 전용',questions.describe(codex))

    def test_hook_wait_returns_only_explicit_response(self):
        (self.root/'enabled').touch(); (self.root/'reply-enabled').touch()
        runtime.write_json('ui.json',{'watched_sessions':[{'provider':'claude','session_id':'question-thread'}]})
        payload={'hook_event_name':'PermissionRequest','session_id':'question-thread','tool_name':'Bash','tool_input':{'command':'echo test'}}
        def deliver(*args,**kwargs):
            record=runtime.read_json('questions.json')[0]
            questions.answer(record['id'],'거절')
        with patch.object(plain_delivery,'deliver',side_effect=deliver):
            output=questions.handle('claude',payload)
        self.assertEqual(output['hookSpecificOutput']['decision']['behavior'],'deny')
        self.assertEqual(runtime.read_json('questions.json')[0]['status'],'returned_to_hook')

    def test_render_paginates_and_keeps_source_without_model(self):
        text='한글 긴 결과 · 비용과 사용량\n'+('원문은 요약하지 않고 그대로 표시합니다. '*15+'\n')*12
        files=render.text_images(text,'example')
        self.assertGreater(len(files),1)
        for file in files:
            with Image.open(file) as image:
                self.assertEqual(image.width,1080)
                self.assertLessEqual(image.height,1540)
        runtime.write_json('delivery.json',{'mode':'native','long_text':'auto'})
        import subprocess
        with patch.object(plain_delivery.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'{"verified":true}','')) as run, \
             patch.object(plain_delivery,'call_luna') as luna:
            plain_delivery.deliver(text,'render-event')
        payload=json.loads(run.call_args.kwargs['input'])
        self.assertGreater(len(payload['images']),1)
        self.assertIn('TalkTurn 결과',payload['text'])
        luna.assert_not_called()

    def test_manual_send_command_keeps_message_spacing(self):
        parts=commands.parse('톡턴 전달 T1234ABCD 줄  간격 $() 그대로',remote=True)
        self.assertEqual(parts,['send','T1234ABCD','줄  간격 $() 그대로'])

    def test_session_heading_precedes_text_and_rendered_result_without_model(self):
        import subprocess
        runtime.write_json('delivery.json',{'mode':'native','long_text':'auto'})
        runtime.write_json('session-catalog.json',{'rows':[
            {'provider':'codex','session_id':'heading-test','name':'TalkTurn 결과 정리'}]})
        origin={'source':'codex','session_id':'heading-test'}
        for index,body in enumerate(('수정 완료.', '원문 결과입니다. '*150)):
            with self.subTest(long=bool(index)), \
                 patch.object(plain_delivery.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'{"verified":true}','')) as send, \
                 patch.object(render,'text_images',wraps=render.text_images) as pages, \
                 patch.object(plain_delivery,'call_luna') as luna:
                plain_delivery.deliver(body,f'heading-{index}',origin=origin)
            payload=json.loads(send.call_args.kwargs['input'])
            self.assertTrue(payload['text'].startswith('[TalkTurn 결과 정리]\n컴퓨터: 내 PC\n\n'))
            if index:
                self.assertTrue(pages.call_args.args[0].startswith('[TalkTurn 결과 정리]\n컴퓨터: 내 PC\n\n'))
                self.assertTrue(payload['images'])
            else:
                self.assertEqual(payload['text'],'[TalkTurn 결과 정리]\n컴퓨터: 내 PC\n\n'+body)
                pages.assert_not_called()
            self.assertEqual(runtime.read_json('result-origins.json')[-1]['origin'],origin)
            luna.assert_not_called()


if __name__=='__main__':
    unittest.main()
