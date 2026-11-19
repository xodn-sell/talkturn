"""Official AGY ACP metadata and explicit replies to existing conversations only."""
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import uuid

from . import __version__, runtime


ACK_TIMEOUT = 30
TURN_TIMEOUT = 1800
LIMITATION = 'AGY 원격 승인·질문 응답은 아직 지원하지 않습니다. 원래 AGY 창에서 확인해 주세요.'
NO_RETRY = '접수 여부를 확인하지 못했습니다. 원래 AGY 작업을 확인하고 자동 재전송하지 마세요.'


def _command():
    return [runtime.executable('agy'), 'acp', '--log-file', os.devnull]


class AgyClient:
    """One in-flight RPC; no authentication, history replay, or new-session methods."""
    def __init__(self, *, writable=False):
        self.writable = writable
        self.process = None
        self.sequence = 0
        self.messages = queue.Queue(maxsize=128)
        self.closing = threading.Event()
        self.capabilities = {}
        self.permission_cancelled = False
        self.unsupported_request = False

    def __enter__(self):
        self.process = subprocess.Popen(_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding='utf-8', bufsize=1, creationflags=runtime.NO_WINDOW,
            env=dict(os.environ, TALKTURN_AGY_MANAGED_FORWARD='1') if self.writable else None)

        def put(value):
            while not self.closing.is_set():
                try:
                    self.messages.put(value, timeout=.1)
                    return
                except queue.Full:
                    pass

        def read():
            try:
                while not self.closing.is_set():
                    line = self.process.stdout.readline(1024 * 1024 + 1)
                    if not line:
                        break
                    if len(line) > 1024 * 1024:
                        put({'_invalid': True})
                        return
                    try:
                        put(json.loads(line))
                    except ValueError:
                        put({'_invalid': True})
                        return
            except (OSError, ValueError, UnicodeError):
                put({'_invalid': True})
            finally:
                put({'_closed': True})

        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        try:
            result = self.call('initialize', {'protocolVersion': 1, 'clientCapabilities': {},
                'clientInfo': {'name': 'talkturn', 'version': __version__}})
            if type(result.get('protocolVersion')) is not int or result['protocolVersion'] != 1:
                raise RuntimeError('지원하지 않는 AGY ACP 프로토콜입니다.')
            self.capabilities = result.get('agentCapabilities', {})
            if not isinstance(self.capabilities, dict):
                raise RuntimeError('AGY 기능 응답 형식을 확인할 수 없습니다.')
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def supports(self, name):
        capabilities = self.capabilities.get('sessionCapabilities', {})
        return isinstance(capabilities, dict) and isinstance(capabilities.get(name), dict)

    def send(self, message):
        try:
            self.process.stdin.write(json.dumps(message, ensure_ascii=False) + '\n')
            self.process.stdin.flush()
        except (OSError, ValueError):
            raise RuntimeError('AGY 연결이 종료됐습니다.') from None

    def _server_request(self, message):
        identifier = message['id']
        if type(identifier) not in (int, str):
            raise RuntimeError('AGY 요청 번호 형식을 확인할 수 없습니다.')
        if message['method'] == 'session/request_permission':
            self.permission_cancelled = True
            self.send({'jsonrpc': '2.0', 'id': identifier,
                       'result': {'outcome': {'outcome': 'cancelled'}}})
        else:
            # No fs, terminal, or elicitation capabilities were advertised.
            self.unsupported_request = True
            self.send({'jsonrpc': '2.0', 'id': identifier,
                       'error': {'code': -32601, 'message': 'Client method is not supported'}})

    def call(self, method, params=None, *, timeout=25, on_update=None):
        allowed = {'initialize', 'session/list'}
        if self.writable:
            allowed.update({'session/resume', 'session/prompt'})
        if method not in allowed:
            raise ValueError('허용되지 않은 AGY 메서드입니다.')
        if timeout <= 0:
            raise TimeoutError('AGY 조회 시간이 초과됐습니다.')
        self.sequence += 1
        identifier = self.sequence
        self.send({'jsonrpc': '2.0', 'id': identifier, 'method': method, 'params': params or {}})
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('AGY 응답 시간이 초과됐습니다.')
            try:
                message = self.messages.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError('AGY 응답 시간이 초과됐습니다.') from None
            if not isinstance(message, dict) or message.get('_invalid'):
                raise RuntimeError('AGY 응답 형식을 확인할 수 없습니다.')
            if message.get('_closed'):
                raise RuntimeError('AGY 응답 연결이 종료됐습니다.')
            if message.get('jsonrpc') != '2.0':
                raise RuntimeError('AGY 응답 프로토콜을 확인할 수 없습니다.')
            if 'method' in message:
                if not isinstance(message['method'], str) or not isinstance(message.get('params', {}), dict):
                    raise RuntimeError('AGY 알림 형식을 확인할 수 없습니다.')
                if 'id' in message:
                    self._server_request(message)
                elif message['method'] == 'session/update' and on_update:
                    on_update(message.get('params', {}))
                continue
            if type(message.get('id')) is not int or message['id'] != identifier:
                raise RuntimeError('AGY 응답 번호가 요청과 다릅니다.')
            if 'error' in message:
                error = message['error']
                if not isinstance(error, dict) or type(error.get('code')) is not int:
                    raise RuntimeError('AGY 오류 응답 형식을 확인할 수 없습니다.')
                # Provider errors can contain account details or conversation text.
                raise RuntimeError(f'AGY 요청 실패 (code {error["code"]}). 원래 AGY 창에서 확인해 주세요.')
            if not isinstance(message.get('result'), dict):
                raise RuntimeError('AGY 결과 응답 형식을 확인할 수 없습니다.')
            return message['result']

    def __exit__(self, *_):
        if not self.process:
            return
        self.closing.set()
        try:
            self.process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        self.process.stdout.close()
        self.reader.join(timeout=.2)


