"""Completion hooks: exact final answer + explicit local image attachments."""
import argparse
from datetime import datetime
import hashlib
import json
import subprocess
import sys
import os
from pathlib import Path
from .runtime import ROOT, NO_WINDOW, read_json, write_json


def notice(source, payload):
    if payload.get('hook_event_name') != 'Stop' or payload.get('agent_id') or payload.get('stop_hook_active'):
        return None
    result = payload.get('last_assistant_message')
    if not isinstance(result, str) or not result.strip():
        return None
    identifier = payload.get('turn_id') or result
    key = hashlib.sha256(f"{source}|{payload.get('session_id','')}|{identifier}".encode()).hexdigest()
    return result.strip(), key


def main():
    global ROOT
    if hasattr(sys.stdin, 'reconfigure'):
        sys.stdin.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['hook','codex-notify','event'])
    parser.add_argument('notification', nargs='?')
    parser.add_argument('--source', default='claude', choices=['claude','codex'])
    parser.add_argument('--home')
    args = parser.parse_args()
    if args.home:
        os.environ['TALKTURN_HOME'] = args.home
        from . import runtime
        runtime.ROOT = ROOT = Path(args.home).resolve()
    try:
        if args.command == 'codex-notify':
            if os.environ.get('TALKTURN_NOTIFY_CHAINED'):
                # The saved previous notify can wrap this hook (Codex computer use stores
                # `turn-ended --previous-notify [talkturn.notify …]`): it calls back here, and without
                # this stop the two relaunched each other for ever, once per finished Codex turn.
                print('{}')
                return
            previous = read_json('previous-notify.json', [])
            if previous:
                try:
                    subprocess.Popen(previous + [args.notification or '{}'], stdin=subprocess.DEVNULL,
                                     env={**os.environ, 'TALKTURN_NOTIFY_CHAINED': '1'},
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
                except OSError:
                    pass
            event = json.loads(args.notification or '{}')
            args.source = 'codex'
            payload = {'hook_event_name': 'Stop' if event.get('type') == 'agent-turn-complete' else '',
                       'session_id': event.get('thread-id',''), 'turn_id': event.get('turn-id',''),
                       'cwd': event.get('cwd',''), 'model': event.get('model',''),
                       'last_assistant_message': event.get('last-assistant-message','')}
        else:
            payload = json.load(sys.stdin)
        if (ROOT/'enabled').exists():
            from .tasks import record
            if args.command=='event':
                states={'SessionStart':'시작됨','UserPromptSubmit':'진행 중','SessionEnd':'종료됨'}
                state=states.get(payload.get('hook_event_name'))
                if state:
                    record(args.source,payload,state)
                print('{}')
                return
            item = notice(args.source, payload)
            if item:
                record(args.source,payload,'완료')
                from .sessions import receives_notifications
                if not receives_notifications(args.source,payload):
                    print('{}')
                    return
                from .plain_delivery import deliver
                deliver(*item, cwd=str(payload.get('cwd','')), origin={
                    'source': args.source, 'session_id': payload.get('session_id',''), 'kind':'result',
                    'cwd': payload.get('cwd',''), 'model': payload.get('model','')})
    except Exception as error:
        ROOT.mkdir(parents=True, exist_ok=True)
        write_json('hook-status.json', {'error': type(error).__name__, 'at': datetime.now().isoformat()})
    print('{}')


if __name__ == '__main__':
    main()
