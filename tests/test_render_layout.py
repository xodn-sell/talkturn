from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw
from talkturn import render


class RenderLayoutTest(unittest.TestCase):
    def capture(self, text, heading='[검증 작업]\n컴퓨터: 테스트 PC'):
        calls = []
        draw_text = ImageDraw.ImageDraw.text

        def record(draw, xy, text, *args, **kwargs):
            font = kwargs.get('font')
            calls.append((draw._image, xy, text, font))
            return draw_text(draw, xy, text, *args, **kwargs)

        with tempfile.TemporaryDirectory() as folder, patch.object(render, 'ROOT', Path(folder)), \
             patch.object(ImageDraw.ImageDraw, 'text', record):
            files = render.text_images(text, 'layout-test', heading=heading)
            sizes = []
            for path in files:
                with Image.open(path) as image:
                    sizes.append(image.size)
            # Images are closed after saving; captured geometry/text/font remain inspectable.
            return calls, sizes

    def content(self, calls):
        return ''.join(item[2] for item in calls if item[3].size in (34, 36, 42, 48))

    def test_unicode_urls_and_plain_source_are_preserved_without_horizontal_clipping(self):
        cases = ['한글문장' * 120, 'https://example.org/' + 'very-long-path_' * 40,
                 'English words with wrapping. ' * 90, '공백  두 칸\t탭\r\n다음 줄',
                 '✅ 확인 ❌ 실패 🚀 시작 🧪 검증 👨‍👩‍👧‍👦 가족 e\u0301 결합',
                 'x' * 46 + ' ' + 'y' * 80, '[문서](https://example.org/document?q=1&v=2)',
                 '첫 줄\n\n\n마지막 줄']
        for text in cases:
            with self.subTest(text=text[:25]):
                calls, sizes = self.capture(text)
                actual = self.content(calls)
                expected = text.replace('\r\n', '\n').expandtabs(4).replace('\n', '')
                self.assertEqual(actual, expected)
                for image, (x, y), value, font in calls:
                    if font.size not in (34, 36, 42, 48):
                        continue
                    self.assertLessEqual(x + font.getlength(value), render.WIDTH - render.MARGIN + 1)
                self.assertTrue(all(width == 1080 and height <= 1500 for width, height in sizes))

    def test_common_emoji_use_real_glyphs_and_keep_joined_sequences(self):
        source = '✅❌🚀🧪📌⚠️🎉👨‍👩‍👧‍👦'
        calls, _ = self.capture(source)
        content = [item for item in calls if item[3].size == 42]
        self.assertEqual(''.join(item[2] for item in content), source)
        for _, _, glyph, font in content:
            self.assertNotEqual(bytes(font.getmask(glyph)), bytes(font.getmask('\u0378')))
        self.assertEqual(render._clusters('e\u0301👨‍👩‍👧‍👦👍🏽'), ['e\u0301', '👨‍👩‍👧‍👦', '👍🏽'])

    def test_fenced_code_keeps_spacing_and_small_block_stays_on_one_page(self):
        code = 'def example():\n    if True:\n        print("**literal** `literal`")\n    return 1'
        calls, sizes = self.capture(('앞 문장입니다.\n' * 16) + '\n```python\n' + code + '\n```')
        code_calls = [item for item in calls if item[3].size == 36]
        self.assertEqual(''.join(item[2] for item in code_calls), code.replace('\n', ''))
        self.assertEqual(len({id(item[0]) for item in code_calls}), 1)
        self.assertGreater(len(sizes), 1)

    def test_heading_emphasis_and_inline_code_change_style_without_losing_words(self):
        calls, _ = self.capture('## 결과 제목\n**강조 단어**와 `name = 1`을 확인합니다.')
        self.assertEqual(self.content(calls), '결과 제목강조 단어와 name = 1을 확인합니다.')
        self.assertTrue(any(item[3].size == 48 for item in calls))
        self.assertTrue(any(item[3].size == 36 for item in calls))

    def test_table_cell_contents_survive_wrapping_and_wide_table_stays_readable(self):
        for columns in (2, 3, 4, 5, 8):
            with self.subTest(columns=columns):
                labels = [f'항목{i}' for i in range(columns)]
                cells = [f'값{i}' + '긴설명' * (i + 1) for i in range(columns)]
                text = '| ' + ' | '.join(labels) + ' |\n|' + '|'.join(['---'] * columns) + '|\n|' + '|'.join(cells) + '|'
                calls, sizes = self.capture(text)
                content = self.content(calls)
                for label, value in zip(labels, cells):
                    self.assertIn(label, content)
                    self.assertIn(value, content)
                self.assertTrue(all(height <= 1500 for _, height in sizes))

    def test_malformed_or_escaped_markdown_remains_literal(self):
        cases = ['```python\nnever closed', '| a \\| b | c |\n| --- | --- |\n| one | two |',
                 '| a | b |\n| --- | --- |\n| one | two | three |', '**not closed', '###no-space',
                 '2 ** 3 ** 4', '** **', r'\**literal**', '``a`b``',
                 '|A|B|C|\n|---|---|\n|1|2|3|', 'https://example.org/**path**']
        for text in cases:
            with self.subTest(text=text):
                calls, _ = self.capture(text)
                self.assertEqual(self.content(calls), text.replace('\n', ''))

    def test_long_code_label_wraps_and_never_sits_alone_on_a_page(self):
        label = 'language-info-' * 20
        calls, sizes = self.capture(('前\n' * 20) + '\n```' + label + '\n' + ('print(1)\n' * 30) + '```')
        for image, (x, y), value, font in calls:
            self.assertLessEqual(x + font.getlength(value), render.WIDTH - render.MARGIN + 1)
        code_pages = {id(item[0]) for item in calls if item[3].size == 36}
        label_pages = {id(item[0]) for item in calls if item[3].size == 26 and item[2] in set(label)}
        self.assertTrue(label_pages.issubset(code_pages))
        self.assertTrue(all(height <= 1500 for _, height in sizes))

    def test_small_table_header_and_first_row_stay_together(self):
        text = ('앞 문장\n' * 16) + '\n|H1|H2|\n|---|---|\n|' + '긴내용 ' * 20 + '|value|'
        calls, _ = self.capture(text)
        pages = {id(item[0]) for item in calls if item[3].size == 34}
        self.assertEqual(len(pages), 1)

    def test_short_table_columns_stay_narrow_and_long_ones_take_the_rest(self):
        self.assertEqual(render.column_widths([100, 200], 600), [100, 500])
        widths = render.column_widths([80, 900, 900], 952)
        self.assertEqual(widths[0], 80)
        self.assertAlmostEqual(sum(widths), 952)
        calls, _ = self.capture('|번호|설명|\n|---|---|\n|1|' + '아주 긴 설명 문장 ' * 12 + '|')
        starts = sorted({x for _, (x, _), _, font in calls if font.size == 34})
        self.assertLess(starts[1] - starts[0], 200)  # the number column no longer takes half the width

    def test_heading_stays_with_following_content_including_blank_line(self):
        for count in (15, 16, 17, 18, 19):
            for body in ('이어질 본문', '```python\nprint(1)\nprint(2)\n```'):
                with self.subTest(prefix=count, code=body.startswith('```')):
                    calls, _ = self.capture(('앞\n' * count) + '\n# 단락 제목\n\n' + body)
                    headings = {id(item[0]) for item in calls if item[3].size == 48}
                    if body.startswith('```'):
                        content = {id(item[0]) for item in calls if item[3].size == 36}
                    else:
                        content = {id(item[0]) for item in calls if item[3].size == 42 and item[2] == '이'}
                    self.assertEqual(headings, content)

    def test_short_empty_and_long_results_have_bounded_pages_and_repeated_title(self):
        for text in ('', '완료했습니다.', '긴 결과 문장입니다.\n' * 180):
            with self.subTest(length=len(text)):
                calls, sizes = self.capture(text)
                titles = ''.join(item[2] for item in calls if item[3].size == 38)
                self.assertEqual(titles, '[검증 작업]' * len(sizes))
                self.assertTrue(all(340 <= height <= 1500 for _, height in sizes))
                self.assertNotIn('다음 장에 계속', calls[-1][2])


if __name__ == '__main__':
    unittest.main()
