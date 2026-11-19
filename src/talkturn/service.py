"""Pairing, hook installation and a user-local background watcher."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import time
import uuid
from .runtime import ROOT, PYTHON, NO_WINDOW, atomic_write, read_json, write_json, locked, module_command
from .providers import config_path, provider_config, replace_toml_root


def process_running(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == 'darwin':
        try:
            os.kill(pid, 0)
        except PermissionError:
            return True
        except OSError:
            return False
        return True
    if sys.platform != 'win32':
        return False
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        return False
    try:
        return kernel.WaitForSingleObject(handle, 0) == 258
    finally:
        kernel.CloseHandle(handle)


def worker_pid():
    try:
        return int((ROOT/'reply-bridge.pid').read_text(encoding='utf-8'))
    except (FileNotFoundError, ValueError):
        return 0


def chat_windows():
    if sys.platform == 'darwin':
        from . import mac_kakao
        return mac_kakao.chat_windows()
    if sys.platform != 'win32':
        raise RuntimeError('현재 Kakao PC 연결은 Windows·macOS에서 지원됨.')
    import win32gui
    import win32process
    import win32api
    from ._kakao import controller, config
    rows = []
    for row in controller.list_chat_windows():
        hwnd = row['hwnd']
        if not controller.find_child_window_recursive(hwnd, config.KAKAO_EDIT_CLASS) or not controller.find_child_window_recursive(hwnd, config.KAKAO_LIST_CONTROL_CLASS):
            continue
        pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        try:
            handle = win32api.OpenProcess(0x1000, False, pid)
            try:
                buffer = ctypes.create_unicode_buffer(32768)
                size = wintypes.DWORD(len(buffer))
                query = ctypes.windll.kernel32.QueryFullProcessImageNameW
                query.argtypes = [wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)]
                if not query(int(handle),0,buffer,ctypes.byref(size)):
                    continue
                executable = buffer.value
            finally:
                handle.Close()
            if Path(executable).name.lower() != 'kakaotalk.exe':
                continue
        except Exception:
            continue
        rows.append({**row, 'pid': pid})
    return rows


def kakao_running():
    """Check the process name without opening chat contents or changing focus."""
    if sys.platform == 'darwin':
        from . import mac_kakao
        return mac_kakao.kakao_running()
    try:
        result=subprocess.run([str(Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/tasklist.exe'),
            '/FI','IMAGENAME eq KakaoTalk.exe','/FO','CSV','/NH'],capture_output=True,timeout=3,creationflags=NO_WINDOW)
    except (OSError,subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    return any(line.split(b',',1)[0].strip(b'"').lower()==b'kakaotalk.exe' for line in result.stdout.splitlines())


def delivery_issues(*,include_worker=True):
    running=kakao_running()
    if running is False:
        return ['카카오톡 꺼짐 · 카카오톡을 켜세요.']
    if running is None:
        return ['카카오톡 실행 상태 확인 실패 · 앱 실행 후 R로 다시 확인하세요.']
    settings=read_json('delivery.json',{})
    if not settings.get('window_id'):
        return ['나와의 채팅 미연결 · 별도 창을 연 뒤 setup을 입력하세요.']
    try:
        rooms=chat_windows()
    except Exception:
        return ['카톡 창 확인 실패 · 나와의 채팅을 열고 setup으로 다시 연결하세요.']
    matching=[row for row in rooms if row['hwnd']==settings.get('window_id') and
              row['pid']==settings.get('process_id') and row['title']==settings.get('room_name')]
    if not matching:
        reason='카톡 창이 바뀜' if any(row['title']==settings.get('room_name') for row in rooms) else '나와의 채팅 닫힘'
        return [reason+' · 별도 창을 열고 setup으로 다시 연결하세요.']
    if not include_worker:
        return []
    if not (ROOT/'reply-enabled').exists() or not process_running(worker_pid()):
        return ['카톡 연결 중지 · start를 입력하세요.']
    issues=[]
    if not (ROOT/'enabled').exists():
        issues.append('결과 알림 꺼짐 · notifications on을 입력하세요.')
    if settings.get('reply_routing_enabled',True) is False:
        issues.append('답장 전달 꺼짐 · replies on을 입력하세요.')
    relay_error=read_json('relay-error.json',None)
    if isinstance(relay_error,dict) and isinstance(relay_error.get('reason'),str):
        issues.append(relay_error['reason'])
    return issues


def connection_status():
    settings = read_json('delivery.json', {})
    if not settings.get('window_id'):
        return '미연결 · setup 필요'
    try:
        matching = [row for row in chat_windows() if row['hwnd'] == settings['window_id'] and row['pid'] == settings['process_id'] and row['title'] == settings['room_name']]
        if len(matching) != 1:
            return '재연결 필요 · 나와의 채팅 창을 열고 setup'
    except RuntimeError as error:
        return str(error) if sys.platform == 'darwin' else 'Windows 카카오톡 필요'
    receipt = read_json('connection.json', {})
    same = receipt.get('window_id') == settings['window_id'] and receipt.get('process_id') == settings['process_id']
    if receipt.get('verified') and same:
        return '연결됨 · talkturn in 전송 확인'
    return '창 연결됨 · ping으로 전송 확인 필요'


def status_text():
    from . import __version__
    settings = read_json('delivery.json', {})
    running = process_running(worker_pid())
    report = read_json('reply-status.json', {})
    lines = [f'TalkTurn {__version__}', f'카톡: {connection_status()}',
             '결과 알림: ' + ('켜짐' if (ROOT/'enabled').exists() else '꺼짐'),
             '답장·카톡 명령: ' + ('실행 중' if running and (ROOT/'reply-enabled').exists() else '중지'),
             '중계 모델: ' + settings.get('model', 'gpt-6-luna')]
    lines.append('답장 판별: '+('작업 번호 · LLM 없음' if settings.get('reply_mode')=='manual' else '자동 · 설정된 모델 사용'))
    lines.append('긴 글 표시: '+settings.get('long_text','image'))
    if report.get('error'):
        lines.append('최근 수신 확인: ' + report['error'])
    elif report.get('time'):
        lines.append(f"최근 수신 확인: {max(0,int(time.time()-report['time']))}초 전")
    lines.append('PC 사용 중에는 입력 충돌 방지를 위해 수신 확인 대기.')
    return '\n'.join(lines)


def pair(title):
    rows = [row for row in chat_windows() if row['title'] == title]
    if len(rows) != 1:
        raise ValueError('정확한 이름의 채팅 창이 하나만 열려 있어야 함.')
    row = rows[0]
    # The user explicitly selects their self-chat; never discover another room automatically.
    with locked('settings.lock'):
        settings = read_json('delivery.json', {})
        settings.update({'name':'TalkTurn', 'display_name':'톡턴', 'mode':'native', 'room_name':title,
                         'window_id':row['hwnd'], 'process_id':row['pid'], 'reply_enabled_at':time.time()})
        settings.setdefault('model', 'gpt-6-luna')
        write_json('delivery.json', settings)
        (ROOT/'baseline-version').unlink(missing_ok=True)


def _connection_sessions():
    """Merge confirmed subscriptions with exact cached identities, without discovery."""
    from .sessions import identity, watched
    catalog = read_json('session-catalog.json', {})
    rows = catalog.get('rows', []) if isinstance(catalog, dict) else []
    rows = rows if isinstance(rows, list) else []
    selected = []
    seen = set()
    for item in watched():
        if not isinstance(item, dict) or item.get('provider') not in ('claude', 'codex', 'agy'):
            continue
        target = item.get('peer_ref') if item.get('target_kind') == 'claude_peer' else item.get('session_id')
        if not isinstance(target, str) or not target.strip():
            continue
        key = identity(item)
        if key in seen:
            continue
        seen.add(key)
        matches = [row for row in rows if isinstance(row, dict) and identity(row) == key]
        selected.append((key, {**item, **matches[0]} if len(matches) == 1 else item))
    return selected


def connection_message(*, update=False):
    """Keep an aggregate description for remote status queries."""
    from .computer import computer_label
    from .result_format import session_heading
    selected = _connection_sessions()
    lines = ['talkturn update' if update else 'talkturn in', computer_label()]
    groups=[('claude', 'Claude Code'), ('codex', 'Codex')]
    if any(key[0]=='agy' for key,row in selected):
        groups.append(('agy','Antigravity'))
    for provider, label in groups:
        group = [(key, row) for key, row in selected if key[0] == provider]
        lines.extend(['', f'{label} · 결과 수신 선택 {len(group)}개'])
        if not group:
            lines.append('선택한 세션 없음')
        for key, row in group:
            heading = session_heading({'source': provider, 'session_id': key[1],
                                       'title': row.get('name') or row.get('title'), 'name': row.get('name')})
            state = row.get('state')
            if row.get('reachable') is False and state not in ('앱 연결 대기', '수신 연결 없음', '이름 중복'):
                state = '수신 연결 없음'
            elif state not in ('앱 연결 대기', '수신 연결 없음', '이름 중복', '실행 상태 미확인',
                             '저장됨', '오류', '상태 미확인', '재개 가능'):
                state = '실행 상태 미확인' if row.get('live') is False else ''
            lines.append('- ' + heading + (' · ' + state if state else ''))
    return '\n'.join(lines)


def _send_connection_batch(*, update=False, connection_at):
    from .computer import computer_label
    from .plain_delivery import deliver
    from .result_format import session_heading
    from .sessions import code
    cards=[]
    titles={}
    for key,row in _connection_sessions():
        heading=session_heading({'source':key[0],'session_id':key[1],
                                 'title':row.get('name') or row.get('title'),'name':row.get('name')})
        origin={'source':key[0],'session_id':row.get('session_id'),'title':heading[1:-1],
                'kind':'session_anchor','connection_at':connection_at}
        for field in ('name','cwd','peer_ref','target_kind'):
            if row.get(field) is not None:
                origin[field]=row[field]
        cards.append((key,heading,origin))
        titles[heading]=titles.get(heading,0)+1
    # One header bubble: separate provider-label bubbles were only scroll on a phone.
    counts=' · '.join(f'{label} {total}' for provider,label in (('claude','Claude Code'),('codex','Codex'),('agy','Antigravity'))
                      if (total:=sum(card[0][0]==provider for card in cards)))
    header=['talkturn update' if update else 'talkturn in',computer_label(),counts or '연결된 세션 없음']
    if cards:
        settings=read_json('delivery.json',{})
        if settings.get('reply_routing_enabled') is False:
            header.append('작업 답장 꺼짐')
        elif settings.get('reply_mode')=='manual':
            header.append('작업에 요청 → 새 메시지로 ‘톡턴 전달 T번호 내용’')
        else:
            header.append('[작업 제목]에 답장 → 요청 보내기')
        header.append('이 안내에 help 답장 → 사용법')
    else:
        header.append('PC의 톡턴에서 받을 작업을 선택하세요.')
    messages=[('\n'.join(header),None)]
    for provider in ('claude','codex','agy'):
        for key,heading,origin in cards:
            if key[0]==provider:
                messages.append((heading+(' · '+code(*key) if titles[heading]>1 else ''),origin))
    event=('selection-update-' if update else 'connection-')+uuid.uuid4().hex
    for index,(text,origin) in enumerate(messages):
        options={'render_long':False}
        if origin is not None:
            options['origin']=origin
        if not deliver(text,f'{event}-{index}',**options):
            raise RuntimeError('세션 변경 메시지 전송을 확인하지 못했습니다.' if update else
                               '연결 메시지 전송을 확인하지 못했습니다.')


def selection_update(previous):
    """Announce changed subscriptions without resetting the reply baseline."""
    from .sessions import identity, watched
    if {identity(row) for row in previous} == {identity(row) for row in watched()}:
        return False
    settings=read_json('delivery.json',{})
    receipt=read_json('connection.json',{})
    if not (receipt.get('verified') and settings.get('window_id')
            and receipt.get('window_id')==settings.get('window_id')
            and receipt.get('process_id')==settings.get('process_id')):
        return False  # First connection announces the current selection with talkturn in.
    _send_connection_batch(update=True,connection_at=receipt.get('at',0))
    return True


def ping():
    settings = read_json('delivery.json', {})
    if not settings.get('window_id'):
        raise RuntimeError('setup에서 나와의 채팅 연결 필요.')
    started=time.time()
    _send_connection_batch(connection_at=started)
    write_json('connection.json', {'verified':True, 'window_id':settings['window_id'],
                                  'process_id':settings['process_id'], 'at':started})


def owned_hook(command, module=None):
    """Recognize actual TalkTurn invocations in legacy shell and exec-form hooks."""
    if isinstance(command,dict):
        if command.get('type','command')!='command':
            return False
        if 'args' in command:
            if not isinstance(command['args'],list) or not all(isinstance(arg,str) for arg in command['args']):
                return False
            parts=[command.get('command',''),*command['args']]
        else:
            command=command.get('command','')
    if isinstance(command,str):
        try:
            parts=[part.strip('"') for part in shlex.split(command,posix=False)]
        except ValueError:
            return False
    elif not isinstance(command,dict):
        return False
    if not parts or not isinstance(parts[0],str):
        return False
    executable_name=parts[0].replace('\\','/').rsplit('/',1)[-1].lower()
    if executable_name not in ('python','python.exe','pythonw','pythonw.exe'):
        return False
    # Historical module names are accepted only for migration/removal, never generated.
    renamed={'tokturn.notify':'talkturn.notify','tokturn.questions':'talkturn.questions'}
    if len(parts)>=3 and parts[1]=='-m':
        actual=renamed.get(parts[2],parts[2])
        if actual in ('talkturn.notify','talkturn.questions','talkturn.statusline'):
            return module is None or actual==renamed.get(module,module)
    return (module in (None,'talkturn.notify') and len(parts)>=2
            and parts[1].replace('\\','/').endswith('/kakao-completion/notify.py'))


def remove_own_stop_hooks(config):
    groups = config.get('hooks', {}).get('Stop', [])
    preserved = []
    for group in groups:
        group = dict(group)
        group['hooks'] = [entry for entry in group.get('hooks', []) if not owned_hook(entry)]
        if group['hooks']:
            preserved.append(group)
    return preserved


def git_bash():
    """Claude Code runs status lines in Git Bash when it finds one, otherwise in PowerShell."""
    if os.name != 'nt':
        return True
    if os.environ.get('CLAUDE_CODE_GIT_BASH_PATH'):
        return Path(os.environ['CLAUDE_CODE_GIT_BASH_PATH']).is_file()
    git = shutil.which('git')
    return bool(git) and any((parent / 'bin/bash.exe').is_file() for parent in Path(git).parents[1:3])


def wrap_statusline(data):
    """Put the usage recorder in front of the user's status line; the shell pipes the same JSON on."""
    current = data.get('statusLine')
    if isinstance(current, dict) and owned_hook(current.get('command', '')):
        current = read_json('previous-statusline.json')
    python, home = str(PYTHON).replace('\\', '/'), str(ROOT).replace('\\', '/')
    previous = current.get('command') if isinstance(current, dict) and current.get('type') == 'command' else None
    # ponytail: Claude Code runs this in Git Bash or PowerShell. A spaced interpreter path or a compound
    # previous command has no form valid in both, and a PowerShell 5.1 pipe prefixes a BOM the next status
    # line cannot parse, so those keep the user's status line as is (usage falls back to ccswap).
    if ' ' in python or current is not None and (not isinstance(previous, str) or not git_bash()
                                                  or any(token in previous for token in (';', '&&', '||', '\n'))):
        return
    write_json('previous-statusline.json', current)
    ours = f'{python} -m talkturn.statusline --home "{home}"'
    data['statusLine'] = {**current, 'command': f'{ours} --pass | {previous}'} if current else {'type': 'command', 'command': ours}


