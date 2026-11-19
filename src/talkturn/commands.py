"""One command vocabulary shared by terminal and paired Kakao self-chat."""
import json
import re
import sys
from datetime import datetime
from . import providers
from .runtime import ROOT, read_json, write_json, locked

ALIASES = {'도움말':'help','도움':'help','상태':'status','연결':'ping','사용량':'usage','한도':'usage','토큰':'usage',
           '계정':'accounts','전환':'switch','모델':'model','모델목록':'models','설정':'settings',
           '노력도':'effort','작업':'sessions','시작':'start','in':'start','중지':'stop', '종료':'quit',
           '알림':'notifications','답장':'replies','라이선스':'license','질문':'questions','응답':'answer',
           '표시':'display','원문':'original','중계':'router','전달':'send','섹션':'sessions','세션':'sessions',
           '선택':'select','해제':'unselect','체크':'watch','보내':'tell','메시지':'tell','한도알림':'quota-alert','유지':'keep',
           '계정추가':'account-add','계정전환':'switch','계정추가하기':'account-add','계정전환하기':'switch',
           '컴퓨터이름':'computer-name','접수':'receipt','업데이트':'update','화면':'screen','스샷':'screen','스크린샷':'screen','screenshot':'screen',
           'agy':'agy','antigravity':'agy','안티그래비티':'agy'}
PROVIDERS = {'클코':'claude','클로드':'claude','코덱스':'codex','chatgpt':'codex',
             'gpt':'codex','챗지피티':'codex','챗gpt':'codex','중계':'relay','전부':'all','전체':'all',
             'agy':'agy','antigravity':'agy','안티그래비티':'agy'}
SECTION_LABELS = {'claude':'Claude Code','codex':'ChatGPT · Codex','agy':'AGY · Antigravity','all':'전체'}
SECTION_MENU = '1 Claude Code  ·  2 ChatGPT (Codex)  ·  3 전체  ·  4 AGY (Antigravity)'
AGY_ACCOUNT_NOTICE = 'AGY 계정 추가·전환은 TalkTurn에서 지원하지 않습니다. agy에서 직접 관리해 주세요.'
HELP = '''TalkTurn 명령
카톡: 'talkturn in' 말풍선에 답장하면 명령만 쓰면 됨. 예: help, 세션, 화면, out
세션  → Claude Code / Codex / AGY의 실제 작업 목록
화면  → 지금 PC 화면 스샷 (모니터마다 1장)
선택 C-… / X-… / G-…  → 기존 세션 선택
체크 C-… X-… G-…  → 여러 작업의 결과 수신 (체크 none: 모두 해제)
보내 내용  → 선택한 원래 세션에 전달
해제 / 세션 recent
상태 / 연결 / 사용량 [claude|codex|agy] / 계정
접수  → 이번 연결에서 최근 답장을 원래 작업이 받았는지 확인
컴퓨터이름 [이름]  → 현재 PC의 TalkTurn 표시 이름
전환 claude 2  → 등록된 2번 계정
모델  → 현재 기본값
모델목록 codex|claude|agy|relay
모델 claude claude-opus-5-5
모델 codex gpt-6-astra
모델 relay gpt-6-luna
모델 relay claude-haiku-5-5
노력도 codex high / 설정 / 작업
알림 on|off / 답장 on|off
한도알림 on|off / 유지
질문 / 응답 Q1234ABCD 답변
원문 [T번호]  → 이번 연결에서 체크한 작업이 보낸 원문 복사
표시 auto|text|image / 중계 on|off
out  → 연결 끊기 (마우스 잠금도 풀림)
답장 없이 일반 메시지로 보낼 때: 앞에 '톡턴 ' 또는 '/talkturn ' 추가.
예: 톡턴 사용량
작업 결과에서 '답장'하면 원래 세션으로 전달.'''
# The phone gets only what is done from a phone; the full list is one screen too long there.
PHONE_HELP = '''TalkTurn 사용법
작업에 요청 → 그 작업 제목 카드에 답장
아래 명령 → 'talkturn in' 안내에 답장으로 단어만
화면  → PC 화면 스샷
사용량  → 남은 한도
out  → 연결 끊기'''


