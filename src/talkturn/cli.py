"""TalkTurn terminal entry point."""
import argparse
import json
import os
import subprocess
import sys
import time
from rich.console import Console
from rich.panel import Panel
from . import __version__
from .commands import parse, execute, HELP, selected_section, select_section, SECTION_LABELS, SECTION_MENU, AGY_ACCOUNT_NOTICE
from .runtime import ROOT, executable, read_json, write_json, locked

console = Console(highlight=False)
RELAY_MODELS={'1':'gpt-6-luna','luna':'gpt-6-luna','gpt-6-luna':'gpt-6-luna',
              '2':'claude-haiku-5-5','haiku':'claude-haiku-5-5','claude-haiku-5-5':'claude-haiku-5-5'}


def choose_relay_model():
    current=read_json('delivery.json',{}).get('model','gpt-6-luna')
    from .terminal import choice_menu
    choices=[('gpt-6-luna','GPT-6 Luna · Codex 로그인 및 사용량 적용'),
             ('claude-haiku-5-5','Claude Haiku 5.5 · Claude 로그인 및 사용량 적용')]
    return choice_menu(console,'카톡 답장을 어느 작업으로 보낼지 판단할 AI',choices,
                       selected=1 if current=='claude-haiku-5-5' else 0)


def needs_relay_choice(settings):
    return (not settings.get('model') and settings.get('reply_mode')!='manual'
            and settings.get('reply_routing_enabled',True))


def confirm_connection():
    if os.name!='nt' or not sys.stdin.isatty():
        return True
    from .mouse_lock import HOLD,active
    if active():
        return True
    from .terminal import choice_menu
    warning=('연결하면 이 PC의 마우스와 터치패드가 잠깁니다.\n'
             f'Esc를 {HOLD:g}초간 누르면 잠금이 풀리고 카톡 연결이 종료됩니다.')
    return bool(choice_menu(console,'카카오톡에 연결할까요?',[(True,warning)],subtitle='Enter 연결 · Esc 취소'))


def save_relay(model):
    with locked('settings.lock'):
        data=read_json('delivery.json',{})
        data['model']=model
        write_json('delivery.json',data)


def ask_computer_name():
    from .computer import computer_name
    current=computer_name()
    try:
        value=console.input(f'컴퓨터 이름 [Enter: {current}]: ',markup=False)
    except (KeyboardInterrupt,EOFError):
        return None
    return value or current


def edit_computer_name():
    from .computer import computer_label,set_computer_name
    value=ask_computer_name()
    if value is None: return '컴퓨터 이름 변경 취소.'
    set_computer_name(value)
    return computer_label()


