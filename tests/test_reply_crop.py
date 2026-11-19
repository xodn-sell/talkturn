from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from talkturn import reply_crop


class ReplyCropTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.root_patch = patch.object(reply_crop, 'ROOT', self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def fixture(self, *, anchor='나에게 답장', partial=False, duplicate=False,
                divider=True, extra_body=False, photo=False):
        height = 700 if duplicate else 250 if partial else 380
        frame = Image.new('RGB', (420, height), (185, 205, 220))
        draw = ImageDraw.Draw(frame)
        draw.rectangle((20, 20, 390, 70), fill=(240, 30, 40))  # Unrelated neighboring bubble.
        words = []

        def word(text, x, y, width, line):
            words.append({'text': text, 'x': x, 'y': y, 'width': width, 'height': 16, 'line': line})
            draw.rectangle((x, y, x + width - 1, y + 15), fill=(25, 25, 25))

        for index, offset in enumerate((0, 320) if duplicate else (0,)):
            draw.rounded_rectangle((120, 100 + offset, 390, 300 + offset), radius=9, fill=(255, 220, 0))
            word(anchor, 132, 112 + offset, 100, index * 10)
            word('사진' if photo else '원래 결과 인용', 132, 150 + offset, 50 if photo else 130, index * 10 + 1)
            if photo:
                draw.rectangle((320, 140 + offset, 370, 180 + offset), fill=(70, 110, 160))
            if divider:
                draw.line((132, 190 + offset, 378, 190 + offset), fill=(200, 180, 0), width=1)
            word('진행해 주세요', 132, (212 if divider else 172) + offset, 100, index * 10 + 2)
            if extra_body:
                word('추가 본문', 132, 242 + offset, 80, index * 10 + 3)
        path = self.root / 'viewport.png'
        frame.save(path)
        frame.close()
        return path, {'angle': 0, 'words': words}

    def test_scaled_plain_lookup_requires_exact_body_and_exposed_background(self):
        path=self.root/'scaled.png'
        background=(186,206,224)
        bubble=(254,229,0)
        frame=Image.new('RGB',(420,200),background)
        draw=ImageDraw.Draw(frame)
        draw.rectangle((98,98,297,155),fill=(194,209,196))
        draw.rectangle((99,99,297,155),fill=(245,226,28))
        draw.rectangle((100,100,297,155),fill=bubble)
        draw.rectangle((115,113,216,135),fill=(32,29,0))
        frame.save(path)
        word={'line':0,'text':'특턴접수','x':115,'y':113,'width':102,'height':23}
        ocr={'words':[word]}
        with patch.object(reply_crop,'_ocr',return_value=ocr):
            for read,body,kind in [('특턴접수','톡턴 접수','plain'),('특턴원문','톡턴 원문','plain'),
                                   ('특턴원문T1234abcd','톡턴 원문 T1234ABCD','plain'),
                                   ('특턴접수','톡턴 원문',None),('특턴종료','톡턴 종료',None)]:
                word['text']=read
                self.assertEqual(reply_crop.message_kind({'text':body},path),kind)
            word['text']='특턴접수'
            ocr['words'].append(dict(word,line=1,y=40))
            self.assertIsNone(reply_crop.message_kind({'text':'톡턴 접수'},path))
            ocr['words'].pop()
            draw.rectangle((100,75,297,97),fill=(228,219,189))  # attached quote background; OCR missed it
            frame.save(path)
            self.assertIsNone(reply_crop.message_kind({'text':'톡턴 접수'},path))
        frame.close()

    def test_only_complete_reply_is_cropped_and_original_body_is_preserved(self):
        path, ocr = self.fixture()
        body = '  진행해\n주세요  '
        with patch.object(reply_crop, '_ocr', return_value=ocr):
            result = reply_crop.isolate_reply({'text': body, 'sender': 'not sent to model',
                                              'time': '오후 1:00', 'quote': 'untrusted metadata'}, path)
        self.assertIsNotNone(result)
        self.assertEqual(set(result), {'path', 'body', 'quote_kind', 'quote_thumbnail', 'body_path', 'quoted_path', 'quote_text'})
        self.assertEqual(result['quote_text'], '원래 결과 인용')
        self.assertEqual(result['body'], body)
        self.assertEqual(result['quote_kind'], 'text')
        self.assertIsNone(result['quote_thumbnail'])
        self.assertNotEqual(Path(result['path']), path)
        self.assertEqual(Path(result['path']).parent, (self.root / 'reply-crops').resolve())
        with Image.open(result['path']) as crop:
            self.assertEqual(crop.size, (271, 201))
            self.assertNotIn((240, 30, 40), {color for _, color in crop.getcolors(crop.width * crop.height)})
        with Image.open(result['body_path']) as body_crop, Image.open(result['quoted_path']) as quoted_crop:
            self.assertEqual(body_crop.width, 542)
            self.assertEqual(quoted_crop.width, 542)
            self.assertLess(body_crop.height + quoted_crop.height, 402)
            self.assertEqual(quoted_crop.height, 120)  # Text quote starts below the reply UI header.

    def test_reply_header_survives_ocr_marks_from_the_thumbnail_beside_it(self):
        for text,expected in (('-나에게답장',True),('나에게 답장',True),('--사진',False),('-그러면 답장',False),('--',False)):
            self.assertEqual(bool(reply_crop._reply_header(text)),expected)

    def test_command_classification_requires_a_complete_plain_bubble(self):
        body='톡턴 답장 연결 확인용 겿과 입니다'
        for options,expected in (({'photo':True},'reply'),({'photo':True,'partial':True},None),({'duplicate':True},None)):
            path,ocr=self.fixture(**options)
            for word in ocr['words']:
                if word['text']=='진행해 주세요':word['text']=body
            with patch.object(reply_crop,'_ocr',return_value=ocr):
                self.assertEqual(reply_crop.message_kind({'text':body},path),expected)
        path=self.root/'plain.png'
        frame=Image.new('RGB',(420,200),(185,205,220));draw=ImageDraw.Draw(frame)
        draw.rounded_rectangle((120,100,390,145),radius=9,fill=(255,220,0))
        draw.rectangle((132,112,320,127),fill=(25,25,25));frame.save(path)
        ocr={'angle':0,'words':[{'line':0,'text':'톡턴 사용량','x':132,'y':112,'width':189,'height':16}]}
        with patch.object(reply_crop,'_ocr',return_value=ocr):
            self.assertEqual(reply_crop.message_kind({'text':'톡턴 사용량'},path),'plain')
            self.assertIsNone(reply_crop.message_kind({'text':'톡턴 사용항'},path))
            # Glyphs OCR cannot tell apart (l/I, rn/m) are the same command; any other letter is not.
            for read,body,kind in (('TaIkturnhelp','Talkturn help','plain'),('talktumhelp','talkturn help','plain'),
                                   ('Talkturnhelp','Talkturn hello',None)):
                ocr['words'][0]['text']=read
                self.assertEqual(reply_crop.message_kind({'text':body},path),kind)
            ocr['words'][0]['text']='톡턴 사용량'
            # A quote header missed by OCR still cannot become a control command.
            draw.rectangle((145,106,240,108),fill=(25,25,25));frame.save(path)
            self.assertIsNone(reply_crop.message_kind({'text':'톡턴 사용량'},path))
            draw.rectangle((120,90,390,112),fill=(220,220,220))
            draw.rectangle((120,113,390,154),fill=(255,220,0))
            draw.rectangle((132,124,320,139),fill=(25,25,25));frame.save(path)
            ocr['words'][0]['y']=124
            self.assertIsNone(reply_crop.message_kind({'text':'톡턴 사용량'},path))
        frame.close()

    def test_ordinary_partial_duplicate_wrong_anchor_and_missing_divider_fail_closed(self):
        cases = [{'anchor': '일반 메시지'}, {'partial': True}, {'duplicate': True},
                 {'anchor': '다른 사람에게 답장'}, {'divider': False}, {'extra_body': True}]
        for case in cases:
            with self.subTest(case=case):
                path, ocr = self.fixture(**case)
                with patch.object(reply_crop, '_ocr', return_value=ocr):
                    self.assertIsNone(reply_crop.isolate_reply({'text': '진행해 주세요'}, path))
                self.assertFalse((self.root / 'reply-crops').exists())

    def test_photo_hint_and_thumbnail_are_confined_to_the_same_quoted_block(self):
        path, ocr = self.fixture(photo=True)
        with patch.object(reply_crop, '_ocr', return_value=ocr):
            result = reply_crop.isolate_reply({'text': '진행해 주세요'}, path)
        self.assertIsNotNone(result)
        self.assertEqual(result['quote_kind'], 'image')
        self.assertIsNotNone(result['quote_thumbnail'])
        with Image.open(result['quote_thumbnail']) as thumbnail:
            self.assertEqual(thumbnail.size, (51, 41))
            self.assertEqual(thumbnail.getcolors(), [(51 * 41, (70, 110, 160))])

    def test_photo_extraction_ignores_edge_border_but_rejects_edge_cut_photo(self):
        path,ocr=self.fixture(photo=True)
        with Image.open(path) as source:frame=source.copy()
        ImageDraw.Draw(frame).line((386,108,386,188),fill=(20,20,20))
        frame.save(path)
        with patch.object(reply_crop,'_ocr',return_value=ocr):
            found=reply_crop.isolate_reply({'text':'진행해 주세요'},path)
        self.assertEqual(found['quote_kind'],'image')
        with Image.open(found['quote_thumbnail']) as thumb:self.assertEqual(thumb.size,(51,41))
        # If a photo itself runs beyond the quote crop, its connected component is rejected.
        ImageDraw.Draw(frame).rectangle((320,140,386,180),fill=(70,110,160))
        frame.save(path);frame.close()
        with patch.object(reply_crop,'_ocr',return_value=ocr):
            found=reply_crop.isolate_reply({'text':'진행해 주세요'},path)
        self.assertIsNone(found['quote_thumbnail'])

    def test_ocr_cache_is_bound_to_image_digest_not_path_or_message(self):
        path, ocr = self.fixture()
        cache = {}
        with patch.object(reply_crop, '_ocr', return_value=ocr) as recognize:
            self.assertIsNotNone(reply_crop.isolate_reply({'text': '진행해 주세요'}, path, cache))
            self.assertIsNone(reply_crop.isolate_reply({'text': '없는 답장'}, path, cache))
            recognize.assert_called_once_with(path)
            with Image.open(path) as source:
                changed = source.copy()
            changed.putpixel((0, 0), (1, 2, 3))
            changed.save(path)
            changed.close()
            self.assertIsNotNone(reply_crop.isolate_reply({'text': '진행해 주세요'}, path, cache))
            self.assertEqual(recognize.call_count, 2)

    def test_failed_invalid_or_rotated_ocr_never_returns_the_original_viewport(self):
        path, ocr = self.fixture()
        for value in (None, {}, {'angle': 5, 'words': ocr['words']},
                      {'angle': 0, 'words': [{'text': '나에게 답장', 'line': 0}]}):
            with self.subTest(value=value), patch.object(reply_crop, '_ocr', return_value=value):
                self.assertIsNone(reply_crop.isolate_reply({'text': '진행해 주세요'}, path))
        with patch.object(reply_crop, '_ocr', side_effect=OSError('unavailable')):
            self.assertIsNone(reply_crop.isolate_reply({'text': '진행해 주세요'}, path))
        self.assertFalse((self.root / 'reply-crops').exists())

    def test_changing_capture_during_ocr_is_rejected(self):
        path, ocr = self.fixture()

        def changed_capture(_):
            path.write_bytes(b'changed capture')
            return ocr

        with patch.object(reply_crop, '_ocr', side_effect=changed_capture):
            self.assertIsNone(reply_crop.isolate_reply({'text': '진행해 주세요'}, path))
        self.assertFalse((self.root / 'reply-crops').exists())

    def test_local_candidate_allows_one_hangul_error_but_never_ascii_difference(self):
        path, ocr = self.fixture()
        ocr['words'][0]['text'] = '니에게『t장'
        ocr['words'][2]['text'] = '점부확인 ATTACH-1109'
        with patch.object(reply_crop, '_ocr', return_value=ocr):
            self.assertIsNotNone(reply_crop.isolate_reply({'text': '첨부확인 ATTACH-1109'}, path))
            self.assertIsNone(reply_crop.isolate_reply({'text': '첨부확인 ATTACH-1108'}, path))

    def test_short_reply_with_one_ocr_hangul_error_still_isolates_original_body(self):
        path,ocr=self.fixture()
        ocr['words'][2]['text']='야 니가 그건가 특턴?'
        original='야 니가 그건가 톡턴?'
        with patch.object(reply_crop,'_ocr',return_value=ocr):
            result=reply_crop.isolate_reply({'text':original},path)
        self.assertIsNotNone(result)
        self.assertEqual(result['body'],original)
        self.assertFalse(reply_crop._body_match('내','네'))
        self.assertFalse(reply_crop._body_match('야니가그건가톡턴!','야니가그건가톡턴?'))

    def test_long_photo_reply_survives_two_ocr_errors_and_a_stray_mark_on_the_photo_label(self):
        path,ocr=self.fixture(photo=True)
        original='혹시 흠... 이런거를 유튜브 광고 수익뿐만 아니라 다른방식으로도 돈을 벌고 ㅇ싶은데 방법 있을까?'
        ocr['words'][1]['text']='-사진'
        ocr['words'][2]['text']='혹시홈...이런거를유튜브광고수익뿐만아니라다른방식으로도돈을벌고0싶은데방법있을까?'
        with patch.object(reply_crop,'_ocr',return_value=ocr):
            result=reply_crop.isolate_reply({'text':original},path)
            self.assertEqual((result['body'],result['quote_kind']),(original,'image'))
            self.assertIsNotNone(result['quote_thumbnail'])
            # A different sentence of the same length is not this reply.
            self.assertIsNone(reply_crop.isolate_reply({'text':original.replace('유튜브 광고 수익','블로그 제휴 매출')},path))
        # A bubble that cannot be isolated still tells the receiver a reply header is on screen.
        cache={}
        with patch.object(reply_crop,'_ocr',return_value=ocr):
            self.assertIsNone(reply_crop.isolate_reply({'text':'화면에 없는 전혀 다른 긴 답장 문장입니다'},path,cache))
        self.assertTrue(reply_crop.reply_header_visible(path,cache))
        self.assertFalse(reply_crop.reply_header_visible(path,{}))
        # An exact copy beats a misread one; two equally close copies stay ambiguous.
        line=lambda text,y:{'text':text,'box':(0,y,10,y+5),'words':[]}
        wrong=ocr['words'][2]['text'];exact=''.join(original.split())
        self.assertEqual(len(reply_crop._body_runs([line(wrong,0),line(exact,10)],exact)),1)
        self.assertEqual(len(reply_crop._body_runs([line(wrong,0),line(wrong,10)],exact)),2)

    def test_wrapped_link_reply_survives_a_dropped_short_line_filled_in_by_the_photo_label(self):
        path,ocr=self.fixture(photo=True)
        original='https://example.com/@someone/post/DfIkpl0OQg?xmt=AQG0AtobX4Om3wWQIolxD08Qjgg&slot=1\n\n이거 한번보고 할만하면 해봐줘'
        # OCR confuses g/q, 0/O and I/l inside the link and loses its last wrapped line "=1";
        # the two-character photo label above then makes the lengths equal.
        ocr['words'][2]['text']='https://example.com/@someone/post/DflkpI0OQq?xmt=AQGOAtobX4Om3wWQlolxD08Qjqq&slot이거한번보고할만하면해봐줘'
        with patch.object(reply_crop,'_ocr',return_value=ocr):
            result=reply_crop.isolate_reply({'text':original},path)
            self.assertEqual((result['body'],result['quote_kind']),(original,'image'))
            self.assertIsNotNone(result['quote_thumbnail'])
            # Another link of the same length and the same sentence is not this reply.
            self.assertIsNone(reply_crop.isolate_reply({'text':original.replace('DfIkpl0OQg?xmt=AQG0AtobX4Om3wWQ','Zr7mVc2TnYh?utm=PwKe9sBd3FxU6jLa')},path))
            # A bare link that OCR could not read exactly is never classified as a plain command bubble.
            self.assertIsNone(reply_crop.message_kind({'text':original.split('\n')[0]},path))

    def test_newest_message_is_selected_by_position_when_its_body_cannot_be_read(self):
        path,ocr=self.fixture(photo=True)
        ocr['words'][2]['text']='00'  # Windows OCR reads the reply "ㅇㅇ" as two zeros.
        with patch.object(reply_crop,'_ocr',return_value=ocr):
            self.assertIsNone(reply_crop.isolate_reply({'text':'ㅇㅇ'},path))
            result=reply_crop.isolate_reply({'text':'ㅇㅇ'},path,last=True)
            self.assertEqual((result['body'],result['quote_kind']),('ㅇㅇ','image'))
            # A body that plainly reads as some other text is not this message.
            ocr['words'][2]['text']='전혀다른긴문장이여기에적혀있습니다'
            self.assertIsNone(reply_crop.isolate_reply({'text':'ㅇㅇ'},path,last=True))
            self.assertIsNone(reply_crop.isolate_reply({'text':'내일아침아홉시에다시확인해줘'},path,last=True))
            ocr['words'][2]['text']='00'
            # Anything below the bubble (a newer bubble, photo or link card) means it is not the newest.
            with Image.open(path) as frame:
                ImageDraw.Draw(frame).rectangle((200,330,390,360),fill=(255,255,255))
                frame.save(path)
            self.assertIsNone(reply_crop.isolate_reply({'text':'ㅇㅇ'},path,last=True))
        # Two visible copies of one sentence: chat order says the newest is the lower bubble.
        path,ocr=self.fixture(duplicate=True)
        with patch.object(reply_crop,'_ocr',return_value=ocr), \
             patch.object(reply_crop,'_write_crops',return_value={'path':'crop.png'}) as crops:
            self.assertIsNone(reply_crop.isolate_reply({'text':'진행해 주세요'},path))
            self.assertTrue(reply_crop.isolate_reply({'text':'진행해 주세요'},path,last=True))
            self.assertEqual(crops.call_args.args[1][1],420)
        # An ordinary bubble without a reply header is never selected by position.
        path,ocr=self.fixture(anchor='일반 메시지')
        with patch.object(reply_crop,'_ocr',return_value=ocr):
            self.assertIsNone(reply_crop.isolate_reply({'text':'진행해 주세요'},path,last=True))

    def test_photo_quote_is_known_by_its_picture_when_ocr_drops_the_label(self):
        # Kakao's layout: the thumbnail left of the header, the pale "사진" label under the header.
        # Beside a white page of small print Windows OCR returns no word for that label.
        frame=Image.new('RGB',(420,380),(185,205,220)); draw=ImageDraw.Draw(frame)
        draw.rounded_rectangle((120,100,390,300),radius=9,fill=(255,220,0))
        draw.rectangle((135,112,185,162),fill=(250,250,250))
        for y in range(120,160,8):
            draw.line((140,y,180,y),fill=(90,90,90))
        draw.rectangle((200,112,299,127),fill=(25,25,25))    # header
        draw.rectangle((200,150,240,165),fill=(148,136,18))  # the label OCR did not read
        draw.line((132,190,378,190),fill=(200,180,0),width=1)
        draw.rectangle((132,212,231,227),fill=(25,25,25))    # body
        path=self.root/'viewport.png'; frame.save(path); frame.close()
        ocr={'angle':0,'words':[{'text':'나에게 답장','x':200,'y':112,'width':100,'height':16,'line':0},
                                {'text':'진행해 주세요','x':132,'y':212,'width':100,'height':16,'line':2}]}
        with patch.object(reply_crop,'_ocr',return_value=ocr):
            for last in (False,True):
                result=reply_crop.isolate_reply({'text':'진행해 주세요'},path,last=last)
                self.assertEqual(result['quote_kind'],'image')
                with Image.open(result['quote_thumbnail']) as thumbnail:
                    self.assertEqual(thumbnail.size,(51,51))
        # No words and no picture in the quoted block: still not a reply, and the reason is named.
        path,ocr=self.fixture()
        del ocr['words'][1]
        cache={}
        with patch.object(reply_crop,'_ocr',return_value=ocr):
            self.assertIsNone(reply_crop.isolate_reply({'text':'진행해 주세요'},path,cache))
        self.assertEqual(cache['isolate_stop'],'quote_block_unreadable')
        # An OCR process that ran out of time is named as such, not as "no reply on screen".
        cache={}
        with patch.object(reply_crop,'_ocr',side_effect=reply_crop.subprocess.TimeoutExpired('ocr',45)):
            self.assertIsNone(reply_crop.isolate_reply({'text':'진행해 주세요'},path,cache))
        self.assertEqual(cache['isolate_stop'],'ocr_unavailable')


if __name__ == '__main__':
    unittest.main()
