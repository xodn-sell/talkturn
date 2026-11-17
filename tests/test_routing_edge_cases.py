"""Routing stays within current subscriptions; control quotes require exact local body evidence."""
from contextlib import ExitStack
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw
from talkturn import commands, plain_delivery, reply_bridge as bridge, reply_crop, runtime


class RoutingEdgeCasesTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for module in (runtime, commands, plain_delivery, bridge, reply_crop):
            self.stack.enter_context(patch.object(module, 'ROOT', self.root))
        self.stack.enter_context(patch('subprocess.run', side_effect=AssertionError('Unexpected external call')))
        runtime.write_json('delivery.json', {'reply_enabled_at': 1, 'reply_mode': 'luna'})
        runtime.write_json('connection.json', {'at': 1})
        runtime.write_json('ui.json', {'watched_sessions': [{'provider': 'codex', 'session_id': 'qa-thread'}]})
        runtime.write_json('baseline-hashes.json', [])
        (self.root / 'baseline-version').write_text('3', encoding='utf-8')
        (self.root / 'reply-enabled').touch()
        self.anchor = {'id': 'qa-anchor', 'text': '[QA session]', 'sent_at': 10, 'awaiting_reply': True,
                       'origin': {'source': 'codex', 'session_id': 'qa-thread', 'kind': 'session_anchor'}}
        runtime.write_json('result-origins.json', [self.anchor])

    def test_dispatch_rechecks_changes_during_classification_and_preserves_valid_older_results(self):
        crop = self.root / 'crop.png'
        crop.write_bytes(b'synthetic isolated crop')
        message = {'text': 'continue QA work', 'date': '', 'time': ''}
        changes = {
            'unwatch': lambda: runtime.write_json('ui.json', {'watched_sessions': []}),
            'reconnect': lambda: runtime.write_json('connection.json', {'at': 20}),
            'replies_off': lambda: runtime.write_json('delivery.json', {'reply_enabled_at': 1, 'reply_routing_enabled': False}),
            'router_off': lambda: runtime.write_json('delivery.json', {'reply_enabled_at': 1, 'reply_mode': 'manual'}),
            'room_changed': lambda: runtime.write_json('delivery.json', {'reply_enabled_at': 1, 'window_id': 2}),
            'stop': lambda: (self.root / 'reply-enabled').unlink(),
            'same_connection': lambda: None,
            'newer_result': lambda: runtime.write_json('result-origins.json', [self.anchor, {
                'id': 'newer', 'text': 'New result', 'sent_at': 11, 'awaiting_reply': True,
                'origin': {'source': 'codex', 'session_id': 'qa-thread', 'kind': 'result'}}]),
        }
        for name, change in changes.items():
            with self.subTest(name=name):
                runtime.write_json('delivery.json', {'reply_enabled_at': 1, 'reply_mode': 'luna'})
                runtime.write_json('connection.json', {'at': 1})
                runtime.write_json('ui.json', {'watched_sessions': [{'provider': 'codex', 'session_id': 'qa-thread'}]})
                runtime.write_json('baseline-hashes.json', [])
                runtime.write_json('result-origins.json', [self.anchor])
                (self.root / 'reply-enabled').touch()

                def classify(*_):
                    change()
                    return {'body': message['text'], 'quote': '[QA session]', 'quote_kind': 'text'}

                with patch.object(bridge, 'native_read', side_effect=lambda: [copy.deepcopy(message)]), \
                     patch.object(bridge, 'screenshot', return_value=str(self.root / 'viewport.png')), \
                     patch.object(bridge, 'isolate_reply', return_value={
                         'path': str(crop), 'body': message['text'], 'quote_kind': 'text', 'quote_text': '[QA session]'}), \
                     patch.object(bridge, 'call_luna', side_effect=classify), \
                     patch.object(bridge, '_reply_ack', return_value={'ack_sent': True}), \
                     patch.object(bridge, 'forward', return_value={'accepted': True}) as forward:
                    result = bridge.poll_once(require_enabled=True)
                expected = int(name in ('same_connection', 'newer_result'))
                self.assertEqual(forward.call_count, expected)
                self.assertEqual(result['forwarded'], expected)
                if name == 'newer_result':
                    self.assertIn('newer', {item['id'] for item in plain_delivery.pending_replies()})
                elif not expected and name != 'stop':
                    self.assertEqual(runtime.read_json('reply-log.json')[-1]['stage'], 'routing_changed')

    def test_control_quote_requires_exact_body_but_regular_reply_keeps_fuzzy_selection(self):
        shown = 'computer-name abcdefghijklmnopqrstuvwxyz'
        changed = 'computer-name abcdefghijklmnopqrstuvwxzz'
        frame = Image.new('RGB', (420, 380), (185, 205, 220))
        draw = ImageDraw.Draw(frame)
        draw.rounded_rectangle((120, 100, 390, 300), radius=9, fill=(255, 220, 0))
        words = []
        for index, (text, y, width) in enumerate((('나에게 답장', 112, 100), ('talkturn in', 150, 130), (shown, 212, 235))):
            words.append({'text': text, 'x': 132, 'y': y, 'width': width, 'height': 16, 'line': index})
            draw.rectangle((132, y, 131 + width, y + 15), fill=(25, 25, 25))
        draw.line((132, 190, 378, 190), fill=(200, 180, 0), width=1)
        path = self.root / 'viewport.png'
        frame.save(path)
        frame.close()
        with patch.object(reply_crop, '_ocr', return_value={'angle': 0, 'words': words}):
            self.assertIsNotNone(reply_crop.isolate_reply({'text': changed}, path))
            self.assertIsNone(reply_crop.isolate_reply({'text': changed}, path, strict=True))
            for body, expected in ((shown, 1), (changed, 0)):
                with self.subTest(body=body):
                    runtime.write_json('baseline-hashes.json', [])
                    with patch.object(bridge, 'native_read', side_effect=lambda: [{'text': body, 'date': '', 'time': ''}]), \
                         patch.object(bridge, 'screenshot', return_value=str(path)), \
                         patch.object(commands, 'execute', return_value='synthetic response') as execute, \
                         patch.object(plain_delivery, 'deliver', return_value=True), \
                         patch.object(bridge, 'choose_origin', return_value=None) as route:
                        bridge.poll_once(require_enabled=True)
                    self.assertEqual(execute.call_count, expected)
                    self.assertEqual(route.call_count, 1 - expected)


if __name__ == '__main__':
    unittest.main()
