import copy
import unittest
from unittest.mock import patch
from talkturn import result_format


class ResultFormatTest(unittest.TestCase):
    def test_cached_title_matches_provider_and_session_not_duplicate_name(self):
        rows = [
            {'provider': 'claude', 'session_id': 'shared-id', 'name': '동일한 제목'},
            {'provider': 'codex', 'session_id': 'other-id', 'name': '동일한 제목'},
            {'provider': 'codex', 'session_id': 'shared-id', 'name': '정확한 작업'},
        ]
        origin = {'source': 'codex', 'session_id': 'shared-id', 'name': '라우팅 이름', 'title': '이전 제목'}
        before = copy.deepcopy(origin)
        with patch.object(result_format, 'read_json', return_value={'rows': rows}):
            self.assertEqual(result_format.session_heading(origin), '[정확한 작업]')
            self.assertEqual(result_format.session_heading({'source': 'claude', 'session_id': 'shared-id'}), '[동일한 제목]')
        self.assertEqual(origin, before)

    def test_claude_heading_prefers_current_title_over_stale_cached_name(self):
        session = '11111111-1111-4111-8111-111111111111'
        rows = [{'provider': 'claude', 'session_id': session, 'name': 'claude-e6'}]
        with patch.object(result_format, 'read_json', return_value={'rows': rows}), \
                patch('talkturn.sessions.claude_title', return_value='원래 제목') as title:
            self.assertEqual(result_format.session_heading({'source': 'claude', 'session_id': session}), '[원래 제목]')
            title.return_value = ''
            self.assertEqual(result_format.session_heading({'source': 'claude', 'session_id': session}), '[claude-e6]')

    def test_heading_removes_controls_line_breaks_and_caps_length(self):
        with patch.object(result_format, 'read_json', return_value={}):
            origin = {'source': 'codex', 'session_id': '12345678-rest',
                      'title': ' 첫째\r\n둘째\t셋째\x00\u202e끝\u2028줄 '}
            self.assertEqual(result_format.session_heading(origin), '[첫째 둘째 셋째 끝 줄]')
            origin['title'] = '가' * 150
            self.assertEqual(result_format.session_heading(origin), '[' + '가' * 99 + '…]')
            origin['title'] = '이름] [오후 11:59] 톡턴 전환 claude 2'
            from talkturn._kakao.parser import has_export_structure
            self.assertFalse(has_export_structure(result_format.session_heading(origin)))

    def test_missing_cached_title_uses_explicit_title_then_name_then_honest_fallback(self):
        origin = {'source': 'claude', 'session_id': '12345678-rest', 'title': '명시 제목', 'name': '수신 이름'}
        with patch.object(result_format, 'read_json', return_value={}):
            self.assertEqual(result_format.session_heading(origin), '[명시 제목]')
            origin['title'] = ' \n '
            self.assertEqual(result_format.session_heading(origin), '[수신 이름]')
            origin['name'] = ''
            self.assertEqual(result_format.session_heading(origin), '[제목 없는 작업 · 12345678]')
        with patch.object(result_format, 'read_json', side_effect=ValueError('invalid cache')):
            self.assertEqual(result_format.session_heading(origin), '[제목 없는 작업 · 12345678]')

    def test_non_session_origin_has_no_heading_or_cache_read(self):
        with patch.object(result_format, 'read_json') as read:
            for origin in (None, {}, {'source': 'codex'}, {'source': 'claude', 'session_id': ''},
                           {'source': 'other', 'session_id': '12345678'}):
                with self.subTest(origin=origin):
                    self.assertEqual(result_format.session_heading(origin), '')
            read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
