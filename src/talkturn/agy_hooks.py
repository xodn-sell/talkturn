"""AGY lifecycle notifications; hooks never inject prompts or grant permissions."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
import uuid

from . import runtime
from .providers import config_path

RECEIPT = 'installed-agy-hooks.json'
MAX_TRANSCRIPT_BYTES = 2 * 1024 * 1024


def _hooks_path():
    return config_path('agy').parent.parent / 'config' / 'hooks.json'


def _entry():
    def handler(command, *args, timeout=30):
        return {'type': 'command', 'command': subprocess.list2cmdline(
            runtime.module_command('agy_hooks', command, *args, '--home', str(runtime.ROOT))), 'timeout': timeout}
    return {'enabled': True,
            'SessionStart': [handler('event', '--event', 'SessionStart')],
            'PreInvocation': [handler('event', '--event', 'PreInvocation')],
            'Stop': [handler('stop', timeout=300)]}


def _same_handlers(left, right):
    return (isinstance(left, dict) and isinstance(right, dict)
            and {key: value for key, value in left.items() if key != 'enabled'}
            == {key: value for key, value in right.items() if key != 'enabled'})


def _read_hooks(path):
    original = path.read_text(encoding='utf-8-sig') if path.exists() else '{}\n'
    data = json.loads(original)
    if not isinstance(data, dict):
        raise ValueError('AGY hooks.json 형식을 확인할 수 없습니다.')
    return original, data


def install():
    """Merge only our named entry, retaining other hooks and a user's disabled setting."""
    path = _hooks_path().resolve()
    with runtime.locked('settings.lock'):
        original, data = _read_hooks(path)
        desired = _entry()
        previous = runtime.read_json(RECEIPT, {})
        current = data.get('talkturn')
        recorded = (previous.get('entry') if isinstance(previous, dict)
                    and previous.get('path') == str(path) else None)
        if 'talkturn' in data:
            if not (_same_handlers(current, recorded) or _same_handlers(current, desired)):
                raise ValueError('AGY의 talkturn 이름을 다른 훅이 사용 중입니다. 기존 훅은 변경하지 않았습니다.')
            if 'enabled' in current:
                if not isinstance(current['enabled'], bool):
                    raise ValueError('AGY talkturn 훅의 enabled 값이 올바르지 않습니다.')
                desired['enabled'] = current['enabled']
            else:
                desired.pop('enabled')
        if current != desired:
            runtime.atomic_write(runtime.ROOT / 'backups' / f'agy-hooks-{time.time_ns()}.bak', original)
            data['talkturn'] = desired
            runtime.atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + '\n')
        runtime.write_json(RECEIPT, {'path': str(path), 'entry': desired})
    return 'AGY 완료 알림 훅 설치됨. 기존 사용자 훅은 보존했습니다.'


def uninstall():
    """Remove only the recorded installation; never infer ownership from its name alone."""
    with runtime.locked('settings.lock'):
        receipt = runtime.read_json(RECEIPT, {})
        path = _hooks_path().resolve()
        if not isinstance(receipt, dict) or receipt.get('path') != str(path) or not path.exists():
            return False
        original, data = _read_hooks(path)
        if 'talkturn' not in data:
            return False
        if not _same_handlers(data['talkturn'], receipt.get('entry')):
            raise ValueError('AGY talkturn 훅이 설치 후 변경되어 자동 제거하지 않았습니다.')
        runtime.atomic_write(runtime.ROOT / 'backups' / f'agy-hooks-{time.time_ns()}.bak', original)
        del data['talkturn']
        runtime.atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + '\n')
        (runtime.ROOT / RECEIPT).unlink(missing_ok=True)
    return True


def _metadata(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('conversationId'), str):
        raise ValueError('AGY 세션 UUID가 필요합니다.')
    session = str(uuid.UUID(payload['conversationId']))
    if session != payload['conversationId'].lower():
        raise ValueError('AGY 세션 UUID 형식이 올바르지 않습니다.')
    workspaces = payload.get('workspacePaths')
    cwd = workspaces[0] if isinstance(workspaces, list) and workspaces and isinstance(workspaces[0], str) else ''
    model = payload.get('modelName')
    return {'session_id': session, 'cwd': cwd, 'model': model if isinstance(model, str) else ''}


def _remote_path(path):
    value = str(path).replace('\\', '/')
    return value.startswith('//') or value.lower().startswith(('/??/', '/device/', '/globalroot/'))


def _transcript_path(payload, session):
    value = payload.get('transcriptPath')
    if not isinstance(value, str) or not value or _remote_path(value):
        raise ValueError('AGY transcript 경로가 올바르지 않습니다.')
    supplied = Path(value).expanduser()
    if supplied.name not in ('transcript.jsonl', 'transcript_full.jsonl'):
        raise ValueError('지원하지 않는 AGY transcript 파일입니다.')
    base = config_path('agy').parent.absolute()
    canonical_base = base.resolve()
    expected = base / 'brain' / session / '.system_generated' / 'logs' / supplied.name
    if not supplied.is_absolute() or _remote_path(base) or _remote_path(canonical_base):
        raise ValueError('AGY 로컬 transcript 경로가 필요합니다.')
    # Windows can report the same local home using its short 8.3 or expanded spelling.
    if Path(os.path.abspath(supplied)) not in (expected, canonical_base / expected.relative_to(base)):
        raise ValueError('AGY 세션과 transcript 경로가 일치하지 않습니다.')
    # Do not follow a per-session link into another conversation or a network location.
    for component in (expected, *expected.parents):
        if component == base:
            break
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('AGY transcript 링크는 읽지 않습니다.')
    resolved = expected.resolve()
    if _remote_path(resolved) or supplied.resolve() != resolved or not resolved.is_file():
        raise ValueError('AGY 세션과 transcript 경로가 일치하지 않습니다.')
    return resolved