def install_hooks():
    with locked('settings.lock'):
        claude, codex = config_path('claude'), config_path('codex')
        claude_text = claude.read_text(encoding='utf-8-sig') if claude.exists() else '{}'
        codex_text = codex.read_text(encoding='utf-8-sig') if codex.exists() else ''
        data = json.loads(claude_text)
        previous = provider_config('codex').get('notify', [])
        new_notify = module_command('notify', '--home', str(ROOT), 'codex-notify')
        previous_is_ours=bool(isinstance(previous,list) and previous and
                              owned_hook({'command':previous[0],'args':previous[1:]},'talkturn.notify'))
        recover_legacy=(any('kakao-completion' in str(piece) for piece in previous)
                        and not (ROOT/'previous-notify.json').exists())
        if previous != new_notify and (not previous_is_ours or recover_legacy):
            if any('kakao-completion' in str(piece) for piece in previous):
                legacy = Path.home()/'.local/share/kakao-completion/previous-notify.json'
                previous = json.loads(legacy.read_text(encoding='utf-8')) if legacy.exists() else []
            write_json('previous-notify.json', previous)
        groups = remove_own_stop_hooks(data)
        argv = module_command('notify', '--home', str(ROOT), 'hook', '--source', 'claude')
        command = {'command':argv[0],'args':argv[1:]}
        groups.append({'hooks':[{'type':'command',**command,'async':True,'timeout':300}]})
        data.setdefault('hooks', {})['Stop'] = groups
        wrap_statusline(data)
        updated = replace_toml_root(codex_text, 'notify', new_notify)
        for provider, original in [('claude',claude_text),('codex',codex_text)]:
            atomic_write(ROOT/'backups'/f'{provider}-hooks-{time.time_ns()}.bak', original)
        atomic_write(claude, json.dumps(data, ensure_ascii=False, indent=2)+'\n')
        atomic_write(codex, updated)
        write_json('installed-hooks.json', {'codex':new_notify, 'claude':command})
    install_question_hooks()