QUEUED_NOTICE='대기열에 저장됐어요. PC에서 원래 작업을 열어 주세요.'


def forward_response(receipt):
    if receipt.get('stage')=='queued':
        return QUEUED_NOTICE
    return '원래 세션에 전달 접수.'+(' 재개 상태는 원래 창에서 확인 필요.' if receipt.get('resume_warning') else '')


def send_numbered(selector,message):
    if not re.fullmatch(r'T[0-9A-Fa-f]{8}',selector):
        raise ValueError('전달 형식: 톡턴 전달 T1234ABCD 내용')
    matches=[item for item in read_json('result-origins.json',[]) if 'T'+item['id'][:8].upper()==selector.upper()]
    if len(matches)!=1 or matches[0]['origin'].get('request_id'):
        raise ValueError('작업 번호가 없거나 모호함. 톡턴 작업으로 확인.')
    from .reply_bridge import forward
    receipt=forward(matches[0]['origin'],message)
    if not receipt.get('accepted'):
        raise RuntimeError('원래 작업의 접수를 확인할 수 없습니다. 다시 보내기 전에 원래 작업 창을 확인하세요.')
    return receipt


def receipt_text():
    settings=read_json('delivery.json',{})
    connection=read_json('connection.json',{})
    epoch=[connection.get('at') or 0,settings.get('reply_enabled_at') or 0]
    heading='TalkTurn 최근 답장'
    if not epoch[0] or not connection.get('verified'):
        return heading+'\n확인된 카톡 연결이 없습니다.'
    uncertain='접수 미확인. 다시 보내기 전에 PC의 작업 창을 확인하세요.'
    states={
        'queued':QUEUED_NOTICE,
        'accepted':'원래 작업에 접수됐습니다.',
        'not_sent':'전달되지 않았습니다. PC의 작업 창을 확인하세요.',
        'unmatched_reply':'받을 작업을 찾지 못했습니다. 작업 제목에 답장하세요.',
        'duplicate_body':'같은 답장이 겹쳐 전달하지 않았습니다. PC의 작업 창을 확인하세요.',
        'routing_changed':'연결이나 작업 선택이 바뀌어 전달하지 않았습니다.',
        'dispatching':uncertain,
        'forward_failed':uncertain,
    }
    # Keep the last identified reply even when ordinary notes rotate the diagnostic log.
    event=read_json('last-reply-state.json',{})
    if event.get('connection_at')==epoch and event.get('stage') in states:
        stamp=datetime.fromtimestamp(event['time']).strftime('%H:%M:%S')
        return f"{heading} · PC {stamp}\n{states[event['stage']]}"
    return heading+'\n이번 연결의 답장 기록이 없습니다.'


def selected_section():
    value=read_json('ui.json',{}).get('section','all')
    return value if value in SECTION_LABELS else 'all'


def select_section(value):
    value=value.lower()
    value={'1':'claude','2':'codex','3':'all','4':'agy'}.get(value,PROVIDERS.get(value,value))
    if value not in SECTION_LABELS:
        raise ValueError('섹션 선택: claude, chatgpt, agy 또는 all')
    with locked('view.lock'):
        data=read_json('ui.json',{})
        data['section']=value
        write_json('ui.json',data)
    return value


