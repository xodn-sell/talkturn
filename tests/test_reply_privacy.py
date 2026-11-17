from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from talkturn import reply_bridge, runtime


class ReplyPrivacyTest(unittest.TestCase):
    def test_failed_isolation_and_viewport_fallback_never_call_a_model(self):
        target={'id':'result','text':'quoted result','origin':{'source':'codex','session_id':'session'}}
        with tempfile.TemporaryDirectory() as folder, patch.object(runtime,'ROOT',Path(folder)):
            viewport=Path(folder)/'viewport.png'; viewport.write_bytes(b'private neighboring messages')
            for isolated in (None,{'path':str(viewport),'body':'reply'}):
                with self.subTest(isolated=isolated), \
                     patch.object(reply_bridge,'isolate_reply',return_value=isolated), \
                     patch.object(reply_bridge,'call_luna') as classify:
                    self.assertIsNone(reply_bridge.choose_origin({'text':'reply'},[target],str(viewport)))
                    classify.assert_not_called()

    def test_model_receives_only_two_parts_of_one_reply_and_exact_body_is_required(self):
        target={'id':'result','text':'quoted result PRIVATE HISTORY',
                'images':['never-send-candidate-photo.png'],
                'origin':{'source':'codex','session_id':'private-session-id'}}
        with tempfile.TemporaryDirectory() as folder, patch.object(runtime,'ROOT',Path(folder)):
            crop=Path(folder)/'reply.png'; crop.write_bytes(b'only this reply')
            body=Path(folder)/'body.png'; body.write_bytes(b'only reply body')
            quoted=Path(folder)/'quoted.png'; quoted.write_bytes(b'only quoted source')
            isolated={'path':str(crop),'body':'첨부확인 ATTACH-1109','quote_thumbnail':None,
                      'body_path':str(body),'quoted_path':str(quoted)}
            for model_body,expected in (('첨부확인 ATTACH-1109',target),('점부확인 ATTACH-1109',None)):
                with self.subTest(model_body=model_body), \
                     patch.object(reply_bridge,'isolate_reply',return_value=isolated), \
                     patch.object(reply_bridge,'call_luna',return_value={
                         'body':model_body,'quote':'quoted result','quote_kind':'text'}) as classify:
                    self.assertEqual(reply_bridge.choose_origin({'text':'첨부확인 ATTACH-1109'},[target],'viewport.png'),expected)
                    prompt,schema,images=classify.call_args.args
                    self.assertEqual(images,[str(body.resolve()),str(quoted.resolve())])
                    self.assertEqual(set(schema['properties']),{'body','quote','quote_kind'})
                    self.assertLess(len(prompt),1400)
                    self.assertIn(json.dumps(isolated['body'],ensure_ascii=False),prompt)
                    for private in ('PRIVATE HISTORY','private-session-id','never-send-candidate-photo'):
                        self.assertNotIn(private,prompt)

    def test_photo_candidates_are_compared_locally_and_multiple_origins_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(runtime,'ROOT',Path(folder)):
            crop=Path(folder)/'reply.png'; crop.write_bytes(b'one reply')
            thumbnail=Path(folder)/'quote.png'; thumbnail.write_bytes(b'quoted thumbnail')
            target={'id':'first','text':'private result','images':['original.png'],
                    'origin':{'source':'codex','session_id':'first-session'}}
            other={**target,'id':'second','origin':{'source':'codex','session_id':'second-session'}}
            isolated={'path':str(crop),'body':'reply','quote_thumbnail':str(thumbnail)}
            with patch.object(reply_bridge,'isolate_reply',return_value=isolated), \
                 patch('talkturn.reply_photo.match_photos',return_value=['original.png']) as local_match, \
                 patch.object(reply_bridge,'call_luna',return_value={'body':'reply','quote':'','quote_kind':'image'}) as classify:
                self.assertIsNone(reply_bridge.choose_origin({'text':'reply'},[target,other],'viewport.png'))
            local_match.assert_called_once_with(str(thumbnail),['original.png'])
            self.assertEqual(classify.call_args.args[2],[str(crop.resolve())])

    def test_actual_text_quote_separators_match_without_changing_letters_or_routing_ambiguously(self):
        actual_quote=('너에게 답장\nTalkTurn 첨부 기능 테스트 : ATTACH-1109\n\n'
                      '01. 답장에 이미지와 파일을 붙일 수 있는지 확인하는 테스트 메시지입니다.\n'
                      '02. 답장에 이미지와 파일을 붙일 수 있는')
        source_quote=actual_quote.split('\n',1)[1]
        target={'id':'result','text':source_quote.replace(' : ',' · ')+'지 확인합니다.',
                'origin':{'source':'codex','session_id':'one'}}
        with tempfile.TemporaryDirectory() as folder, patch.object(runtime,'ROOT',Path(folder)):
            crop=Path(folder)/'reply.png'; crop.write_bytes(b'one reply')
            isolated={'path':str(crop),'body':'첨부확인 ATTACH-1109','quote_kind':'text'}
            with patch.object(reply_bridge,'isolate_reply',return_value=isolated):
                for quote,expected in ((source_quote,target),(source_quote.replace(':','：'),target),
                                       (actual_quote,None),(source_quote.replace('1109','1108'),None),
                                       (source_quote.replace('첨부','점부'),None)):
                    with self.subTest(quote=quote), patch.object(reply_bridge,'call_luna',return_value={
                            'body':isolated['body'],'quote':quote,'quote_kind':'text'}):
                        self.assertEqual(reply_bridge.choose_origin({'text':isolated['body']},[target],'viewport.png'),expected)
                other={**target,'id':'other','origin':{'source':'codex','session_id':'two'}}
                with patch.object(reply_bridge,'call_luna',return_value={
                        'body':isolated['body'],'quote':source_quote,'quote_kind':'text'}):
                    self.assertIsNone(reply_bridge.choose_origin({'text':isolated['body']},[target,other],'viewport.png'))


if __name__=='__main__':
    unittest.main()