def install_question_hooks():
    with locked('settings.lock'):
        for provider in ('claude','codex'):
            path=config_path(provider) if provider=='claude' else config_path(provider).with_name('hooks.json')
            original=path.read_text(encoding='utf-8-sig') if path.exists() else '{}'
            data=json.loads(original)
            argv=module_command('questions','--home',str(ROOT),'--source',provider)
            command=({'command':argv[0],'args':argv[1:]} if provider=='claude'
                     else {'command':subprocess.list2cmdline(argv)})
            events={'PreToolUse':'^AskUserQuestion$' if provider=='claude' else '.*request_user_input.*', 'PermissionRequest':'.*'}
            if provider=='claude':
                events['Notification']='idle_prompt|permission_prompt|elicitation_dialog'
            for event,matcher in events.items():
                groups=[]
                for group in data.setdefault('hooks',{}).get(event,[]):
                    group=dict(group)
                    group['hooks']=[hook for hook in group.get('hooks',[]) if not owned_hook(hook,'talkturn.questions')]
                    if group['hooks']:
                        groups.append(group)
                handler={'type':'command',**command,'timeout':660}
                if event=='Notification' or provider=='codex' and event=='PreToolUse':
                    handler['async']=True
                groups.append({'matcher':matcher,'hooks':[handler]})
                data['hooks'][event]=groups
            argv=module_command('notify','--home',str(ROOT),'event','--source',provider)
            activity_command=({'command':argv[0],'args':argv[1:]} if provider=='claude'
                              else {'command':subprocess.list2cmdline(argv)})
            for event in ('SessionStart','UserPromptSubmit','SessionEnd'):
                groups=[]
                for group in data['hooks'].get(event,[]):
                    group=dict(group)
                    group['hooks']=[hook for hook in group.get('hooks',[]) if not owned_hook(hook)]
                    if group['hooks']:
                        groups.append(group)
                groups.append({'hooks':[{'type':'command',**activity_command,'async':True,'timeout':10}]})
                data['hooks'][event]=groups
            atomic_write(ROOT/'backups'/f'{provider}-questions-{time.time_ns()}.bak',original)
            atomic_write(path,json.dumps(data,ensure_ascii=False,indent=2)+'\n')