def setup(args, *, commit_selection=None):
    from . import service
    parser = argparse.ArgumentParser(prog='talkturn setup')
    parser.add_argument('--self-chat', help='사용자가 직접 연 나와의 채팅의 정확한 창 이름')
    parser.add_argument('--migrate', action='store_true')
    parser.add_argument('--no-startup', action='store_true')
    parser.add_argument('--relay-model',type=str.lower,choices=['luna','haiku','gpt-6-luna','claude-haiku-5-5'],
                        help='답장 중계 모델. 새 설정 기본값: gpt-6-luna')
    parser.add_argument('--section',type=str.lower,choices=['claude','codex','chatgpt','agy','antigravity','안티그래비티','all'],help='처음 표시할 AI 섹션')
    parser.add_argument('--computer-name',help='TalkTurn에 표시할 이 컴퓨터의 이름 (기본: 내 PC)')
    options = parser.parse_args(args)
    existing=read_json('delivery.json',{})
    title = options.self_chat
    if not options.migrate:
        reuse=not title and existing.get('window_id') and not service.delivery_issues(include_worker=False)
        if reuse:
            title=existing['room_name']
            console.print('기존 나와의 채팅 연결을 사용합니다.',markup=False)
        if not title:
            if not sys.stdin.isatty():
                raise ValueError('talkturn setup --self-chat "나와의 채팅 창 이름"')
            console.print('카카오톡에서 본인의 ‘나와의 채팅’을 별도 창으로 열어 주세요. 연결 후 talkturn in을 전송합니다.', markup=False)
            rows = service.chat_windows()
            if not rows:
                return '나와의 채팅 창이 없습니다. 별도 창을 연 뒤 setup을 다시 실행하세요.'
            for index, row in enumerate(rows, 1):
                console.print(f"  {index}. {row['title']}",markup=False)
            answer = input('나와의 채팅 번호 (취소: Enter): ').strip()
            if not answer:
                return '연결 취소.'
            if not answer.isdigit() or not 1 <= int(answer) <= len(rows):
                raise ValueError('목록의 번호 입력 필요.')
            title = rows[int(answer)-1]['title']
    relay_model=RELAY_MODELS.get(options.relay_model)
    if not relay_model and needs_relay_choice(existing) and sys.stdin.isatty():
        relay_model=choose_relay_model()
        if relay_model is None: return '설정 취소.'
    # The first connection keeps the default '내 PC'; N or `computer-name` renames it later.
    name=options.computer_name
    if not confirm_connection():
        return '연결 취소.'
    if options.migrate:
        service.migrate_legacy()
    if name is not None:
        from .computer import set_computer_name
        set_computer_name(name)
    if options.migrate:
        service.pair(read_json('delivery.json',{})['room_name'])
    elif not reuse:
        service.pair(title)
    if relay_model is not None:
        save_relay(relay_model)
    if options.section:
        select_section(options.section)
    service.install_hooks()
    if options.section in ('agy','antigravity','안티그래비티'):
        from .agy_hooks import install
        install()
    service.startup(not options.no_startup)
    if commit_selection is not None:
        commit_selection()  # The first verified connection message lists the newly chosen work.
    result = service.start()
    if options.migrate:
        service.bridge_legacy_hook()
    settings=read_json('delivery.json',{})
    relay=settings.get('model','gpt-6-luna')
    if not settings.get('reply_routing_enabled',True):
        relay='답장 전달 꺼짐'
    elif settings.get('reply_mode')=='manual':
        relay='작업 번호로 전달'
    return result+'\n답장 중계: '+relay


def dispatch(parts):
    if parts==['setup-agy']:
        executable('agy')
        from .agy_hooks import install
        install()
        return 'Antigravity 완료 알림 훅을 설치했습니다. agy에서 작업을 연 뒤 TalkTurn에서 G- 세션을 체크하세요.'
    if parts[0] in ('account-add','switch') and len(parts)>1 and parts[1]=='agy':
        raise ValueError(AGY_ACCOUNT_NOTICE)
    if parts[0]=='account-add' and len(parts)<=2 and sys.stdin.isatty():
        from .account_menu import add
        return add(console,parts[1] if len(parts)==2 else 'claude')
    if parts[0]=='switch' and len(parts)<=2 and sys.stdin.isatty():
        from .account_menu import switch
        return switch(console,parts[1] if len(parts)==2 else 'claude')
    if parts[0]=='connect' and len(parts)<=2:
        from .connections import connect,menu
        if len(parts)==2:
            return connect(parts[1])
        if not sys.stdin.isatty():
            return 'AI 연결 추가: talkturn connect codex / claude / agy'
        return menu(console)
    if parts[0]=='ui':
        return '웹 화면은 중지했습니다. talkturn을 실행해 터미널에서 사용하세요.'
    if parts[0]=='account-add' and len(parts)==2:
        from .providers import add_account
        account=add_account(parts[1])
        return f"{parts[1]} · {account['number']}번 {account['label']} 등록 확인."
    if parts[0] == 'setup':
        return setup(parts[1:])
    if parts==['start'] and not confirm_connection():
        return '연결 취소.'
    if parts == ['uninstall']:
        from .service import uninstall
        return uninstall()
    if parts[0] in ('codex','claude','agy'):
        # Interactive launch stays in the user's terminal. Arguments remain argv, never shell text.
        result = subprocess.run([executable(parts[0]),*parts[1:]])
        return f'{parts[0]} 종료 코드 {result.returncode}'
    if parts[0]=='update' and len(parts)<=2:
        from .updater import update
        return update(console,parts[1] if len(parts)==2 else None)
    if parts[0] == 'startup' and len(parts)==2 and parts[1] in ('on','off'):
        from .service import startup
        startup(parts[1]=='on')
        return '로그인 시 자동 시작: '+parts[1]
    return execute(parts)