def parse(text, *, remote=False):
    if not isinstance(text, str) or len(text) > 1000:
        return None
    value = text.strip()
    if remote:
        # The phone keyboard may break the line after the prefix ("Talkturn⏎Help").
        match = re.fullmatch(r'(?:톡턴|/?talkturn)(?:\s+([^\r\n]*))?', value, re.I)
        if not match:
            return None
        value = match[1] or 'status'
    parts = value.split()
    if not parts:
        return ['help']
    parts[0] = ALIASES.get(parts[0].lower(), parts[0].lower())
    if parts[0] in ('tell','computer-name'):
        original=value.split(maxsplit=1)
        return [parts[0],*original[1:]]
    if parts[0] in ('answer','send'):
        original=value.split(maxsplit=2)
        return [parts[0],*original[1:]]
    if len(parts) > 1 and parts[0] in ('account-add','connect','section','sessions','usage','switch','model','models','effort'):
        parts[1] = PROVIDERS.get(parts[1].lower(), parts[1].lower())
    return parts


def screen_images():
    """One PNG per monitor: a merged multi-monitor grab is unreadable once Kakao shrinks it."""
    if sys.platform=='darwin':
        for stale in ROOT.glob('screen-*.png'):
            stale.unlink()
        from . import mac_kakao
        return mac_kakao.screen_images(ROOT)
    import ctypes
    import win32api
    from PIL import ImageGrab
    user32=ctypes.windll.user32
    user32.SetThreadDpiAwarenessContext.restype=ctypes.c_void_p
    user32.SetThreadDpiAwarenessContext.argtypes=[ctypes.c_void_p]
    previous=user32.SetThreadDpiAwarenessContext(-3)  # physical pixels, the unit Pillow grabs in
    try:
        boxes=sorted(tuple(rect) for _,_,rect in win32api.EnumDisplayMonitors())
    finally:
        user32.SetThreadDpiAwarenessContext(previous)
    for stale in ROOT.glob('screen-*.png'):
        stale.unlink()
    paths=[]
    for number,box in enumerate(boxes,1):
        path=ROOT/f'screen-{number}.png'
        with ImageGrab.grab(bbox=box,all_screens=True) as image:
            image.save(path)
        paths.append(path)
    return paths


def usage_text(data):
    lines = ['사용 한도']
    for provider, block in data.items():
        lines.append(f'\n{provider}')
        if block.get('state') == 'unavailable':
            lines.append('조회 불가 · ' + block.get('error', '로그인 상태 확인 필요'))
            continue
        groups = block.get('accounts', []) if provider == 'claude' else [block]
        if not groups:
            lines.append('아직 기록 없음. Claude 창에서 답을 한 번 받으면 표시됩니다.')
        for group in groups:
            if provider == 'claude':
                # An inactive account whose limits cannot be read adds nothing; the active one still says so.
                if group.get('state') == 'unavailable' and not group['active']:
                    continue
                lines.append(f"{'●' if group['active'] else '○'} {group['label']}")
                if group.get('state') == 'unavailable':
                    lines.append('  한도 미제공/조회 실패')
                    continue
            for limit in group.get('limits', []):
                pct = limit.get('remaining_pct')
                rest = f'{pct:g}%' if pct is not None else '미제공'
                lines.append(f"  {limit['name']} 잔여 {rest} · 초기화 {providers.reset_time(limit.get('resets_at'))}")
        if provider in ('codex','agy'):
            if not block.get('limits'):
                lines.append('한도 미제공')
    return '\n'.join(lines)


