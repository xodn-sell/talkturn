"""AGY navigation and commands without launching providers or accessing real user state."""
from contextlib import ExitStack
import io
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from rich.console import Console
from talkturn import account_menu, cli, commands, connections, providers, runtime, sessions, terminal


def row(provider, number):
    prefix = {'claude': 'C', 'codex': 'X', 'agy': 'G'}[provider]
    return {'provider': provider, 'session_id': f'{provider}-{number}', 'code': f'{prefix}-{number:08X}',
            'name': f'{provider}-task-{number}', 'state': '재개 가능' if provider == 'agy' else '대기',
            'model': None, 'live': provider != 'agy', 'reachable': True}


class AgyUiTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for module in (runtime, commands, cli):
            self.stack.enter_context(patch.object(module, 'ROOT', root))

    def test_aliases_and_section_numbers_keep_existing_section_three(self):
        for alias in ('agy', 'AGY', 'antigravity', 'Antigravity', '안티그래비티'):
            with self.subTest(alias=alias):
                for command in ('connect', 'section', 'sessions', 'usage', 'models', 'account-add'):
                    self.assertEqual(commands.parse(f'{command} {alias}'), [command, 'agy'])
                self.assertEqual(commands.parse(f'{alias} --Argument MiXeD'), ['agy', '--Argument', 'MiXeD'])
                self.assertEqual(commands.select_section(alias), 'agy')
        self.assertEqual(commands.select_section('3'), 'all')
        self.assertEqual(commands.select_section('4'), 'agy')
        self.assertIn('3 전체', commands.SECTION_MENU)
        self.assertIn('G-…', commands.HELP)

    def test_agy_queries_use_the_agy_provider_without_changing_the_selected_session(self):
        original = row('codex', 1)
        runtime.write_json('ui.json', {'selected_session': original, 'watched_sessions': [original]})
        commands.select_section('agy')
        settings = {provider: {'model': provider + '-model'} for provider in ('claude', 'codex', 'agy', 'relay')}
        settings['scope'] = 'provider defaults'
        catalog = {'rows': [row('agy', 1)], 'errors': {}}
        with patch.object(providers, 'usage', return_value={'agy': {'limits': []}}) as usage, \
             patch.object(providers, 'models', return_value=['agy-model']) as models, \
             patch.object(providers, 'model_settings', return_value=settings), \
             patch.object(sessions, 'discover', return_value=catalog), \
             patch.object(sessions, 'list_text', return_value='AGY work') as listed:
            self.assertIn('한도 미제공', commands.execute(['usage']))
            usage.assert_called_once_with('agy')
            self.assertIn('agy-model', commands.execute(['models']))
            models.assert_called_once_with('agy')
            self.assertIn('agy-model', commands.execute(['model']))
            self.assertNotIn('codex-model', commands.execute(['model']))
            self.assertEqual(commands.execute(['sessions', 'agy']), 'AGY work')
            listed.assert_called_once_with(catalog, 'agy')
            commands.select_section('all')
            all_models = commands.execute(['model'])
            for provider in ('claude', 'codex', 'agy', 'relay'):
                self.assertIn(provider + '-model', all_models)
        self.assertEqual(runtime.read_json('ui.json')['selected_session'], original)

    def test_agy_account_actions_never_reach_ccswap_or_account_login_ui(self):
        with patch.object(providers, 'switch_account') as switch, \
             patch.object(providers, 'add_account') as add, \
             patch.object(providers, 'accounts') as accounts, \
             patch.object(account_menu, 'switch') as switch_ui, \
             patch.object(account_menu, 'add') as add_ui:
            for parts in (['switch', 'agy'], ['switch', 'agy', '2'], ['account-add', 'agy']):
                with self.subTest(parts=parts):
                    with self.assertRaisesRegex(ValueError, 'AGY.*지원하지'):
                        cli.dispatch(parts)
                    with self.assertRaisesRegex(ValueError, 'AGY.*지원하지'):
                        commands.execute(parts, remote=True)
            commands.select_section('agy')
            self.assertIn('지원하지', commands.execute(['accounts']))
            for call in (switch, add, accounts, switch_ui, add_ui):
                call.assert_not_called()

    def test_agy_model_timeout_returns_controlled_error_without_provider_output(self):
        failure = subprocess.TimeoutExpired(['agy.exe', 'models'], 20, output='private partial output')
        with patch.object(providers, 'executable', return_value='agy.exe'), \
             patch.object(providers.subprocess, 'run', side_effect=failure) as query:
            with self.assertRaisesRegex(RuntimeError, 'AGY 모델 조회 시간이 초과') as caught:
                commands.execute(['models', 'agy'])
        self.assertNotIn('private', str(caught.exception))
        self.assertEqual(query.call_count, 1)

    def test_explicit_cli_launch_preserves_arguments_and_remote_commands_cannot_launch(self):
        for alias in ('agy', 'AGY', 'Antigravity', '안티그래비티'):
            with self.subTest(alias=alias), patch.object(cli.sys, 'argv', ['talkturn', alias, '--prompt', 'MiXeD $() ; text']), \
                 patch.object(cli, 'console', Mock()), patch.object(cli, 'executable', return_value='agy.exe'), \
                 patch.object(cli.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as launch:
                cli.main()
                launch.assert_called_once_with(['agy.exe', '--prompt', 'MiXeD $() ; text'])
                with self.assertRaises(ValueError):
                    commands.execute(['agy'], remote=True)
                self.assertEqual(launch.call_count, 1)

    def test_connect_uses_resumable_metadata_or_explicitly_opens_one_agy_terminal(self):
        stored = row('agy', 1)
        with patch.object(sessions, 'discover', return_value={'rows': [stored]}) as discover, \
             patch.object(connections, 'executable', return_value='agy.exe'), \
             patch.object(connections.subprocess, 'Popen') as launch:
            text = connections.connect('agy')
            self.assertIn('같은 ID로 재개', text)
            self.assertNotIn('자동 연결', text)
            launch.assert_not_called()
            discover.return_value = {'rows': []}
            self.assertIn('터미널', connections.connect('agy'))
            self.assertEqual(launch.call_args.args[0], ['agy.exe'])
            self.assertEqual(launch.call_args.kwargs['creationflags'], subprocess.CREATE_NEW_CONSOLE)
            with self.assertRaises(ValueError):
                connections.connect('agy;whoami')
            self.assertEqual(launch.call_count, 1)
        console = Mock()
        console.input.return_value = '3'
        with patch.object(sessions, 'discover', return_value={'rows': []}), \
             patch.object(terminal, 'response'), patch.object(connections, 'connect', return_value='AGY selected') as connect:
            self.assertEqual(connections.menu(console), 'AGY selected')
            connect.assert_called_once_with('agy')

    def test_login_header_keeps_verified_provider_counts_without_claiming_agy_login(self):
        console = Console(file=io.StringIO(), width=100, height=30)
        view = {'compact': True, 'account_counts': {'claude': 1, 'codex': 0, 'agy': 5}}
        console.print(terminal.header(console, view))
        text = console.file.getvalue()
        self.assertIn('Claude 1개', text)
        self.assertIn('Codex 0개', text)
        self.assertNotIn('AGY', text)
        self.assertNotIn('5개', text)
        # Conversations read over ACP show AGY as connected, still without a login count.
        console = Console(file=io.StringIO(), width=100, height=30)
        console.print(terminal.header(console, {**view, 'rows': [{'provider': 'agy', 'reachable': True}]}))
        text = console.file.getvalue()
        self.assertIn('AGY 연결됨', text)
        self.assertNotIn('5개', text)

    def picker(self, view, keys):
        console = Console(file=io.StringIO(), force_terminal=True, legacy_windows=False, width=80, height=24)
        frames = []
        original = terminal.dashboard

        def render(console, current, **kwargs):
            frames.append((current['active_provider'], current.get('focused_code')))
            return original(console, current, **kwargs)

        keyboard = SimpleNamespace(getwch=lambda: keys.pop(0), kbhit=lambda: bool(keys))
        with patch.object(terminal.sys.stdin, 'isatty', return_value=True), \
             patch.dict('sys.modules', {'msvcrt': keyboard}), patch.object(terminal, 'dashboard', side_effect=render):
            result = terminal.keyboard_pick(console, view)
        self.assertFalse(keys)
        return result, frames, console.file.getvalue()

    def test_three_tabs_cycle_both_ways_preserving_each_cursor_and_all_checks(self):
        rows = [row(provider, number) for provider in ('claude', 'codex', 'agy') for number in (1, 2)]
        view = {'rows': rows, 'selected': None, 'watched': [], 'usage': {}, 'waiting': []}
        keys = ['\xe0', 'P', ' ', '\xe0', 'M', '\xe0', 'P', ' ',
                '\xe0', 'M', '\xe0', 'P', ' ', '\xe0', 'M',
                '\xe0', 'K', '\xe0', 'K', '\xe0', 'M', '\r']
        checked, frames, text = self.picker(view, keys)
        self.assertEqual(set(checked), {rows[1]['code'], rows[2]['code'], rows[5]['code']})
        self.assertEqual(frames[-4:], [('claude', rows[1]['code']), ('agy', rows[5]['code']),
                                      ('codex', rows[2]['code']), ('agy', rows[5]['code'])])
        self.assertIn('AGY', text)
        self.assertEqual(view['active_provider'], 'agy')

    def test_empty_agy_tab_and_unsupported_account_keys_preserve_existing_checks(self):
        original = row('claude', 1)
        view = {'rows': [original], 'selected': original, 'watched': [original], 'usage': {}, 'waiting': []}
        checked, frames, _ = self.picker(view, ['\xe0', 'M', '\xe0', 'M', ' ', 'a', 's', '\r'])
        self.assertEqual(checked, [original['code']])
        self.assertIn(('agy', None), frames)
        self.assertEqual(frames[-1], ('agy', None))

    def test_agy_code_input_does_not_conflict_with_account_add_shortcut(self):
        item = row('agy', 1)
        view = {'rows': [item], 'selected': None, 'watched': [], 'usage': {}, 'waiting': []}
        selected, _, _ = self.picker(view, list(item['code']) + ['\r'])
        self.assertEqual(selected, item['code'])


if __name__ == '__main__':
    unittest.main()