def interactive():
    from .terminal import show_dashboard,response,keyboard_pick,page_rows
    from .sessions import choose,selected,selection_text
    fresh=True

    def load(render=False):
        # Until this run's first Enter nothing is pre-checked: old checks (often ended, unlisted
        # sessions) were invisible and got connected along with the new ones. Saved state is untouched.
        loaded=show_dashboard(console,render=render)
        if fresh and loaded.get('watched'):
            loaded['action_notice']=f"지금 카톡 연결 {len(loaded['watched'])}개 · Enter 시 이번에 체크한 작업만 연결"
            loaded['watched']=[]
        return loaded

    view=load()

    def pick(value):
        if value.isdigit():
            index=int(value)-1
            visible,_,_=page_rows(view)
            if not 0<=index<len(visible):
                raise ValueError('표에 있는 세션 번호를 선택해 주세요.')
            value=visible[index]['code']
            return choose(value,{'rows':view['rows'],'fetched_at':time.time()})
        return choose(value)

    def accept(value):
        nonlocal view
        if isinstance(value,dict):
            provider=value.get('provider',view.get('active_provider','claude'))
            try:
                if value['action']=='connect':
                    from .connections import menu
                    message=menu(console)
                elif value['action']=='relay':
                    model=choose_relay_model()
                    if model: save_relay(model)
                    message='중계 모델 저장 완료.' if model else '모델 변경 취소.'
                elif value['action']=='computer-name':
                    message=edit_computer_name()
                elif value['action']=='setup':
                    model=read_json('delivery.json',{}).get('model')
                    message=setup(['--relay-model',model] if model in ('gpt-6-luna','claude-haiku-5-5') else [])
                elif value['action']=='refresh':
                    message='작업 목록과 카톡 상태를 새로고침했습니다.'
                elif provider=='agy' and value['action'] in ('account-add','switch'):
                    message=AGY_ACCOUNT_NOTICE
                else:
                    from .account_menu import add,switch
                    message=(add if value['action']=='account-add' else switch)(console,provider)
            except (ValueError,RuntimeError,OSError,TimeoutError) as error:
                message=str(error)
            response(console,message)
            if value['action']!='computer-name' and message not in ('계정 전환 취소.','계정 추가 취소.','모델 변경 취소.','연결 추가 취소.'):
                view=load()
            view.update({'active_provider':provider,'draft_checked_codes':value.get('checked_codes',[]),
                         'action_notice':message.splitlines()[0] if message else ''})
            return False
        elif isinstance(value,list):
            from .sessions import watch_many,watched
            from .service import delivery_issues,selection_update
            previous=watched()
            view['draft_checked_codes']=list(value)

            def commit_selection():
                nonlocal fresh
                rows=watch_many(value,{'rows':view['rows'],'fetched_at':time.time()})
                fresh=False
                view.update({'watched':rows,'selected':selected(),'action_notice':f'결과 수신 {len(rows)}개 작업 확정'})
                view.pop('draft_checked_codes',None)

            try:
                issues=delivery_issues() if value else []
                view['kakao_issues']=issues
                if value and issues:
                    result=''
                    if not any(issue.startswith(('나와의 채팅 닫힘','카카오톡 꺼짐')) for issue in issues):
                        result=setup([],commit_selection=commit_selection)
                    if not result or result.startswith('나와의 채팅 창이 없습니다'):
                        view['connection_alert']='카톡 ‘나와의 채팅’을 열어주세요'
                        view.pop('action_notice',None)
                    elif 'draft_checked_codes' in view:
                        view['action_notice']=result
                    else:
                        issues=delivery_issues()
                        view['kakao_issues']=issues
                        view['action_notice']+=' · '+(issues[0] if issues else '카톡 연결됨')
                    return False
                if value:
                    settings=read_json('delivery.json',{})
                    model=None
                    if needs_relay_choice(settings):
                        model=choose_relay_model()
                        if model is None:
                            view['action_notice']='모델 선택 취소.'
                            return False
                    if not confirm_connection():
                        view['action_notice']='연결 취소.'
                        return False
                    if model:
                        save_relay(model)
                commit_selection()
                if value:
                    from .mouse_lock import engage
                    view['action_notice']+=' · 카톡 연결됨'
                    if engage():
                        view['action_notice']+=' · 마우스 잠금(Esc 2.5초로 해제·종료)'
                if selection_update(previous):
                    view['action_notice']+=' · talkturn update 전송 확인'
            except (ValueError,RuntimeError,OSError,TimeoutError) as error:
                prefix='작업 선택을 저장했습니다. ' if 'draft_checked_codes' not in view else '작업 선택을 확정하지 못했습니다. '
                view['action_notice']=prefix+str(error)
            return False
        elif value:
            row=pick(value)
            view.update({'selected':row,'action_notice':f"전송 대상 선택: {row['name']}"})
            return False

    def selection_screen():
        while accept(keyboard_pick(console,view)) is False:
            pass

    while True:
        try:
            selection_screen()
            break
        except (EOFError,KeyboardInterrupt):
            return
        except (ValueError,RuntimeError,OSError,TimeoutError) as error:
            console.print(str(error),style='red',markup=False)
    console.print('명령 입력 후 Enter · Enter만 누르면 메뉴 · K 카톡 연결 · A 계정 추가 · R 새로고침 · quit 종료',style='grey62')
    while True:
        try:
            current=selected()
            label=current['code'] if current else '전송 대상 미선택'
            text = console.input(f'\n[bright_white]명령 ›[/bright_white] [grey62]{label}[/grey62] ').strip()
            if not text:
                view=load()
                selection_screen()
                continue
            shortcut={'a':'account-add','s':'switch','m':'relay','n':'computer-name',
                      'k':'setup','r':'refresh'}.get(text.lower())
            if shortcut:
                accept({'action':shortcut,'provider':view.get('active_provider',(current or {}).get('provider','claude')),
                        'checked_codes':[row['code'] for row in view.get('watched',[])]})
                selection_screen()
                continue
            if text.isdigit() or text.upper().startswith(('C-','X-','G-')) and ' ' not in text:
                response(console,selection_text(pick(text)))
                continue
            parts=parse(text)
            if parts in (['quit'],['exit'],['q']):
                break
            if parts in (['status'],['sessions']):
                view=load(render=True)
                continue
            if parts==['select']:
                view=load()
                selection_screen()
                continue
            if len(parts)==2 and parts[0]=='select':
                response(console,selection_text(pick(parts[1])))
                continue
            response(console,dispatch(parts))
            if parts==['out']:
                break
        except (EOFError, KeyboardInterrupt):
            break
        except (ValueError, RuntimeError, OSError, TimeoutError) as error:
            console.print(f'오류: {error}',style='red',markup=False)


