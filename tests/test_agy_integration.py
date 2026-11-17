from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from talkturn import cli, plain_delivery, providers, reply_bridge, result_format, runtime, service, sessions


ID = '11111111-1111-4111-8111-111111111111'


class AgyIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for module in (runtime, plain_delivery, service, reply_bridge):
            self.stack.enter_context(patch.object(module, 'ROOT', self.root))
        self.row = {'provider':'agy', 'session_id':ID, 'code':sessions.code('agy', ID),
                    'name':'AGY 테스트', 'cwd':str(self.root), 'project':'test',
                    'state':'재개 가능', 'updated_at':time.time(), 'live':False,
                    'reachable':True, 'model':None, 'effort':None}
        runtime.write_json('session-catalog.json', {'rows':[self.row], 'fetched_at':time.time()})

    def test_provider_identity_is_distinct_and_existing_codes_stay_stable(self):
        for provider, prefix in (('claude','C-'), ('codex','X-'), ('agy','G-')):
            expected = prefix + hashlib.sha256(f'{provider}:{ID}'.encode()).hexdigest()[:8].upper()
            self.assertEqual(sessions.code(provider, ID), expected)
        self.assertEqual(len({sessions.identity({**self.row,'provider':p}) for p in ('claude','codex','agy')}), 3)

    def test_agy_metadata_discovery_does_not_start_or_resume_work(self):
        with patch.object(sessions,'claude_sessions',return_value=([],'')), \
             patch.object(sessions,'codex_sessions',return_value=([],'')), \
             patch.object(sessions,'agy_sessions',return_value=([self.row],'')), \
             patch.object(reply_bridge,'forward') as forward:
            catalog = sessions.discover()
        self.assertEqual(catalog['rows'], [self.row])
        self.assertFalse(catalog['rows'][0]['live'])
        forward.assert_not_called()

    def test_selected_agy_routes_exact_id_and_original_text_to_its_adapter(self):
        from talkturn import agy_transport
        message = '원문  두 칸\n그대로 $(keep)'
        with patch.object(agy_transport,'validate',return_value=self.row), \
             patch.object(agy_transport,'forward',return_value={'accepted':True,'provider':'agy','session_id':ID}) as send:
            sessions.choose(self.row['code'])
            self.assertEqual(sessions.selected()['provider'], 'agy')
            sessions.send_selected(message)
        origin, text = send.call_args.args
        self.assertEqual((origin['source'], origin['session_id'], origin['cwd']), ('agy', ID, str(self.root)))
        self.assertEqual(text, message)

    def test_agy_result_keeps_title_origin_and_reply_candidate(self):
        runtime.write_json('delivery.json', {'mode':'native','long_text':'text'})
        runtime.write_json('ui.json', {'watched_sessions':[self.row]})
        origin = {'source':'agy','session_id':ID,'cwd':str(self.root),'kind':'result'}
        receipt = subprocess.CompletedProcess([], 0, '{"verified":true,"baseline_hashes":[]}', '')
        with patch.object(plain_delivery.subprocess,'run',return_value=receipt) as send, \
             patch('talkturn.cache.trim_generated_images'):
            self.assertTrue(plain_delivery.deliver('AGY 결과 원문', 'agy-result', origin=origin))
        packet = json.loads(send.call_args.kwargs['input'])
        self.assertIn('[AGY 테스트]', packet['text'])
        candidates = plain_delivery.reply_candidates()
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]['origin'], origin)
        self.assertEqual(result_format.session_heading(origin), '[AGY 테스트]')
        runtime.write_json('ui.json', {'watched_sessions':[]})
        self.assertEqual(plain_delivery.reply_candidates(), [])

    def test_connection_batch_contains_agy_count_and_anchor(self):
        runtime.write_json('ui.json', {'watched_sessions':[self.row]})
        with patch.object(plain_delivery,'deliver',return_value=True) as deliver:
            service._send_connection_batch(connection_at=1)
        self.assertIn('Antigravity 1', deliver.call_args_list[0].args[0])
        self.assertEqual(deliver.call_args_list[1].kwargs['origin']['source'], 'agy')
        self.assertIn('재개 가능', service.connection_message())

    def test_unsupported_agy_settings_never_fall_through_to_codex(self):
        with patch.object(providers,'config_path') as path, patch.object(providers,'atomic_write') as write:
            with self.assertRaises(ValueError):
                providers.update_provider_setting('agy','model','anything')
            with self.assertRaises(ValueError):
                providers.set_model('agy','anything')
            with self.assertRaises(ValueError):
                providers.set_effort('agy','high')
        path.assert_not_called()
        write.assert_not_called()
        with self.assertRaises(ValueError):
            providers.config_path('unknown')
        with patch.object(providers,'CodexReader') as codex:
            self.assertEqual(providers.usage('agy')['agy']['state'], 'unavailable')
        codex.assert_not_called()

    def test_explicit_missing_agy_binary_does_not_fall_back_to_a_real_install(self):
        with patch.dict(os.environ, {'TALKTURN_AGY_BIN':str(self.root/'missing.exe')}), \
             patch.object(runtime.shutil,'which') as which:
            with self.assertRaises(RuntimeError):
                runtime.executable('agy')
        which.assert_not_called()

    def test_setup_agy_installs_only_the_agy_hook_without_kakao_or_other_hooks(self):
        with patch.object(cli,'executable',return_value='agy.exe'), \
             patch('talkturn.agy_hooks.install') as install, \
             patch.object(service,'install_hooks') as other, \
             patch.object(service,'start') as start:
            self.assertIn('Antigravity', cli.dispatch(['setup-agy']))
        install.assert_called_once_with()
        other.assert_not_called()
        start.assert_not_called()

    def test_modified_agy_hook_does_not_prevent_other_owned_hooks_from_uninstalling(self):
        with patch.object(service,'stop'), patch.object(service,'startup'), \
             patch('talkturn.agy_hooks.uninstall',side_effect=ValueError('modified')), \
             patch.object(service,'config_path',side_effect=lambda provider:self.root/provider/'config'), \
             patch.object(service,'provider_config',return_value={}) as config:
            result = service.uninstall()
        self.assertEqual([call.args[0] for call in config.call_args_list], ['claude','codex'])
        self.assertIn('AGY 훅', result)
        self.assertIn('보존', result)


if __name__ == '__main__':
    unittest.main()