def _last_response(path):
    """Read only a bounded tail and require a complete current-user turn boundary."""
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno())
        start = max(0, before.st_size - MAX_TRANSCRIPT_BYTES)
        stream.seek(start)
        raw = stream.read(MAX_TRANSCRIPT_BYTES)
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('AGY transcript가 읽는 중 변경됐습니다.')
    if not raw or not raw.endswith(b'\n'):
        raise ValueError('AGY transcript의 마지막 줄이 완성되지 않았습니다.')
    lines = raw.splitlines()
    if start:
        lines = lines[1:]  # The first tail fragment cannot prove a complete JSON record.
    user_step = None
    response = None
    for line in lines:
        if not line.strip():
            continue
        row = json.loads(line.decode('utf-8'))
        if not isinstance(row, dict):
            raise ValueError('AGY transcript 기록 형식이 올바르지 않습니다.')
        if row.get('source') == 'USER_EXPLICIT' and row.get('type') == 'USER_INPUT':
            step = row.get('step_index')
            if type(step) is not int or step < 0 or row.get('status') != 'DONE':
                raise ValueError('AGY 사용자 입력 경계를 확인하지 못했습니다.')
            user_step, response = step, None
        elif row.get('source') == 'MODEL' and row.get('type') == 'PLANNER_RESPONSE' and user_step is not None:
            step = row.get('step_index')
            if type(step) is not int or step <= user_step or response is not None and step < response['step_index']:
                raise ValueError('AGY 완료 응답의 순서를 확인하지 못했습니다.')
            response = row
    if (response is None or response.get('status') != 'DONE'
            or not isinstance(response.get('content'), str) or not response['content'].strip()):
        raise ValueError('현재 AGY 사용자 입력 뒤의 완료 응답이 없습니다.')
    # CLI 1.5.9 can include a separate `thinking` field in both transcript variants.
    # Only `content` is the visible response; never serialize the whole record.
    return response['content'], response['step_index']


def handle(command, payload, *, event='PreInvocation'):
    from .tasks import record
    metadata = _metadata(payload)
    if not (runtime.ROOT / 'enabled').exists():
        return {}
    if command == 'event':
        if event not in ('SessionStart', 'PreInvocation'):
            raise ValueError('지원하지 않는 AGY 상태 이벤트입니다.')
        record('agy', metadata, '시작됨' if event == 'SessionStart' else '진행 중')
        return {}
    if command != 'stop':
        raise ValueError('지원하지 않는 AGY 훅입니다.')
    # Documented model_stop and the observed CLI 1.5.9 final-response reason only.
    if (payload.get('terminationReason') not in ('model_stop', 'NO_TOOL_CALL')
            or payload.get('fullyIdle') is not True or payload.get('error')):
        return {}
    record('agy', metadata, '완료')
    if os.environ.get('TALKTURN_AGY_MANAGED_FORWARD') == '1':
        return {}
    from .sessions import receives_notifications
    if not receives_notifications('agy', metadata):
        return {}
    execution = payload.get('executionNum')
    if type(execution) is not int or execution < 0:
        raise ValueError('AGY 실행 번호를 확인하지 못했습니다.')
    path = _transcript_path(payload, metadata['session_id'])
    text, step = _last_response(path)
    key = hashlib.sha256(f"agy|{metadata['session_id']}|{execution}|{step}".encode('utf-8')).hexdigest()
    from .plain_delivery import deliver
    deliver(text, key, cwd=metadata['cwd'], origin={'source': 'agy', 'kind': 'result', **metadata})
    return {}


def main():
    parser = argparse.ArgumentParser(description=__doc__, add_help=False, exit_on_error=False)
    parser.add_argument('command', nargs='?', choices=('event', 'stop'))
    parser.add_argument('--event', choices=('SessionStart', 'PreInvocation'), default='PreInvocation')
    parser.add_argument('--home')
    try:
        args, unknown = parser.parse_known_args()
        if not args.command or unknown:
            raise ValueError('AGY 훅 명령 인수가 올바르지 않습니다.')
        if args.home:
            runtime.ROOT = Path(args.home).expanduser().resolve()
            os.environ['TALKTURN_HOME'] = str(runtime.ROOT)
        if hasattr(sys.stdin, 'reconfigure'):
            sys.stdin.reconfigure(encoding='utf-8')
        handle(args.command, json.load(sys.stdin), event=args.event)
    except Exception as error:
        try:
            runtime.write_json('hook-status.json', {'source': 'agy', 'error': type(error).__name__, 'at': time.time()})
        except Exception:
            pass
    print('{}')


if __name__ == '__main__':
    main()