def main():
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    args=sys.argv[1:]
    try:
        if args in (['--version'],['-V']):
            console.print(__version__)
        elif args in (['--help'],['-h']):
            console.print(HELP+'\n\n터미널 전용: connect [claude|codex|agy] / setup-agy / setup [--self-chat 이름] [--relay-model luna|haiku] [--computer-name 이름] [--section claude|chatgpt|agy|all] / account-add claude|codex / start / stop / startup on|off / update [버전] / uninstall / codex / claude / agy [인수]\nAGY 별칭: antigravity / 안티그래비티. AGY 계정 추가·전환은 지원하지 않습니다.',markup=False)
        elif not args:
            if sys.stdin.isatty():
                interactive()
            else:
                console.print(execute(['status'])+'\n\n'+HELP,markup=False)
        else:
            parts=[*args]
            from .commands import ALIASES, PROVIDERS
            parts[0]=ALIASES.get(parts[0].lower(),parts[0])
            if parts[0] in ('tell','computer-name') and len(parts)>1:
                parts=[parts[0],' '.join(parts[1:])]
            if len(parts)>1 and parts[0] in ('account-add','connect','section','sessions','usage','switch','model','models','effort'):
                parts[1]=PROVIDERS.get(parts[1].lower(),parts[1].lower())
            console.print(dispatch(parts),markup=False)
    except (ValueError,RuntimeError,OSError,TimeoutError) as error:
        console.print(f'TalkTurn: {error}',style='red',markup=False)
        raise SystemExit(1)