def _session_id(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError('원래 AGY 세션 UUID가 필요합니다.') from None


def _cwd(value):
    # AGY's Windows metadata can expose a URI-style drive path (/C:/work).
    # Convert only that local-drive form; arbitrary URIs and rooted paths remain invalid.
    if os.name == 'nt' and isinstance(value, str) and re.match(r'^/[A-Za-z]:[/\\]', value):
        value = value[1:]
    if (not isinstance(value, str) or not value or value.replace('\\', '/').startswith('//')
            or not Path(value).is_absolute() or '\x00' in value):
        raise ValueError('AGY 작업의 절대 경로가 필요합니다.')
    return os.path.normcase(os.path.normpath(value))


def _rows(client, *, recent=False, deadline=None):
    from .sessions import code, timestamp
    if not client.supports('list'):
        raise RuntimeError('설치된 AGY는 세션 목록 조회를 지원하지 않습니다.')
    deadline = deadline if deadline is not None else time.monotonic() + 25
    rows = {}
    cursor = None
    cursors = set()
    limit = 300 if recent else 100
    for _ in range(10):
        page = client.call('session/list', {'cursor': cursor} if cursor else {},
                           timeout=min(25, deadline-time.monotonic()))
        if not isinstance(page.get('sessions'), list):
            raise RuntimeError('AGY 세션 목록 응답 형식을 확인할 수 없습니다.')
        for item in page['sessions']:
            if not isinstance(item, dict):
                raise RuntimeError('AGY 세션 항목 형식을 확인할 수 없습니다.')
            try:
                session_id = _session_id(item.get('sessionId'))
                cwd = _cwd(item.get('cwd'))
            except ValueError:
                continue
            reachable = client.supports('resume')
            row = {'provider': 'agy', 'session_id': session_id, 'code': code('agy', session_id),
                   'name': item.get('title') if isinstance(item.get('title'), str) and item['title'].strip()
                           else '제목 없는 작업 · ' + session_id[:8],
                   'cwd': cwd, 'project': Path(cwd).name, 'updated_at': timestamp(item.get('updatedAt')),
                   'live': False, 'reachable': reachable, 'state': '재개 가능' if reachable else '재개 미지원',
                   'model': None, 'effort': None, 'model_source': None, 'effort_source': None, 'client': 'Antigravity CLI'}
            if session_id in rows and _cwd(rows[session_id]['cwd']) != _cwd(row['cwd']):
                raise RuntimeError('동일 AGY 세션의 작업 경로가 모호합니다.')
            rows[session_id] = row
            if len(rows) >= limit:
                break
        next_cursor = page.get('nextCursor')
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise RuntimeError('AGY 페이지 응답 형식을 확인할 수 없습니다.')
        if not next_cursor or next_cursor in cursors or not page['sessions'] or len(rows) >= limit:
            break
        cursors.add(next_cursor)
        cursor = next_cursor
    return list(rows.values())


def sessions(recent=False):
    deadline = time.monotonic() + 30
    with AgyClient() as client:
        rows = _rows(client, recent=recent, deadline=deadline)
    return rows, '저장된 AGY 대화입니다. 선택한 대화를 ACP로 재개하며 기존 터미널의 실행 상태는 확인하지 않습니다.'


def _validate(origin, client, *, deadline=None):
    session_id = _session_id(origin.get('session_id'))
    cwd = _cwd(origin.get('cwd'))
    matches = [row for row in _rows(client, recent=True, deadline=deadline) if row['session_id'] == session_id]
    if len(matches) != 1 or _cwd(matches[0]['cwd']) != cwd:
        raise ValueError('AGY 세션 또는 작업 경로가 바뀌었습니다. 목록을 새로고침해 주세요.')
    if not matches[0]['reachable']:
        raise ValueError('설치된 AGY는 기존 대화 재개를 지원하지 않습니다.')
    return matches[0]


def validate(origin):
    deadline = time.monotonic() + 30
    with AgyClient() as client:
        return _validate(origin, client, deadline=deadline)


def _deliver_result(origin, text, request_id):
    from .sessions import receives_notifications
    if not (runtime.ROOT / 'enabled').exists() or not receives_notifications('agy', origin):
        return False
    from .plain_delivery import deliver
    return bool(deliver(text, 'agy-' + request_id, cwd=origin['cwd'],
                        origin={**origin, 'source': 'agy', 'kind': 'result'}))


def run_worker(payload):
    """The caller's unique request is never retried, including after uncertain acceptance."""
    request_id = uuid.UUID(payload['request_id']).hex
    session_id = _session_id(payload['origin'].get('session_id'))
    origin = {**payload['origin'], 'session_id': session_id}
    _cwd(origin.get('cwd'))
    text = payload['text']
    if not isinstance(text, str) or not text.strip():
        raise ValueError('전달할 AGY 메시지가 필요합니다.')
    receipt = {'provider': 'agy', 'session_id': session_id, 'request_id': request_id,
               'worker_pid': os.getpid(), 'accepted': False, 'completed': False, 'stage': 'validating'}

    def save(**fields):
        receipt.update(fields, at=time.time())
        runtime.write_json('agy/' + request_id + '.json', receipt)

    client = None
    response = []
    response_size = 0
    deadline = time.monotonic() + TURN_TIMEOUT
    startup_deadline = min(deadline, time.monotonic() + 25)

    def update(params):
        nonlocal response_size
        if params.get('sessionId') != session_id or not isinstance(params.get('update'), dict):
            return
        item = params['update']
        kind = item.get('sessionUpdate')
        if kind not in ('agent_message_chunk', 'agent_thought_chunk', 'tool_call', 'tool_call_update'):
            return
        if not receipt['accepted']:
            save(accepted=True, stage='running')
        content = item.get('content')
        if kind == 'agent_message_chunk' and isinstance(content, dict) and content.get('type') == 'text':
            part = content.get('text')
            if isinstance(part, str):
                response_size += len(part)
                if response_size > 2_000_000:
                    raise RuntimeError('AGY 응답이 너무 커서 결과 수신을 중지했습니다.')
                response.append(part)

    try:
        with runtime.locked('agy-' + session_id + '.lock', timeout=.1):
            with AgyClient(writable=True) as client:
                row = _validate(origin, client, deadline=startup_deadline)
                client.call('session/resume', {'sessionId': session_id, 'cwd': row['cwd'], 'mcpServers': []},
                            timeout=startup_deadline-time.monotonic())
                save(stage='submitting')
                result = client.call('session/prompt', {'sessionId': session_id,
                    'prompt': [{'type': 'text', 'text': text}]}, timeout=deadline-time.monotonic(), on_update=update)
                reason = result.get('stopReason')
                if reason not in {'end_turn', 'max_tokens', 'max_turn_requests', 'refusal', 'cancelled'}:
                    raise RuntimeError('AGY 종료 상태를 확인할 수 없습니다.')
                note = LIMITATION if client.permission_cancelled or client.unsupported_request else ''
                save(accepted=True, completed=True, stage='completed' if reason == 'end_turn' else 'stopped',
                     stop_reason=reason, resume_warning=bool(note) or reason != 'end_turn', note=note)
        body = ''.join(response).strip() or 'AGY 응답에 표시할 본문이 없습니다.'
        if receipt.get('note'):
            body += '\n\n' + receipt['note']
        if receipt.get('stop_reason') != 'end_turn':
            body += '\n\nAGY 작업이 완료 전에 중단됐습니다. 원래 작업을 확인해 주세요.'
        try:
            save(result_delivered=_deliver_result(origin, body, request_id))
        except Exception as error:
            # Delivery may have succeeded before its verification failed. Never resend.
            save(result_delivered=False, delivery_error=type(error).__name__)
    except Exception as error:
        note = LIMITATION if client and (client.permission_cancelled or client.unsupported_request) else ''
        save(completed=True, stage='failed', error_type=type(error).__name__, resume_warning=True,
             note=note or 'AGY 처리 상태를 확인하지 못했습니다. 원래 작업을 확인하고 자동 재전송하지 마세요.')
        if receipt['accepted']:
            try:
                save(result_delivered=_deliver_result(origin, receipt['note'], request_id))
            except Exception:
                save(result_delivered=False)
    return receipt


def forward(origin, text):
    origin = {**origin, 'session_id': _session_id(origin.get('session_id'))}
    _cwd(origin.get('cwd'))
    if not isinstance(text, str) or not text.strip():
        raise ValueError('전달할 AGY 메시지가 필요합니다.')
    request_id = uuid.uuid4().hex
    name = 'agy/' + request_id + '.json'
    payload = {'home': str(runtime.ROOT), 'request_id': request_id, 'origin': origin, 'text': text}
    process = subprocess.Popen(runtime.module_command('agy_transport'), stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
        env=dict(os.environ, TALKTURN_HOME=str(runtime.ROOT), PYTHONUTF8='1'), creationflags=runtime.NO_WINDOW)
    failures = []

    def write():
        try:
            process.stdin.write(json.dumps(payload, ensure_ascii=False))
            process.stdin.close()
        except (OSError, ValueError) as error:
            failures.append(type(error).__name__)

    threading.Thread(target=write, daemon=True).start()
    deadline = time.monotonic() + ACK_TIMEOUT
    while time.monotonic() < deadline:
        finished = process.poll() is not None
        receipt = runtime.read_json(name, {})
        if isinstance(receipt, dict) and receipt.get('request_id') == request_id and receipt.get('session_id') == origin['session_id']:
            if receipt.get('accepted') is True:
                return receipt
            if receipt.get('completed'):
                raise RuntimeError(receipt.get('note') or NO_RETRY)
        if finished or failures:
            raise RuntimeError(NO_RETRY)
        time.sleep(.1)
    # The worker may already be processing the prompt. Let it finish and deliver its
    # result; never terminate someone else's session or launch a replacement retry.
    raise TimeoutError(NO_RETRY)


def main():
    sys.stdin.reconfigure(encoding='utf-8')
    payload = json.load(sys.stdin)
    home = Path(payload['home'])
    if not home.is_absolute():
        raise ValueError('절대 TalkTurn 상태 경로가 필요합니다.')
    runtime.ROOT = home.resolve()
    os.environ['TALKTURN_HOME'] = str(runtime.ROOT)
    run_worker(payload)


if __name__ == '__main__':
    main()
