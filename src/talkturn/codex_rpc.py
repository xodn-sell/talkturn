"""Read-only Codex queries and a separate shared-server attachment queue."""
from collections import deque
import json
import queue
import subprocess
import threading
import time
import os
from pathlib import Path
import socket
import websocket
from .runtime import executable, NO_WINDOW


class ProxySocket:
    """Socket-compatible bytes over Codex's supported stdio proxy (Windows AF_UNIX)."""
    def __init__(self,path):
        self.process=subprocess.Popen([executable('codex'),'app-server','proxy','--sock',str(path)],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,bufsize=0,creationflags=NO_WINDOW)
        self.timeout=5
        self.buffer=b''
        self.chunks=queue.Queue()
        def read():
            try:
                while True:
                    chunk=os.read(self.process.stdout.fileno(),65536)
                    self.chunks.put(chunk)
                    if not chunk:
                        break
            except (OSError,ValueError):
                self.chunks.put(b'')
        threading.Thread(target=read,daemon=True).start()

    def settimeout(self,value):
        self.timeout=value

    def gettimeout(self):
        return self.timeout

    def send(self,data):
        count=self.process.stdin.write(data)
        self.process.stdin.flush()
        return count

    def recv(self,size):
        if not self.buffer:
            try:
                self.buffer=self.chunks.get(timeout=self.timeout)
            except queue.Empty:
                raise socket.timeout('Codex proxy read timed out') from None
        result,self.buffer=self.buffer[:size],self.buffer[size:]
        return result

    def shutdown(self,*_):
        self.close()

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        self.process.stdin.close()
        self.process.stdout.close()


