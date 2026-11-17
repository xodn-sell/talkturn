"""Keyboard account workflows using ccswap's own account operations."""
import subprocess
from rich.text import Text
from . import providers
from .runtime import executable
from .terminal import choice_menu


def switch(console,provider='claude'):
    if provider not in ('claude','codex'):
        raise ValueError('계정 종류는 claude 또는 codex입니다.')
    rows=providers.claude_usage()['accounts'] if provider=='claude' else providers.accounts()['codex']
    if not rows:
        return '등록된 계정이 없습니다. A로 계정을 추가하세요.'
    choices=[]
    for row in rows:
        label=Text(f"{'●' if row['active'] else '○'} {row['number']}. {row['label']}",style='green' if row['active'] else 'white')
        if row['disabled']: label.append(' · 사용 중지',style='dim')
        elif row['active']: label.append(' · 현재 계정',style='green')
        for limit in row.get('limits',[])[:2]:
            pct=limit['remaining_pct']
            label.append(f"  {limit['name']} ",style='grey70')
            label.append('미확인' if pct is None else '━'*round(pct/20)+'·'*(5-round(pct/20))+f' {pct:g}%',style='green' if pct is not None and pct>20 else 'grey74')
        if row.get('state')=='cached': label.append(' · 이전 조회값',style='grey62')
        choices.append((row,label))
    chosen=choice_menu(console,f'{provider} · 계정 전환하기',choices,
        selected=next((i for i,row in enumerate(rows) if row['active']),0),subtitle='↑↓ 이동 · Enter 전환 · Esc 뒤로')
    if chosen is None: return '계정 전환 취소.'
    if chosen['disabled']: return '사용 중지된 계정입니다. 활성 계정을 선택하세요.'
    if chosen['active']: return f"{chosen['label']} · 이미 사용 중인 계정입니다."
    console.print(f"{chosen['label']} 계정으로 전환하고 활성 상태를 확인합니다…",markup=False)
    result=providers.switch_account(provider,chosen['number'],expected_label=chosen['label'])
    return f"{chosen['label']} 계정으로 전환 확인.\n"+result['scope']


def add(console,provider='claude'):
    if provider not in ('claude','codex'):
        raise ValueError('계정 종류는 claude 또는 codex입니다.')
    mode=choice_menu(console,f'{provider} · 계정 추가하기',[
        ('login','새 계정 로그인 후 추가'),('current','현재 로그인 계정 등록')])
    if mode is None: return '계정 추가 취소.'
    before=providers.accounts().get(provider,[])
    if mode=='login':
        if providers.login_state(provider):
            console.print('1/3 · 현재 로그인 계정을 보관합니다…',markup=False)
            providers.add_account(provider)
        console.print('2/3 · 로그인 창에서 추가할 계정을 선택하세요. 완료 후 이 터미널로 돌아옵니다.',markup=False)
        command=[executable(provider)]+(['auth','login'] if provider=='claude' else ['login'])
        import os
        environment=dict(os.environ)
        environment.pop('CLAUDECODE',None)
        try:
            result=subprocess.run(command,env=environment)
        except KeyboardInterrupt:
            return '로그인 취소. 보관한 기존 계정은 S → 계정 전환하기에서 선택할 수 있습니다.'
        if result.returncode:
            return '로그인이 완료되지 않아 추가하지 않았습니다. 저장한 기존 계정은 S에서 선택할 수 있습니다.'
        if not providers.login_state(provider):
            return '로그인이 확인되지 않아 등록하지 않았습니다. 다시 로그인하세요.'
    elif not providers.login_state(provider):
        return '현재 로그인 계정이 없습니다. A → 새 계정 로그인 후 추가를 선택하세요.'
    alias=None
    if provider=='claude':
        while True:
            alias=console.input('계정 별칭 [Enter: 기존 이름 유지]: ').strip()
            if len(alias)<=40 and all(ord(char)>=32 and ord(char)!=127 for char in alias):
                break
            console.print('별칭은 제어문자 없이 40자 이내로 입력하세요.',markup=False)
    console.print('3/3 · 계정 등록과 활성 상태를 확인합니다…' if mode=='login' else '현재 로그인 계정을 등록하고 확인합니다…',markup=False)
    account=providers.add_account(provider,alias=alias or None)
    updated=any(row['number']==account['number'] for row in before)
    return f"{account['number']}번 {account['label']} · "+('이미 등록된 계정이라 정보를 갱신했습니다.' if updated else '계정 추가 완료.')+'\nS를 누르면 계정 목록에서 전환할 수 있습니다.'
