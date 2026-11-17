"""Attach to discoverable work; launch an interactive CLI only on user request."""
import os
import subprocess
from pathlib import Path
from . import sessions
from .runtime import executable


def connected(rows,provider):
    return [row for row in rows if row['provider']==provider and
            (row.get('reachable') is True if provider in ('claude','agy') else row.get('live') is True)]


def connect(provider):
    if provider not in ('claude','codex','agy'):
        raise ValueError('connect claude, connect codex 또는 connect agy')
    catalog=sessions.discover()
    ready=connected(catalog['rows'],provider)
    if ready:
        if provider=='agy':
            return f'AGY · 기존 {len(ready)}개 대화를 같은 ID로 재개할 수 있습니다. 세션 선택에서 체크하세요.'
        return f'{provider} · 기존 {len(ready)}개 작업 자동 연결 확인. 세션 선택에서 체크하세요.'
    environment=dict(os.environ)
    environment.pop('CLAUDECODE',None)
    subprocess.Popen([executable(provider)],cwd=str(Path.cwd()),env=environment,
                     creationflags=getattr(subprocess,'CREATE_NEW_CONSOLE',0))
    return f'{provider} 터미널을 열었습니다. 필요한 로그인·작업 시작 후 TalkTurn에서 Enter로 새로고침하세요.'


def menu(console):
    from .terminal import response
    catalog=sessions.discover()
    names=[('codex','Codex'),('claude','Claude Code'),('agy','AGY · Antigravity')]
    lines=['AI 연결 추가']
    for index,(provider,label) in enumerate(names,1):
        count=len(connected(catalog['rows'],provider))
        summary = f'{count}개 같은 ID로 재개 가능' if provider == 'agy' else f'{count}개 자동 연결됨'
        lines.append(f'{index}. {label} · '+(summary if count else '연결 없음 · 새 터미널 열기'))
    response(console,'\n'.join(lines))
    while True:
        choice=console.input('번호 선택 [Enter: 취소]: ').strip()
        if not choice:
            return '연결 추가 취소.'
        if choice in ('1','2','3'):
            return connect(names[int(choice)-1][0])
        console.print('1, 2 또는 3을 입력하세요.',style='grey74')