def startup(enable):
    if sys.platform == 'darwin':
        return  # ponytail: login autostart would need its own permissions for the Python binary; run `talkturn start` after login
    import win32com.client
    folder = Path(os.environ['APPDATA'])/'Microsoft/Windows/Start Menu/Programs/Startup'
    link = folder/'TalkTurn replies.lnk'
    if not enable:
        link.unlink(missing_ok=True)
    else:
        shell = win32com.client.Dispatch('WScript.Shell')
        shortcut = shell.CreateShortcut(str(link))
        pythonw = PYTHON.with_name('pythonw.exe')
        shortcut.TargetPath = str(pythonw if pythonw.exists() else PYTHON)
        shortcut.Arguments = subprocess.list2cmdline(['-m', 'talkturn.service', 'autostart', '--home', str(ROOT)])
        shortcut.WorkingDirectory = str(ROOT)
        shortcut.WindowStyle = 7
        shortcut.Description = 'TalkTurn result and reply bridge'
        shortcut.Save()
    legacy = folder/'TokTurn replies.lnk'  # Exact former application shortcut only.
    if legacy.is_file():
        shell = shell if enable else win32com.client.Dispatch('WScript.Shell')
        old = shell.CreateShortcut(str(legacy))
        try:
            args=[part.strip('"') for part in shlex.split(old.Arguments,posix=False)]
        except ValueError:
            return
        target=old.TargetPath.replace('\\','/').rsplit('/',1)[-1].lower()
        if target in ('python.exe','pythonw.exe') and args[:3]==['-m','tokturn.service','autostart']:
            legacy.unlink()


