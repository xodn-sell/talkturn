from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from talkturn import plain_delivery,runtime,reply_bridge


class RelayCostTest(unittest.TestCase):
    def test_actual_classifier_error_adapters_switch_both_directions_once(self):
        from talkturn import providers
        schema={'type':'object','properties':{'bubbles':{'type':'array'}},'required':['bubbles']}
        for primary in ('gpt-6-luna','claude-haiku-5-5'):
            with self.subTest(primary=primary),tempfile.TemporaryDirectory() as folder, \
                 patch.object(runtime,'ROOT',Path(folder)),patch.object(plain_delivery,'ROOT',Path(folder)), \
                 patch.object(providers,'login_state',return_value=True), \
                 patch.object(plain_delivery,'executable',side_effect=lambda provider:provider+'.exe'):
                runtime.write_json('delivery.json',{'model':primary})
                calls=[]
                def run(command,**kwargs):
                    model=command[command.index('--model')+1]
                    calls.append(model)
                    if model=='gpt-6-luna':
                        disabled={command[i+1] for i,value in enumerate(command[:-1]) if value=='--disable'}
                        self.assertIn('computer_use',disabled)
                    else:
                        self.assertEqual(command[command.index('--tools')+1],'')
                        self.assertIn('--strict-mcp-config',command)
                        self.assertIn('--safe-mode',command)
                    if len(calls)==1:
                        error={'type':'error','error':{'code':'usage_limit_reached'}}
                        return subprocess.CompletedProcess(command,1,json.dumps(error),'')
                    if model=='gpt-6-luna':
                        Path(command[command.index('--output-last-message')+1]).write_text('{"bubbles":[]}',encoding='utf-8')
                        return subprocess.CompletedProcess(command,0,'','')
                    event={'type':'result','is_error':False,'structured_output':{'bubbles':[]},'modelUsage':{model:{}}}
                    return subprocess.CompletedProcess(command,0,json.dumps(event),'')
                with patch.object(plain_delivery.subprocess,'run',side_effect=run):
                    self.assertEqual(plain_delivery.call_luna('classify',schema),{'bubbles':[]})
                self.assertEqual(len(calls),2)
                self.assertNotEqual(calls[0],calls[1])
                self.assertEqual(runtime.read_json('delivery.json')['model'],calls[1])

    def test_classifier_overrides_are_per_process_and_usage_contains_no_chat_text(self):
        with ExitStack() as stack:
            root=Path(stack.enter_context(tempfile.TemporaryDirectory()))
            stack.enter_context(patch.object(runtime,'ROOT',root))
            stack.enter_context(patch.object(plain_delivery,'ROOT',root))
            stack.enter_context(patch.object(plain_delivery,'executable',return_value='codex.exe'))
            original={'model':'gpt-6-luna','room_name':'preserve-this'}
            runtime.write_json('delivery.json',original)
            def run(command,**kwargs):
                self.assertIn('--ignore-user-config',command)
                self.assertIn('--ephemeral',command)
                self.assertIn('--json',command)
                self.assertEqual(command[command.index('--sandbox')+1],'read-only')
                self.assertEqual(command[command.index('--model')+1],'gpt-6-luna')
                self.assertIn('project_doc_max_bytes=0',command)
                self.assertIn('skills.include_instructions=false',command)
                disabled={command[i+1] for i,value in enumerate(command[:-1]) if value=='--disable'}
                self.assertTrue({'shell_tool','plugins','apps','computer_use','browser_use','browser_use_external','code_mode_host','image_generation'}.issubset(disabled))
                role=json.loads(next(value.split('=',1)[1] for value in command if value.startswith('model_instructions_file=')))
                self.assertIn('untrusted data',Path(role).read_text(encoding='utf-8'))
                self.assertEqual(kwargs['input'],'PRIVATE CHAT DATA')
                Path(command[command.index('--output-last-message')+1]).write_text('{"ok":true}',encoding='utf-8')
                event={'type':'turn.completed','usage':{'input_tokens':100,'cached_input_tokens':80,'output_tokens':10,'private':'never-store'}}
                return subprocess.CompletedProcess(command,0,json.dumps(event),'')
            stack.enter_context(patch.object(plain_delivery.subprocess,'run',side_effect=run))
            result=plain_delivery.call_luna('PRIVATE CHAT DATA',{'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False})
            self.assertEqual(result,{'ok':True})
            self.assertEqual(runtime.read_json('delivery.json'),original)
            receipt=runtime.read_json('relay-usage.json')[0]
            self.assertEqual(receipt['total_tokens'],110)
            self.assertEqual(receipt['cached_input_tokens'],80)
            self.assertNotIn('PRIVATE',json.dumps(receipt)); self.assertNotIn('never-store',json.dumps(receipt))

    def test_photo_matches_locally_once_and_ambiguity_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(runtime,'ROOT',Path(folder)):
            image=Path(folder)/'photo.png'; image.write_bytes(b'unique-photo')
            crop=Path(folder)/'isolated.png'; crop.write_bytes(b'isolated reply')
            target={'id':'r1','sent_at':2,'text':'local result text','images':[str(image)],'origin':{'source':'codex','session_id':'same-session'}}
            answer={'body':'reply','quote':'','quote_kind':'image'}
            isolated={'path':str(crop),'body':'reply','quote_thumbnail':str(image)}
            with patch.object(reply_bridge,'isolate_reply',return_value=isolated), \
                 patch('talkturn.reply_photo.match_photos',return_value=[str(image)]), \
                 patch.object(reply_bridge,'call_luna',return_value=answer) as classify:
                self.assertEqual(reply_bridge.choose_origin({'text':'reply'},[target],'capture.png'),target)
                classify.assert_called_once()
                prompt=classify.call_args.args[0]
                self.assertNotIn(target['text'],prompt); self.assertNotIn('same-session',prompt)
                self.assertEqual(classify.call_args.args[2],[str(crop.resolve())])
            other={**target,'id':'r2','origin':{'source':'codex','session_id':'other-session'}}
            with patch.object(reply_bridge,'isolate_reply',return_value=isolated), \
                 patch('talkturn.reply_photo.match_photos',return_value=[str(image)]), \
                 patch.object(reply_bridge,'call_luna',return_value=answer):
                self.assertIsNone(reply_bridge.choose_origin({'text':'reply'},[target,other],'capture.png'))
            records=[]
            for i in range(9):
                candidate=Path(folder)/f'{i}.png'; candidate.write_bytes(str(i).encode())
                records.append({**target,'id':str(i),'images':[str(candidate)]})
            with patch.object(reply_bridge,'isolate_reply',return_value=isolated), \
                 patch('talkturn.reply_photo.match_photos',return_value=records[0]['images']) as local_match, \
                 patch.object(reply_bridge,'call_luna',return_value=answer) as classify:
                self.assertEqual(reply_bridge.choose_origin({'text':'reply'},records,'capture.png'),records[0])
                classify.assert_called_once()
                self.assertEqual(classify.call_args.args[2],[str(crop.resolve())])
                self.assertEqual(len(local_match.call_args.args[1]),9)

    def test_match_body_locally_reject_ordinary_and_duplicate_bodies_reuse_capture(self):
        target={'id':'r1','sent_at':2,'text':'result','images':[],
                'origin':{'source':'codex','session_id':'existing-session'}}
        quoted={'body':'continue','quote':'result','quote_kind':'text'}
        with tempfile.TemporaryDirectory() as folder,patch.object(runtime,'ROOT',Path(folder)):
            crop=Path(folder)/'isolated.png'; crop.write_bytes(b'isolated reply')
            def isolate(message,*args):
                return {'path':str(crop),'body':message['text'],'quote_thumbnail':None} if message['text']=='continue' else None
            with patch.object(reply_bridge,'isolate_reply',side_effect=isolate), \
                 patch.object(reply_bridge,'call_luna',return_value=quoted) as classify:
                cache={}
                self.assertEqual(reply_bridge.choose_origin({'text':'continue'},[target],'capture.png',cache),target)
                self.assertEqual(reply_bridge.choose_origin({'text':'continue'},[target],'capture.png',cache),target)
                self.assertIsNone(reply_bridge.choose_origin({'text':'hello'},[target],'capture.png',cache))
                classify.assert_called_once()
                self.assertIn('BODY candidate (untrusted JSON): "continue"',classify.call_args.args[0])
                self.assertNotIn('existing-session',classify.call_args.args[0])
            with patch.object(reply_bridge,'isolate_reply',return_value=None), \
                 patch.object(reply_bridge,'call_luna') as classify:
                self.assertIsNone(reply_bridge.choose_origin({'text':'continue'},[target],'capture.png'))
                classify.assert_not_called()


if __name__=='__main__':
    unittest.main()