class CodexReader:
    require_shared=False

    def __init__(self,prefer_shared=False):
        self.prefer_shared=prefer_shared
        self.shared=False
        self.ws=None
        self.process=None

    def __enter__(self):
        self.messages = queue.Queue()
        self.inbox = deque(maxlen=200)  # server notifications/requests seen while waiting for responses
        self.sequence = 0
        if self.prefer_shared:
            raw=None
            try:
                path=Path(os.environ.get('CODEX_HOME',Path.home()/'.codex'))/'app-server-control/app-server-control.sock'
                if hasattr(socket,'AF_UNIX'):
                    raw=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
                    raw.settimeout(3)
                    raw.connect(str(path))
                else:
                    raw=ProxySocket(path)
                self.ws=websocket.create_connection('ws://localhost/',socket=raw,timeout=5,suppress_origin=True)
                self.shared=True
            except (OSError,websocket.WebSocketException):
                if raw:
                    raw.close()
                self.ws=None
        if not self.ws:
            if self.require_shared:
                raise RuntimeError('원래 Codex 실행 서버에 연결할 수 없어 첨부를 보내지 않았습니다.')
            self.process = subprocess.Popen([executable('codex'), 'app-server', '--stdio'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding='utf-8', creationflags=NO_WINDOW)
        def read():
            for line in self.process.stdout:
                try:
                    self.messages.put(json.loads(line))
                except ValueError:
                    pass
            self.messages.put({'_closed': True})
        if self.process:
            threading.Thread(target=read, daemon=True).start()
        try:
            self.call('initialize', {'clientInfo': {'name': 'talkturn', 'version': '0.1.0'},
                                     'capabilities': {'experimentalApi': True}})
            self.send({'method': 'initialized'})
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def send(self, message):
        if self.ws:
            try:
                self.ws.send(json.dumps(message))
            except (websocket.WebSocketException,OSError):
                raise RuntimeError('Codex 실행 서버 연결이 종료됨.') from None
            return
        self.process.stdin.write(json.dumps(message) + '\n')
        self.process.stdin.flush()

    def call(self, method, params=None, timeout=25):
        if method not in {'initialize', 'account/read', 'account/rateLimits/read', 'account/usage/read', 'model/list',
                          'thread/list','thread/read','thread/loaded/list'}:
            raise ValueError('Read-only method required')
        return self._request(method,params,timeout)

    def _receive(self, deadline):
        try:
            remaining=max(.01,deadline-time.monotonic())
            if self.ws:
                self.ws.settimeout(remaining)
                raw=self.ws.recv()
                if not raw:
                    raise RuntimeError('Codex 조회 연결 종료')
                message=json.loads(raw)
            else:
                message = self.messages.get(timeout=remaining)
        except (queue.Empty,websocket.WebSocketTimeoutException):
            raise TimeoutError('Codex 사용량 조회 시간 초과') from None
        except (websocket.WebSocketException,OSError):
            raise RuntimeError('Codex 실행 서버 연결이 종료됨.') from None
        except (ValueError,TypeError):
            raise RuntimeError('Codex 조회 응답 형식을 확인할 수 없음.') from None
        if not isinstance(message,dict):
            raise RuntimeError('Codex 조회 응답 형식을 확인할 수 없음.')
        if message.get('_closed'):
            raise RuntimeError('Codex 조회 연결 종료')
        return message

    def _request(self, method, params=None, timeout=25):
        self.sequence += 1
        request_id = self.sequence
        self.send({'id': request_id, 'method': method, 'params': params or {}})
        deadline = time.monotonic() + timeout
        while True:
            message=self._receive(deadline)
            if 'method' in message:
                # Server requests carry their own small ids; never take one for our response.
                self.inbox.append(message)
            elif message.get('id') == request_id:
                if 'error' in message:
                    error=message['error']
                    if not isinstance(error,dict):
                        raise RuntimeError('Codex 조회 오류 응답 형식을 확인할 수 없음.')
                    raise RuntimeError(f'Codex {method} 미지원 또는 조회 실패 ({error.get("code")})')
                if 'result' not in message:
                    raise RuntimeError('Codex 조회 결과가 응답에 없음.')
                return message['result']
            if time.monotonic() >= deadline:
                raise TimeoutError('Codex 사용량 조회 시간 초과')

    def __exit__(self, *_):
        if self.ws:
            try:
                self.ws.close(timeout=1)
            except (OSError,websocket.WebSocketException):
                pass
            self.ws=None
        if not self.process:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process.stdin.close()
        self.process.stdout.close()


class CodexAttachmentWriter(CodexReader):
    """Queue attachments on the existing daemon; never launch/resume a replacement."""
    require_shared=True

    def __init__(self):
        super().__init__(prefer_shared=True)

    def enqueue(self, thread_id, inputs, event_id):
        if not self.shared or not self.ws:
            raise RuntimeError('기존 Codex 공유 연결이 필요합니다.')
        result=self.call('thread/read',{'threadId':thread_id,'includeTurns':False})
        thread=result.get('thread',{})
        if (thread.get('id')!=thread_id or thread.get('ephemeral') or thread.get('parentThreadId')
                or thread.get('canAcceptDirectInput') is False):
            raise RuntimeError('원래 Codex 작업을 확인할 수 없어 첨부를 보내지 않았습니다.')
        result=self._request('thread/queue/add',{
            'threadId':thread_id,'clientUserMessageId':event_id,'input':inputs})
        submission=result.get('queuedSubmission',{}) if isinstance(result,dict) else {}
        queued_id=submission.get('id') if isinstance(submission,dict) else None
        receipt={'provider':'codex','session_id':thread_id,'accepted':True,
                 'client_user_message_id':event_id,'resume_warning':False}
        if not isinstance(queued_id,str) or not queued_id:
            receipt.update(resume_warning=True,note='큐 접수 응답을 받았지만 항목 ID가 없어 시작하지 않았습니다. 자동 재전송하지 마세요.')
            return receipt
        receipt['queued_submission_id']=queued_id
        try:
            self._request('thread/queue/start',{'threadId':thread_id,'queuedSubmissionId':queued_id})
        except Exception:
            # Queue acceptance is already durable; a start failure must not queue it again.
            receipt.update(resume_warning=True,note='기존 작업 큐에 접수됐습니다. 실행 상태는 원래 창에서 확인하세요. 자동 재전송하지 마세요.')
        return receipt


class CodexQuestionAnswerer(CodexReader):
    """Answer a waiting request_user_input on the shared daemon; thread/resume replays it to this client."""
    require_shared=True

    def __init__(self):
        super().__init__(prefer_shared=True)

    def answer(self, thread_id, choose, timeout=15):
        """`choose(questions)` returns the answers map or raises. True once Codex reports it resolved."""
        thread=self.call('thread/read',{'threadId':thread_id,'includeTurns':False}).get('thread',{})
        status=thread.get('status') if isinstance(thread.get('status'),dict) else {}
        # Resuming a thread that is not running on this server would load a second copy of it.
        if (thread.get('id')!=thread_id or status.get('type')!='active'
                or 'waitingOnUserInput' not in status.get('activeFlags',[])):
            raise LookupError('No waiting Codex question on the shared server')
        self._request('thread/resume',{'threadId':thread_id,'excludeTurns':True})
        deadline=time.monotonic()+timeout
        def find(test):
            while True:
                found=next((item for item in self.inbox if test(item)),None)
                if found or time.monotonic()>=deadline:
                    return found
                try:
                    self.inbox.append(self._receive(deadline))
                except TimeoutError:
                    return None
        request=find(lambda item:item.get('method')=='item/tool/requestUserInput' and 'id' in item
                     and (item.get('params') or {}).get('threadId')==thread_id)
        if not request:
            raise LookupError('Codex did not replay the waiting question')
        self.send({'id':request['id'],'result':{'answers':choose(request['params'].get('questions') or [])}})
        deadline=time.monotonic()+5
        return bool(find(lambda item:item.get('method')=='serverRequest/resolved'
                         and (item.get('params') or {}).get('requestId')==request['id']
                         and item['params'].get('threadId')==thread_id))
