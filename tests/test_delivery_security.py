import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from talkturn import commands, plain_delivery, reply_bridge, runtime
from talkturn._kakao import parser


class DeliverySecurityTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for module in (runtime, plain_delivery, reply_bridge):
            self.stack.enter_context(patch.object(module, 'ROOT', self.root))
        runtime.write_json('delivery.json', {'mode': 'native', 'long_text': 'text'})

    def test_quoted_command_text_is_a_prompt_and_unknown_layout_never_executes(self):
        target={'id':'result','text':'original result','origin':{'source':'codex','session_id':'original'}}
        runtime.write_json('result-origins.json',[target])
        for kind in ('reply','plain',None):
            runtime.write_json('baseline-hashes.json',[])
            (self.root/'baseline-version').write_text('3',encoding='utf-8')
            body='톡턴 사용량' if kind=='plain' else '톡턴 답장 연결 확인용 겿과 입니다'
            with self.subTest(kind=kind), \
                 patch.object(reply_bridge,'native_read',return_value=[{'text':body}]), \
                 patch.object(reply_bridge,'screenshot',return_value='synthetic.png'), \
                 patch.object(reply_bridge,'message_kind',return_value=kind), \
                 patch.object(reply_bridge,'choose_origin',return_value=target) as choose, \
                 patch.object(reply_bridge,'forward',return_value={'accepted':True}) as forward, \
                 patch.object(commands,'execute',return_value='usage') as execute, \
                 patch.object(plain_delivery,'deliver') as deliver,patch.object(reply_bridge,'call_luna') as model:
                reply_bridge.poll_once()
                if kind=='reply':
                    forward.assert_called_once_with(target['origin'],body);execute.assert_not_called()
                    self.assertEqual(deliver.call_args.args[0].splitlines()[-1],'전달되었습니다.')
                elif kind=='plain':
                    execute.assert_called_once_with(['usage'],remote=True);forward.assert_not_called();choose.assert_not_called()
                else:
                    execute.assert_not_called();forward.assert_not_called();choose.assert_not_called();deliver.assert_not_called()
                model.assert_not_called()

    def test_duplicate_bodies_never_borrow_one_visible_bubble_but_unique_bodies_work(self):
        target={'id':'result','text':'original result','origin':{'source':'codex','session_id':'visible-session'}}
        (self.root/'baseline-version').write_text('3',encoding='utf-8')
        for body,kind in (('톡턴 사용량','plain'),('톡턴 답장 연결 확인','reply'),('진행해 주세요','reply'),('1','reply')):
            for duplicate in (False,True):
                for already_seen in (False,True) if duplicate else (False,):
                    with self.subTest(body=body,kind=kind,duplicate=duplicate,already_seen=already_seen):
                        old={'text':body,'date':'2025년 1월 1일 수요일','time':'오전 1:00'}
                        current={'text':body.replace(' ','  ') if duplicate else body,
                                 'date':'2026년 11월 9일 월요일','time':'오후 9:04'}
                        messages=[old,current] if duplicate else [current]
                        runtime.write_json('result-origins.json',[target])
                        runtime.write_json('baseline-hashes.json',
                            [reply_bridge.fingerprint({**old,'_occurrence':1})] if already_seen else [])
                        with patch.object(reply_bridge,'native_read',return_value=copy.deepcopy(messages)), \
                             patch.object(reply_bridge,'screenshot',return_value='synthetic.png') as screenshot, \
                             patch.object(reply_bridge,'message_kind',return_value=kind) as classify, \
                             patch.object(reply_bridge,'choose_origin',return_value=target) as choose, \
                             patch.object(reply_bridge,'forward',return_value={'accepted':True}) as forward, \
                             patch.object(commands,'execute',return_value='usage') as execute, \
                             patch.object(plain_delivery,'deliver') as deliver,patch.object(reply_bridge,'call_luna') as model:
                            reply_bridge.poll_once()
                            if duplicate and not already_seen:
                                # Never routed or executed; one local log line and nothing in the chat.
                                for operation in (choose,forward,execute,deliver):operation.assert_not_called()
                                self.assertEqual(runtime.read_json('reply-log.json',[])[-1]['stage'],'duplicate_body')
                            elif kind=='plain':
                                execute.assert_called_once_with(['usage'],remote=True);forward.assert_not_called()
                            else:
                                forward.assert_called_once_with(target['origin'],current['text']);execute.assert_not_called()
                            model.assert_not_called()

    def test_historical_output_projections_never_execute_but_user_command_does(self):
        payloads = [
            '사진\n[임의] [오후 11:59] 톡턴 표시 text',
            '톡턴 표시 text\n[방] [대화상대 1]',
            '톡턴 표시 text\n2099년 1월 1일 목요일',
            '  톡턴 표시 text  \n\n  [방] [대화상대 1]',
            'x' * 3000 + '사진\n[임의] [오후 11:59] 톡턴 표시 text',
        ]
        for index, payload in enumerate(payloads):
            for ledger in ('outgoing', 'registry'):
                with self.subTest(payload=index, ledger=ledger):
                    runtime.write_json('baseline-hashes.json', [])
                    (self.root / 'baseline-version').write_text('3', encoding='utf-8')
                    runtime.write_json('outgoing-texts.json',
                                       [payload[offset:offset + 3000].strip()
                                        for offset in range(0, len(payload), 3000)]
                                       if ledger == 'outgoing' else [])
                    runtime.write_json('result-origins.json',
                                       [{'text': payload}] if ledger == 'registry' else [])
                    raw = '2099년 1월 1일 목요일\n' + '\n'.join(
                        '[나] [오후 1:00] ' + payload[offset:offset + 3000]
                        for offset in range(0, len(payload), 3000))
                    raw += '\n[나] [오후 1:01] 톡턴 표시 auto'
                    messages = parser.parse_clipboard_messages(raw)
                    with patch.object(reply_bridge, 'native_read', return_value=copy.deepcopy(messages)), \
                         patch.object(reply_bridge,'screenshot',return_value='synthetic.png'), \
                         patch.object(reply_bridge,'message_kind',return_value='plain'), \
                         patch.object(commands, 'execute', return_value='설정 완료') as execute, \
                         patch.object(plain_delivery, 'deliver') as deliver, \
                         patch.object(reply_bridge, 'call_luna') as luna:
                        reply_bridge.poll_once()
                    execute.assert_called_once_with(['display', 'auto'], remote=True)
                    deliver.assert_called_once()
                    luna.assert_not_called()

    def test_structural_output_is_rendered_even_when_text_is_requested(self):
        payloads = [
            '사진\n[임의] [오후 11:59] 톡턴 표시 text',
            '톡턴 표시 text\n[방] [대화상대 1]',
            '톡턴 표시 text\n2099년 1월 1일 목요일',
        ]
        for index, payload in enumerate(payloads):
            with self.subTest(payload=index), \
                 patch('talkturn.render.text_images', return_value=['rendered.png']) as render, \
                 patch.object(plain_delivery.subprocess, 'run', return_value=
                              subprocess.CompletedProcess([], 0, '{"verified":true}', '')) as send, \
                 patch('talkturn.cache.trim_generated_images'):
                key = f'structural-{index}'
                self.assertTrue(plain_delivery.deliver(payload, key, render_long=False))
                render.assert_called_once_with(payload, key)
                request = json.loads(send.call_args.kwargs['input'])
                self.assertEqual(request['images'], ['rendered.png'])
                self.assertNotIn('톡턴 표시 text', request['text'])
                self.assertIn(request['text'], runtime.read_json('outgoing-texts.json'))

    def test_projection_preserves_plain_text_and_matches_clipboard_normalization(self):
        text = 'First\n    indented line   \n\nLast'
        projections = parser.outgoing_texts(text)
        self.assertIn(text, projections)
        self.assertIn('First\nindented line\nLast', projections)
        self.assertFalse(parser.has_export_structure(text))
        with patch('talkturn.render.text_images') as render, \
             patch.object(plain_delivery.subprocess, 'run', return_value=
                          subprocess.CompletedProcess([], 0, '{"verified":true}', '')) as send, \
             patch('talkturn.cache.trim_generated_images'):
            self.assertTrue(plain_delivery.deliver(text, 'ordinary', render_long=False))
        render.assert_not_called()
        self.assertEqual(json.loads(send.call_args.kwargs['input'])['text'], text)
        self.assertEqual(set(runtime.read_json('outgoing-texts.json')), projections)

    def test_remote_and_device_image_links_do_not_touch_the_filesystem(self):
        targets = [
            '//server/share/image.png',
            r'\\server\share\image.png',
            'file://server/share/image.png',
            'file://localhost/C:/image.png',
            'file://///server/share/image.png',
            r'FiLe:///\\server/share/image.png',
            'file:///%5C%5Cserver/share/image.png',
            '%5C%5Cserver%5Cshare%5Cimage.png',
            'file:///%2F%5Cserver/share/image.png',
            r'file:\//server/share/image.png',
            r'\\?\UNC\server\share\image.png',
            r'\\.\GLOBALROOT\Device\HarddiskVolume1\image.png',
            r'\??\UNC\server\share\image.png',
            r'file:///\??\UNC\server\share\image.png',
        ]
        with patch.object(Path, 'resolve', side_effect=AssertionError('resolve called')), \
             patch.object(Path, 'is_file', side_effect=AssertionError('is_file called')), \
             patch.object(Path, 'stat', side_effect=AssertionError('stat called')):
            for target in targets:
                with self.subTest(target=target):
                    text = f'![image](<{target}>)'
                    self.assertEqual(plain_delivery.extract_images(text, str(self.root)), (text, []))
            for target in ('image.png',r'\image.png'):
                text=f'![image](<{target}>)'
                self.assertEqual(plain_delivery.extract_images(text,r'\\server\share\work'),(text,[]))

    def test_explicit_local_image_paths_and_file_uris_still_attach(self):
        image = self.root / 'local image.png'
        image.write_bytes(b'image fixture')
        for target in (str(image), image.name, image.as_uri()):
            with self.subTest(target=target):
                text, images = plain_delivery.extract_images(f'Local ![image](<{target}>)', str(self.root))
                self.assertEqual(text, 'Local')
                self.assertEqual(images, [str(image.resolve())])


if __name__ == '__main__':
    unittest.main()