def execute(parts, *, remote=False):
    if not parts:
        return HELP
    command, *args = parts
    section=selected_section()
    if command in ('account-add','switch') and args and args[0]=='agy':
        raise ValueError(AGY_ACCOUNT_NOTICE)
    if command=='computer-name' and len(args)<=1:
        from .computer import computer_label,set_computer_name
        if args:
            set_computer_name(args[0])
        return computer_label()
    if command=='section' and len(args)<=1:
        if not args:
            return f'현재 섹션: {SECTION_LABELS[section]}\n{SECTION_MENU}\n예: 톡턴 섹션 chatgpt'
        section=select_section(args[0])
        from .tasks import status_text
        return status_text(section)
    if command == 'help' and not args:
        return PHONE_HELP if remote else HELP
    if command=='receipt' and not args:
        return receipt_text()
    if command=='original':
        from .plain_delivery import original_text
        return original_text(args)
    if command=='quota-alert' and len(args)<=1:
        if args and args[0] not in ('on','off','켜','꺼'):
            raise ValueError('한도알림 on 또는 off')
        if args:
            with locked('settings.lock'):
                data=read_json('delivery.json',{})
                data['quota_prompt_enabled']=args[0] in ('on','켜')
                write_json('delivery.json',data)
        enabled=read_json('delivery.json',{}).get('quota_prompt_enabled',True)
        return '한도 소진 시 전환 질문: '+('켜짐' if enabled else '꺼짐')+'\n5분 간격 확인. 자동 계정 전환 없음.'
    if command=='keep' and not args:
        return '현재 계정 유지. 같은 한도 소진 알림은 반복하지 않음.'
    if command == 'usage' and len(args) <= 1 and (not args or args[0] in ('all','claude','codex','agy')):
        return usage_text(providers.usage(args[0] if args else section))
    if command == 'accounts' and not args:
        if section=='agy':
            return AGY_ACCOUNT_NOTICE
        data = providers.accounts()
        if section!='all':
            data={section:data.get(section,[])}
        return '\n'.join(['TalkTurn 계정'] + [f"{provider} {'●' if row['active'] else '○'} {row['number']} {row['label']}" + (' [비활성]' if row['disabled'] else '') for provider, rows in data.items() for row in rows])
    if command == 'switch' and len(args) == 2:
        data = providers.switch_account(*args)
        return f"{data['provider']} 계정 {data['account']} 활성 확인.\n{data['scope']}"
    if command in ('model', 'settings') and not args:
        data = providers.model_settings()
        keys=('codex','claude','agy','relay') if section=='all' else (section,'relay')
        return '\n'.join([f'{SECTION_LABELS[section]} · 모델 설정'] + [f"{SECTION_LABELS.get(key,'공통 중계')}: {data[key]['model']}" + (f" · 노력도 {data[key]['effort']}" if 'effort' in data[key] else '') for key in keys] + [data['scope']])
    if command=='models' and not args and section!='all':
        args=[section]
    if command == 'models' and len(args) == 1:
        return f'{args[0]} 모델 목록\n' + '\n'.join(providers.models(args[0])) + ('\nClaude 별칭/예시 목록. 계정·조직별 사용 가능 모델은 다를 수 있음.' if args[0]=='claude' else '')
    if command == 'model' and len(args) == 2:
        return providers.set_model(*args)
    if command == 'effort' and len(args) == 2:
        return providers.set_effort(*args)
    if command=='sessions' and len(args)<=1 and (not args or args[0] in ('all','claude','codex','agy','recent','최근')):
        from .sessions import discover,list_text,phone_text
        if remote and not args:
            return phone_text(discover())
        recent=bool(args and args[0] in ('recent','최근'))
        provider=args[0] if args and args[0] in ('claude','codex','agy') else 'all'
        return list_text(discover(recent=recent),provider)
    if command=='watch':
        from .sessions import identity,watched,watch_many,watch_text
        if not args:
            return watch_text()
        previous=watched()
        rows=watch_many([] if args==['none'] else args)
        from .service import connection_message,selection_update
        if remote:
            if {identity(row) for row in previous}=={identity(row) for row in rows}:
                return watch_text(rows)
            return '' if selection_update(previous) else connection_message(update=True)
        try:
            updated=selection_update(previous)
        except (ValueError,RuntimeError,OSError,TimeoutError) as error:
            return watch_text(rows)+'\n선택 저장됨 · 변경 알림 실패: '+str(error)
        return watch_text(rows)+('\ntalkturn update 전송 확인' if updated else '')
    if command=='select' and len(args)<=1:
        from .sessions import selected,selection_text,choose,list_text
        if not args:
            current=selected()
            return selection_text(current) if current else list_text()
        return selection_text(choose(args[0]))
    if command=='unselect' and not args:
        from .sessions import clear
        return clear()
    if command=='tell' and len(args)==1:
        from .sessions import send_selected
        return send_selected(args[0])
    if command in ('notifications','replies') and len(args)==1 and args[0] in ('on','off','켜','꺼'):
        enabled=args[0] in ('on','켜')
        with locked('settings.lock'):
            if command=='notifications':
                (ROOT/'enabled').touch() if enabled else (ROOT/'enabled').unlink(missing_ok=True)
            else:
                data=read_json('delivery.json',{})
                data['reply_routing_enabled']=enabled
                write_json('delivery.json',data)
        return f'TalkTurn {"결과 알림" if command=="notifications" else "세션 답장"}: {"켜짐" if enabled else "꺼짐"}\n카톡 설정 명령 수신은 유지됨.'
    if command == 'license' and not args:
        return 'TalkTurn: AGPL-3.0-only\n수정·재배포·상업적 이용 가능. 라이선스의 소스 제공 조건 적용.\n출처·전체 라이선스: 배포본의 LICENSE, THIRD_PARTY_LICENSES.txt 및 함께 제공되는 소스 아카이브.'
    if command=='questions' and not args:
        from .questions import pending, describe
        records=pending()
        if section!='all':
            records=[record for record in records if record['provider']==section]
        return '\n\n'.join(describe(record) for record in records) if records else '대기 중인 질문 없음.'
    if command=='answer' and len(args)==2:
        from .questions import answer
        return answer(*args)
    if command=='send' and len(args)==2:
        return forward_response(send_numbered(*args))
    if command=='display' and not args:
        mode=read_json('delivery.json',{}).get('long_text','image')
        options=(f'TalkTurn 표시 · 현재 {mode}\n'
                 '표시 auto · 짧은 답은 글, 긴 답은 사진\n'
                 '표시 text · 글로 받기\n'
                 '표시 image · 사진으로 받기')
        return options+('\n바꾸려면 이 안내에 답장하세요.' if remote else '')
    if command in ('display','router') and len(args)==1:
        allowed=('auto','text','image') if command=='display' else ('on','off')
        if args[0] not in allowed:
            raise ValueError('가능 값: '+', '.join(allowed))
        with locked('settings.lock'):
            data=read_json('delivery.json',{})
            data['long_text' if command=='display' else 'reply_mode']=args[0] if command=='display' else ('luna' if args[0]=='on' else 'manual')
            write_json('delivery.json',data)
        if command=='display':
            return '긴 글 표시: '+args[0]
        return '자동 답장 판별 켜짐. 설정된 중계 모델 사용.' if args[0]=='on' else 'LLM 없는 중계 켜짐.\n작업 번호로 전달: 톡턴 전달 T1234ABCD 내용\n카톡 인용 답장 자동 판별은 꺼짐.'
    if command=='screen' and not args:
        from .computer import computer_label
        # The markdown links are attached as photos by plain_delivery.extract_images.
        return 'PC 화면 · '+computer_label()+'\n'+'\n'.join(f'![화면](<{path}>)' for path in screen_images())
    if command == 'status' and not args:
        from .tasks import status_text
        return status_text('all')
    if command == 'ping' and not args:
        if remote:
            from .service import connection_message
            return connection_message()
        from .service import ping
        ping()
        return '카톡에 talkturn in 전송·확인 완료.'
    if command == 'start' and not args and not remote:
        from .service import start
        return start()
    if command == 'stop' and not args and not remote:
        from .service import stop
        return stop()
    if command == 'out' and not args:
        from .service import disconnect, stop
        if remote:
            stop()
            return 'talkturn out'
        return disconnect()
    raise ValueError('지원하지 않는 명령/인수. help로 확인.')