def _lock_notice():
    """An explicit `in` locks the mouse; a silent resume (autostart, reinstall) never does."""
    from .mouse_lock import NOTICE, engage
    return '\n'+NOTICE if engage() else ''


def start(*, connect=True):
    ROOT.mkdir(parents=True, exist_ok=True)
    if not read_json('delivery.json', {}).get('window_id'):
        raise RuntimeError('setup으로 나와의 채팅을 먼저 연결해야 함.')
    if connect:
        issues=delivery_issues(include_worker=False)
        if issues:
            raise RuntimeError(issues[0])
    with locked('service.lock'):
        if process_running(worker_pid()):
            if not (ROOT/'reply-enabled').exists():
                raise RuntimeError('TalkTurn 수신기 종료 중. 잠시 뒤 start를 다시 실행하세요.')
            if connect:
                ping()
            if not (ROOT/'reply-enabled').exists() or not process_running(worker_pid()):
                raise RuntimeError('TalkTurn 연결 확인 중 수신기가 중지됐습니다.')
            return 'TalkTurn 실행 중. 카톡에 talkturn in 전송·확인 완료.'+_lock_notice() if connect else 'TalkTurn 실행 중.'
        if connect:
            was_enabled=(ROOT/'reply-enabled').exists()
            ping()
            if was_enabled and not (ROOT/'reply-enabled').exists():
                raise RuntimeError('TalkTurn 연결 확인 중 중지 요청을 받았습니다.')
        (ROOT/'enabled').touch()
        (ROOT/'reply-enabled').touch()
        pythonw = PYTHON.with_name('pythonw.exe')
        command = [str(pythonw if pythonw.exists() else PYTHON), '-m', 'talkturn.reply_bridge']
        environment = dict(os.environ, TALKTURN_HOME=str(ROOT), PYTHONUTF8='1')
        try:
            with (ROOT/'bridge-stderr.log').open('ab') as log:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=log, env=environment, creationflags=NO_WINDOW,
                                           start_new_session=sys.platform!='win32')  # macOS: outlive the terminal
            deadline = time.monotonic()+5
            while time.monotonic()<deadline:
                # Windows venv pythonw launchers can run the actual interpreter as a child PID.
                running=process_running(worker_pid())
                if not (ROOT/'reply-enabled').exists():
                    raise RuntimeError('TalkTurn 시작 확인 중 중지 요청을 받았습니다.')
                if running:
                    return 'TalkTurn 시작됨. 카톡 연결 확인: talkturn in'+_lock_notice() if connect else 'TalkTurn 시작됨.'
                if process.poll() is not None:
                    raise RuntimeError('TalkTurn 수신기 시작 실패. bridge-stderr.log 확인 필요.')
                time.sleep(.1)
            raise RuntimeError('TalkTurn 수신기 시작 확인 시간 초과.')
        except BaseException:
            stop()
            raise


def stop():
    (ROOT/'reply-enabled').unlink(missing_ok=True)
    (ROOT/'enabled').unlink(missing_ok=True)
    return 'TalkTurn 중지 요청됨. 진행 중인 확인이 끝나면 종료됨.'


def disconnect():
    from .plain_delivery import deliver
    try:
        deliver('talkturn out','disconnect-'+uuid.uuid4().hex,render_long=False)
    finally:
        stop()
    return 'talkturn out 전송·확인 완료. 수신기 종료 요청됨.'


def uninstall():
    stop()
    startup(False)
    from .agy_hooks import uninstall as uninstall_agy
    agy_warning = ''
    try:
        uninstall_agy()
    except (OSError, ValueError):
        agy_warning = '\nAGY 훅은 변경되었거나 읽을 수 없어 보존했습니다. AGY hooks.json에서 직접 확인해 주세요.'
    with locked('settings.lock'):
        path = config_path('claude')
        data = provider_config('claude')
        line = data.get('statusLine')
        owned_line = isinstance(line, dict) and owned_hook(line.get('command', ''))
        if owned_line:
            previous = read_json('previous-statusline.json')
            if previous:
                data['statusLine'] = previous
            else:
                data.pop('statusLine')
        if 'hooks' in data:
            for event,groups in data['hooks'].items():
                data['hooks'][event]=[{**group,'hooks':[hook for hook in group.get('hooks',[]) if not owned_hook(hook)]} for group in groups]
                data['hooks'][event]=[group for group in data['hooks'][event] if group['hooks']]
        if 'hooks' in data or owned_line:
            atomic_write(path,json.dumps(data,ensure_ascii=False,indent=2)+'\n')
        path = config_path('codex')
        current = provider_config('codex')
        notify=current.get('notify',[])
        if isinstance(notify,list) and notify and owned_hook({'command':notify[0],'args':notify[1:]},'talkturn.notify'):
            atomic_write(path,replace_toml_root(path.read_text(encoding='utf-8-sig'),'notify',read_json('previous-notify.json',[])))
        hooks=path.with_name('hooks.json')
        if hooks.exists():
            data=json.loads(hooks.read_text(encoding='utf-8-sig'))
            for event,groups in data.get('hooks',{}).items():
                data['hooks'][event]=[{**group,'hooks':[hook for hook in group.get('hooks',[]) if not owned_hook(hook)]} for group in groups]
                data['hooks'][event]=[group for group in data['hooks'][event] if group['hooks']]
            atomic_write(hooks,json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    return 'TalkTurn 훅·자동 시작 제거됨. 개인 설정과 기록은 보존됨.' + agy_warning


def migrate_legacy():
    legacy = Path.home()/'.local/share/kakao-completion'
    if not (legacy/'delivery.json').exists():
        raise RuntimeError('이전 TalkTurn 설정 없음.')
    if (ROOT/'delivery.json').exists():
        raise RuntimeError('현재 설정이 이미 존재함. 덮어쓰지 않음.')
    (legacy/'reply-enabled').unlink(missing_ok=True)
    (legacy/'enabled').unlink(missing_ok=True)
    pid = int((legacy/'reply-bridge.pid').read_text()) if (legacy/'reply-bridge.pid').exists() else 0
    deadline = time.monotonic()+50
    while process_running(pid):
        if time.monotonic()>deadline:
            raise RuntimeError('기존 수신기 종료 대기 중. 잠시 후 --migrate 다시 실행.')
        time.sleep(.5)
    ROOT.mkdir(parents=True, exist_ok=True)
    # Explicit allowlist: never migrate OAuth secrets, settings backups or raw chat captures.
    for name in ('delivery.json','native-delivered.json','result-origins.json','previous-notify.json','last-forward.json'):
        if (legacy/name).exists():
            shutil.copy2(legacy/name,ROOT/name)
    records = read_json('result-origins.json', [])
    for row in records:
        images=[]
        for old in row.get('images', []):
            source=Path(old).resolve()
            if source.parent == (legacy/'sent-images').resolve() and source.is_file():
                target=ROOT/'sent-images'/source.name
                target.parent.mkdir(exist_ok=True)
                shutil.copy2(source,target)
                images.append(str(target))
        row['images']=images
    write_json('result-origins.json',records)
    settings=read_json('delivery.json',{})
    settings['reply_enabled_at']=time.time()
    write_json('delivery.json',settings)


def bridge_legacy_hook():
    """Already-open apps may still have the old hook command loaded in memory."""
    path=Path.home()/'.local/share/kakao-completion/notify.py'
    if path.exists():
        backup=path.with_name('notify-before-talkturn-package.py.bak')
        if not backup.exists():
            shutil.copy2(path,backup)
        command=module_command('notify','--home',str(ROOT))
        script='import os, sys\ncommand = '+repr(command)+'\nos.execv(command[0], command + sys.argv[1:])\n'
        atomic_write(path,script)


if __name__ == '__main__':
    # The root may be custom; set it before importing worker modules.
    if '--home' in sys.argv:
        os.environ['TALKTURN_HOME'] = sys.argv[sys.argv.index('--home')+1]
        from . import runtime
        runtime.ROOT = ROOT = Path(os.environ['TALKTURN_HOME']).resolve()
    start(connect=False)
