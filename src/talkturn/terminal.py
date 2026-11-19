"""Compact Rich presentation for provider work and remaining allowances."""
from collections import Counter
from rich import box
from rich.align import Align
from rich.cells import cell_len
from rich.columns import Columns
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.segment import Segment
from .commands import SECTION_LABELS, selected_section, AGY_ACCOUNT_NOTICE
from .providers import reset_time
from .runtime import read_json
import os
from pathlib import PureWindowsPath
import sys
import time

ACCENT='bright_white'
PAGE_SIZE=10
PROVIDER_TABS=(('claude','Claude Code'),('codex','Codex'),('agy','AGY'))


def _read_key(console,keyboard,rendered_size):
    while not keyboard.kbhit():
        if console.size!=rendered_size:
            return None
        time.sleep(.1)
    return keyboard.getwch()


def page_rows(view):
    active=view.get('active_provider') or (view.get('selected') or {}).get('provider') or (view['rows'][0]['provider'] if view['rows'] else 'claude')
    rows=[row for row in view['rows'] if row['provider']==active]
    focus=next((i for i,row in enumerate(rows) if row['code']==view.get('focused_code')),view.get('page_start',0))
    size=view.get('page_size',PAGE_SIZE)
    start=min((focus//size)*size,((max(1,len(rows))-1)//size)*size)
    return rows[start:start+size],start,len(rows)


def choice_menu(console,title,choices,*,selected=0,subtitle='↑↓ 이동 · Enter 선택 · Esc 뒤로'):
    """One small selector shared by model setup and account actions."""
    if not choices:
        return None
    selected=max(0,min(selected,len(choices)-1))
    if os.name!='nt' or not sys.stdin.isatty() or not console.is_terminal:
        response(console,'\n'.join([title]+[f'{i}. {label}' for i,(_,label) in enumerate(choices,1)]))
        while True:
            answer=input(f'번호 [Enter: {selected+1}, 취소: q]: ').strip()
            if answer.lower() in ('q','esc'): return None
            if not answer: return choices[selected][0]
            if answer.isdigit() and 1<=int(answer)<=len(choices): return choices[int(answer)-1][0]
            console.print('목록의 번호를 입력하세요.',style='grey74')
    import msvcrt
    previous=[]; previous_size=None
    with console.screen(hide_cursor=True):
        while True:
            visible_count=max(1,console.height-11)
            start=max(0,selected-visible_count//2)
            entries=[]
            for i,(_,label) in enumerate(choices[start:start+visible_count],start):
                entry=label.copy() if isinstance(label,Text) else Text(str(label))
                entry=Text(('› ' if i==selected else '  '),style='bold '+ACCENT)+entry
                entry.justify='center'
                if i==selected: entry.stylize('bold on grey15')
                entries.append(entry)
            if sum(entry.plain.count('\n')+1 for entry in entries)+len(entries)-1<=console.height-11:
                entries=[item for index,entry in enumerate(entries) for item in ([Text(''),entry] if index else [entry])]
            frame=Align.center(Group(Text('talkturn',style='bold bright_yellow',justify='center'),Text(''),
                Panel(Group(*entries),title=Text(title,style='bold bright_white'),border_style='grey35',padding=(1,2)),Text(''),
                Text(subtitle,style='grey70',justify='center')),width=min(max(1,console.width-4),110),vertical='middle',height=console.height)
            width,height=console.size
            lines=Segment.set_shape(console.render_lines(frame,console.options.update_dimensions(width,height)),width,height)
            if previous_size!=(width,height):
                previous=[]
            with console:
                for y,line in enumerate(lines):
                    if y>=len(previous) or line!=previous[y]: console.update_screen_lines([line],y=y)
            previous=lines; previous_size=(width,height)
            key=_read_key(console,msvcrt,previous_size)
            if key is None:
                continue
            if key=='\x03': raise KeyboardInterrupt
            if key in ('\x00','\xe0'):
                key={'H':'up','P':'down'}.get(msvcrt.getwch(),'')
            elif key=='\x1b':
                if msvcrt.kbhit() and msvcrt.getwch()=='[':
                    key={'A':'up','B':'down'}.get(msvcrt.getwch(),'')
                else: return None
            if key in ('up','down'): selected=(selected+(1 if key=='down' else -1))%len(choices)
            elif key in ('\r','\n'): return choices[selected][0]
            elif key.lower()=='q': return None


def header(console,view=None,*,spacious=True):
    from .computer import computer_label
    relay=read_json('delivery.json',{}).get('model','gpt-6-luna')
    relay={'gpt-6-luna':'GPT-6 Luna','claude-haiku-5-5':'Haiku 5.5'}.get(relay,relay)
    if console.width>=70 and not (view and view.get('compact') and console.height<=24):
        logo=Text('▀█▀ ▄▀█ █   █▄▀ ▀█▀ █ █ █▀█ █▄ █\n █  █▀█ █▄▄ █ █  █  █▄█ █▀▄ █ ▀█',style='bold bright_yellow',justify='center')
    else:
        logo=Text('talkturn',style='bold bright_yellow',justify='center')
    counts=view.get('account_counts',{}) if view else {}
    count_label=lambda provider:f"{counts[provider]}개" if type(counts.get(provider)) is int else '미확인'
    # AGY has no login-status command: show only that its conversations were read over ACP, apart from the login counts.
    agy=view and any(row.get('provider')=='agy' and row.get('reachable') for row in view.get('rows',[]))
    caption=f"현재 로그인 계정 · Claude {count_label('claude')} · Codex {count_label('codex')}"+('   ·   AGY 연결됨' if agy else '') if view else '작업 결과는 카톡으로 · 답장은 원래 세션으로'
    caption=Text(caption,style='grey62',justify='center',no_wrap=True,overflow='ellipsis')
    if view and view.get('compact') and not spacious:
        if '\n' not in logo.plain:
            logo.append('  ·  '+computer_label(),style='grey66')
        return Group(logo,caption)
    items=[logo,Text(''),Text(computer_label()+'   ·   중계 '+relay,style='grey66',justify='center',no_wrap=True,overflow='ellipsis')]
    if spacious:
        items.append(Text(''))
    items.append(caption)
    if view and view.get('compact'):
        return Group(*items)
    from .sessions import selected
    current=selected()
    if current:
        items.append(Text(f"선택 · {current['name']}  ({current['code']})",style='bold bright_yellow',justify='center',no_wrap=True,overflow='ellipsis'))
    else:
        items.append(Text('작업 세션을 골라 원래 대화에 메시지를 보내세요.',style='grey70',justify='center'))
    return Group(*items,Text(''))


def relay_menu(console,current):
    table=Table.grid(padding=(0,2))
    table.add_column(style='bold '+ACCENT); table.add_column(); table.add_column(style='grey62')
    table.add_row('1','GPT-6 Luna','Codex 로그인')
    table.add_row('2','Claude Haiku 5.5','Claude 로그인')
    console.print(Panel(table,title='답장 중계 모델',subtitle=Text('Enter · '+current,style='grey62'),
                        border_style='grey35',box=box.ROUNDED,padding=(1,2)))


def quota_panel(provider,block,model,width):
    color='rgb(221,160,114)' if provider=='claude' else 'bright_cyan'
    title=SECTION_LABELS[provider]
    items=[Text('기본 모델  '+str(model),style='grey70')]
    if block.get('state')=='unavailable':
        items += [Text('한도 조회 불가',style='grey74'),Text(block.get('error','로그인 상태 확인 필요'),style='grey62')]
    else:
        groups=block.get('accounts',[]) if provider=='claude' else [block]
        if not groups:
            items.append(Text('등록된 활성 계정 없음',style='grey62'))
        for group in groups:
            if provider=='claude':
                title+=f" · {group['label']}"
                if group.get('state')=='unavailable':
                    items.append(Text('한도 미제공 · 로그인 확인 필요',style='grey62'))
                    continue
                age=group.get('age_seconds')
                stamp='마지막 성공값' if group.get('state')=='cached' else '조회값'
                if isinstance(age,(int,float)):
                    stamp+=f' · {int(age)//60}분 전'
                items[0].append(' · '+stamp,style='grey50')
            table=Table(box=None,padding=(0,1),expand=True,show_header=True)
            table.add_column('구간',style='grey70',no_wrap=True)
            if width>=64:
                table.add_column('남은 한도',ratio=1)
            table.add_column('잔여',justify='right',no_wrap=True)
            table.add_column('초기화',justify='right',no_wrap=True,style='grey62')
            for limit in group.get('limits',[]):
                pct=limit.get('remaining_pct')
                shade='bright_green' if pct is not None and pct>30 else 'grey74' if pct is not None and pct>10 else 'bright_red'
                label=limit['name'].removeprefix('codex ')
                reset=reset_time(limit.get('resets_at'))
                reset=' '.join(reset.split()[:2]) if reset!='미제공' else '—'
                cells=[Text(label)]
                if width>=64:
                    length=16
                    filled=round(length*pct/100) if pct is not None else 0
                    bar=Text('█'*filled,style=shade)
                    bar.append('░'*(length-filled),style='grey27')
                    cells.append(bar)
                cells += [Text(f'{pct:g}%' if pct is not None else '—',style=shade if pct is not None else 'grey50'),Text(reset)]
                table.add_row(*cells)
            if group.get('limits'):
                items.append(table)
            else:
                items.append(Text('한도 미제공',style='grey62'))
    return Panel(Group(*items),title=Text(title,style='bold '+color),border_style='grey35',box=box.ROUNDED,padding=(0,1))


def blocked_reasons(view):
    confirmed={row['code'] for row in view.get('watched') or []}
    checked=set(view.get('checked_codes',confirmed))
    reasons=[]
    if checked!=confirmed:
        reasons.append('체크 변경 미확정 · Enter를 눌러 적용하세요.')
    elif not checked and ('rows' not in view or page_rows(view)[2]):
        reasons.append('받을 작업 미선택 · Space로 체크 후 Enter로 확정하세요.')
    # The Enter connection dialog handles a closed self-chat without a duplicate footer warning.
    return reasons+[issue for issue in view.get('kakao_issues',[])
                    if not issue.startswith('나와의 채팅 닫힘')]


def project_name(row):
    return row.get('project') or PureWindowsPath(row.get('cwd') or '').name


def dashboard(console,view,*,render_only=False,max_rows=None):
    from .sessions import identity
    width=min(console.width,100)
    panels=[header(console,view,spacious=not view.get('compact') or console.height>24)]
    current=view.get('selected')
    active=view.get('active_provider') or (current or {}).get('provider') or (view['rows'][0]['provider'] if view['rows'] else 'claude')
    tabs=Text(no_wrap=True,overflow='ellipsis')
    for tab_index,(provider,label) in enumerate(PROVIDER_TABS):
        caption=('← ' if tab_index==0 else '')+label+(' →' if tab_index==len(PROVIDER_TABS)-1 else '')
        if tabs:
            tabs.append(' · ',style='grey50')
        tabs.append(caption,style='bold bright_white' if provider==active else 'grey70')
    for provider in (active,):
        table=Table(box=None,padding=(0,1),show_header=True)
        table.add_column('번호',justify='center',style='grey50',no_wrap=True)
        table.add_column('카톡으로 받을 작업',ratio=1,overflow='ellipsis' if max_rows else 'fold',no_wrap=bool(max_rows))
        visible,start,total=page_rows(view)
        all_rows=[row for row in view['rows'] if row['provider']==provider]
        checked_codes=set(view.get('checked_codes',[row['code'] for row in view.get('watched') or []]))
        names=Counter(row['name'] for row in all_rows)
        any_duplicates=any(names[item['name']]>1 for item in visible)
        project_width=min(18,max(12,width//4))
        if any_duplicates:
            projects=Counter((row['name'],project_name(row)) for row in all_rows if names[row['name']]>1)
            table.add_column('작업 구분',max_width=project_width,no_wrap=True,overflow='ellipsis')
        focused_row=None
        for index,item in enumerate(visible,1):
            chosen=identity(current)==identity(item)
            focused=view.get('focused_code')==item['code']
            checked=item['code'] in checked_codes
            if focused:
                focused_row=item
            cells=[str(index),
                   Text(('› ' if focused else '  ')+('[✓] ' if checked else '[ ] ')+item['name'],
                        style='bold yellow' if chosen or checked or focused else 'white')]
            if any_duplicates:
                project=project_name(item)
                label=project if project and cell_len(project)<=project_width and projects[(item['name'],project)]==1 else item['code']
                cells.append(Text(label if names[item['name']]>1 else '',style='grey74'))
            table.add_row(*cells,style='on grey15' if focused else None)
        contents=[]
        block=view['usage'].get(provider,{})
        groups=block.get('accounts',[]) if provider=='claude' else [block]
        summary=Text(style='grey66',justify='center')
        for group in groups:
            for limit in group.get('limits',[]):
                name=limit.get('name','').removeprefix('codex ')
                if name not in ('5시간','7일'):
                    continue
                value=limit.get('remaining_pct')
                if summary:
                    summary.append('  ·  ')
                summary.append(f'{name} 잔여 ')
                summary.append(f'{value:g}%' if value is not None else '—',style='grey74' if value is not None and value<=20 else 'bright_green')
        if summary and (not view.get('compact') or console.height>24 or len(visible)<8):
            contents.append(summary)
        if visible:
            contents.append(Align.center(table))
            if focused_row:
                details=[focused_row['code'],project_name(focused_row)]
                contents.append(Text(' · '.join(filter(None,details)),
                                     style='grey74',justify='center',no_wrap=True,overflow='ellipsis'))
        else:
            contents.append(Text('카톡으로 받을 작업이 없습니다',style='bright_white',justify='center'))
            contents.append(Text(dict(PROVIDER_TABS)[provider]+'에서 작업을 연 뒤 [R] 새로고침',style='bright_white',justify='center'))
        error=view.get('session_errors',{}).get(provider)
        if error:
            contents.append(Text(error,style='grey74',justify='center',no_wrap=bool(max_rows),overflow='ellipsis'))
        size=view.get('page_size',PAGE_SIZE)
        subtitle=f'{start//size+1}/{max(1,(total+size-1)//size)} 페이지 · {total}개 작업 · 전체 {len(checked_codes)}개 체크'
        panels.append(Align.center(Panel(Group(*contents),title=tabs,subtitle=Text(subtitle,style='grey70'),width=min(width,56),
                            border_style='grey35',box=box.ROUNDED,padding=(0,1)),width=width))
    panel_end=len(panels)
    waiting=[request for request in view['waiting'] if request['provider']==active]
    if waiting:
        text=Text(justify='center')
        for index,request in enumerate(waiting[:1] if max_rows else waiting):
            if index:
                text.append('\n')
            text.append(request['id'],style='bold bright_white')
            text.append('  '+SECTION_LABELS[request['provider']]+' · '+('승인 대기' if request['kind']=='permission' else '답변 대기'))
        if view.get('compact'):
            panels.append(Text(f"[Q] 대기 질문 {len(waiting)}개 보기",style='bright_white',justify='center'))
        else:
            panels.append(Panel(text,title=f"질문 · {len(waiting)}개",border_style='grey74',box=box.ROUNDED))
    reasons=blocked_reasons(view)
    if view.get('compact') and console.width<76:
        reasons=[reason for reason in reasons if not reason.startswith('받을 작업 미선택')]
    panels.extend(Text('! '+reason,style='grey74',justify='center') for reason in reasons)
    if not max_rows:
        panels.extend([Text('  번호/코드 선택 · sessions 새로고침 · tell 내용 전달',style='grey66'),
                       Text('  usage 한도 자세히 · model 모델 · help 도움말 · out 종료',style='grey66')])
    if view.get('compact'):
        # Keep controls above the last console row, which some Windows hosts clip.
        content_height=len(console.render_lines(Group(*panels),console.options.update(width=width,height=None)))
        if content_height>console.height-5:
            panels[0]=header(console,view,spacious=False)
            content_height=len(console.render_lines(Group(*panels),console.options.update(width=width,height=None)))
        spare=console.height-5-content_height
        if spare>=2 and panel_end<len(panels):
            panels.insert(panel_end,Text(''))
        if spare>=1:
            panels.insert(1,Text(''))
    result=Align.center(Group(*panels),width=width)
    if render_only:
        return result
    console.print(result)


def show_dashboard(console,*,render=True):
    from .tasks import snapshot
    from .service import delivery_issues
    with console.status('작업과 한도를 불러오는 중…',spinner='dots',spinner_style=ACCENT):
        view=snapshot('all')
        view['kakao_issues']=delivery_issues()
    if render:
        dashboard(console,view)
    return view


def response(console,text):
    console.print(Panel(Text(str(text)),border_style='grey35',box=box.ROUNDED,padding=(1,2)))


def connection_alert_panel(message):
    keys=Text(justify='center')
    keys.append('[Enter]',style='bold bright_white on grey23')
    keys.append(' 다시 연결     ',style='bright_white')
    keys.append('[Esc]',style='bold bright_white on grey23')
    keys.append(' 작업 목록',style='bright_white')
    title='카톡 ‘나와의 채팅’을 열어주세요'
    items=[Text(title,style='bold bright_yellow',justify='center'),Text(''),
           Text('별도 채팅창을 연 뒤 Enter를 누르세요',style='bright_white',justify='center')]
    if message and str(message)!=title:
        items.append(Text(' '.join(str(message).split()),style='grey70',justify='center',no_wrap=True,overflow='ellipsis'))
    return Panel(Group(*items,Text(''),keys),title=Text('카톡 연결',style='bold bright_white'),
                 border_style='yellow',padding=(1,2))


def keyboard_pick(console,view):
    """Read only this terminal's keyboard; arrows select the already displayed snapshot."""
    alert=view.pop('connection_alert',None)
    if os.name!='nt' or not sys.stdin.isatty() or not console.is_terminal:
        if alert:
            console.print(connection_alert_panel(alert))
            while True:
                action=input('[Enter] 다시 연결 · [Esc] 작업 목록: ').strip()
                if not action:
                    return sorted(view.get('draft_checked_codes',[row['code'] for row in view.get('watched',[])]))
                if action.lower() in ('esc','q','\x1b'):
                    break
        dashboard(console,view)
        value=input('세션 번호/코드 선택 [N: 컴퓨터 이름 수정 · Enter: 유지]: ').strip()
        if value.lower()=='n':
            return {'action':'computer-name','checked_codes':[row['code'] for row in view.get('watched',[])]}
        return value
    import msvcrt
    available=list(range(len(view['rows'])))
    current=view.get('selected') or {}
    index=next((i for i in available if view['rows'][i]['code']==current.get('code')),available[0] if available else None)
    active=view.get('active_provider') or (view['rows'][index]['provider'] if index is not None else 'claude')
    group=[i for i in available if view['rows'][i]['provider']==active]
    if index not in group:
        index=group[0] if group else None
    positions={active:index}; typed=''
    watched=view.get('watched')
    initial_checked={row['code'] for row in watched or []}
    checked=set(view.pop('draft_checked_codes',initial_checked))
    notice=view.pop('action_notice','')
    detail_title=''; detail_text=''; detail_offset=0; detail_max=0
    pending_key=None
    previous=[]; previous_size=None
    def paint():
        nonlocal previous,previous_size,detail_max,detail_offset
        view['focused_code']=view['rows'][index]['code'] if index is not None else None
        view['active_provider']=active
        view['checked_codes']=checked
        view['compact']=True
        width,height=console.size
        view['page_size']=min(PAGE_SIZE,max(1,height-14))
        _,view['page_start'],_=page_rows(view)
        input_line=Text('번호/코드 입력: ',style='grey70',justify='center',no_wrap=True,overflow='ellipsis')
        input_line.append(('…'+typed[-24:]) if len(typed)>24 else typed,style='bold bright_white')
        input_line.append(' ▌',style='bold bright_white')
        if notice:
            input_line.append('   '+notice,style='grey74')
        shortcuts=[]
        if width<76:
            key_rows=([('↑↓','이동'),('←→','AI'),('Space','체크'),('?','더보기')],
                      [('Enter','적용' if initial_checked and not checked else '카톡 연결'),('R','새로고침'),('Esc','뒤로')])
        else:
            key_rows=([('↑↓','이동'),('←→','AI'),('Space','체크'),('Enter','체크 해제 적용' if initial_checked and not checked else '확정 및 카톡 연결'),('Esc','뒤로')],
                      [('N','컴퓨터 이름 수정'),('S','계정 전환'),('A','계정 추가'),('M','중계'),('R','새로고침')])
        for actions in key_rows:
            line=Text(justify='center',no_wrap=True,overflow='ellipsis')
            for key,label in actions:
                if line:
                    line.append(' ')
                line.append(f'[{key}]',style='bold bright_white on grey23')
                line.append(' '+label,style='bright_white')
            shortcuts.append(line)
        if detail_title:
            panel_width=min(width,80)
            wrapped=Text(detail_text).wrap(console,max(1,panel_width-4))
            visible_lines=max(1,height-6)
            detail_max=max(0,len(wrapped)-visible_lines)
            detail_offset=min(detail_offset,detail_max)
            footer=('[↑↓] 스크롤 · ' if detail_max else '')+'[Esc] 작업 목록'
            frame=Align.center(Panel(Group(*wrapped[detail_offset:detail_offset+visible_lines]),title=detail_title,
                                     subtitle=footer,width=panel_width,border_style='grey70'),
                               width=panel_width,vertical='middle',height=max(1,height-1))
        elif alert:
            frame=Align.center(connection_alert_panel(alert),width=min(max(1,width-4),64),
                               vertical='middle',height=max(1,height-1))
        else:
            # Leave space for every control and the fixed input when warnings wrap.
            budget=max(1,height-3-len(shortcuts))
            while True:
                body=dashboard(console,view,render_only=True,max_rows=PAGE_SIZE)
                length=len(console.render_lines(body,console.options.update(width=width,height=None)))
                if length<=budget or view['page_size']<=1:
                    break
                view['page_size']=max(1,view['page_size']-(length-budget))
            content=Group(body,Text(''),*shortcuts)
            frame=Group(Align.center(content,width=min(width,100),vertical='middle',height=max(1,height-2)),
                        Align.center(input_line,width=min(width,100)))
        lines=Segment.set_shape(console.render_lines(frame,console.options.update_dimensions(width,height)),width,height)
        if previous_size!=(width,height):
            previous=[]
        # Overwrite only changed rows in one buffered write. Never clear between keys.
        with console:
            for y,line in enumerate(lines):
                if y>=len(previous) or line!=previous[y]:
                    console.update_screen_lines([line],y=y)
        previous=lines; previous_size=(width,height)
    screen=console.screen(hide_cursor=True)
    screen.__enter__()
    try:
        if not console.is_alt_screen:
            dashboard(console,view)
            return input('세션 번호/코드 선택 [Enter: 유지]: ').strip()
        paint()
        while True:
            key=pending_key if pending_key is not None else _read_key(console,msvcrt,previous_size)
            pending_key=None
            if key is None:
                paint()
                continue
            if key=='\x03':
                raise KeyboardInterrupt
            if key in ('\x00','\xe0'):
                key={'H':'up','P':'down','K':'left','M':'right'}.get(msvcrt.getwch(),'')
            elif key=='\x1b' and msvcrt.kbhit():
                following=msvcrt.getwch()
                if following=='[':
                    key={'A':'up','B':'down','D':'left','C':'right'}.get(msvcrt.getwch(),'')
                else:
                    pending_key=following
            if detail_title:
                if key in ('\x1b','q','Q','?'):
                    detail_title=''
                elif key in ('up','down'):
                    detail_offset=max(0,min(detail_max,detail_offset+(1 if key=='down' else -1)))
                paint()
                continue
            if alert:
                if key in ('\r','\n'):
                    return sorted(checked)
                if key=='\x1b':
                    alert=None
                    notice=''
                    paint()
                continue
            if not typed and key.lower() in ('q','?'):
                detail_offset=0
                if key=='?':
                    detail_title='추가 단축키'
                    detail_text='N  컴퓨터 이름 수정\nS  계정 전환\nA  계정 추가\nM  답장 중계 모델\nK  카톡 연결 설정\nQ  현재 AI의 대기 질문'
                else:
                    from .questions import describe
                    detail_title=dict(PROVIDER_TABS)[active]+' · 대기 질문'
                    requests=[item for item in view.get('waiting',[]) if item['provider']==active and item.get('expires_at',float('inf'))>time.time()]
                    detail_text='\n\n'.join(describe(item) for item in requests) or '대기 중인 질문이 없습니다.'
                paint()
                continue
            if key.lower() in ('s','a','m','n','k','r') and not typed:
                if active=='agy' and key.lower() in ('s','a'):
                    notice=AGY_ACCOUNT_NOTICE; paint(); continue
                notice=key.upper()+' · '+{'s':'계정 전환','a':'계정 추가','m':'중계 모델','n':'컴퓨터 이름 수정','k':'카톡 연결','r':'작업 목록 새로고침'}[key.lower()]; paint()
                return {'action':{'s':'switch','a':'account-add','m':'relay','n':'computer-name','k':'setup','r':'refresh'}[key.lower()],
                        'provider':active,'checked_codes':sorted(checked)}
            if key=='\x1b':
                return ''
            if key in ('up','down','left','right'):
                typed=''; notice=''
                group=[i for i in available if view['rows'][i]['provider']==active]
                position=group.index(index) if index in group else 0
                positions[active]=index
                if key in ('left','right'):
                    providers=[provider for provider,_ in PROVIDER_TABS]
                    active=providers[(providers.index(active)+(1 if key=='right' else -1))%len(providers)]
                    group=[i for i in available if view['rows'][i]['provider']==active]
                    index=positions.get(active,group[min(position,len(group)-1)] if group else None)
                elif group:
                    index=group[(position+(1 if key=='down' else -1))%len(group)]
                notice=({'up':'↑ · 이전 작업','down':'↓ · 다음 작업'}[key] if key in ('up','down') else
                        ('←' if key=='left' else '→')+' · '+dict(PROVIDER_TABS)[active])
                paint()
            elif key in ('\r','\n'):
                value=typed.strip().upper()
                if value:
                    if value.isdigit():
                        visible,_,_=page_rows(view)
                        if 1<=int(value)<=len(visible): return visible[int(value)-1]['code']
                    elif value in {row['code'] for row in view['rows']}:
                        return value
                    if value:
                        notice='목록의 번호/코드를 입력하세요. Backspace로 수정 · Esc 취소'; paint(); continue
                if not checked and checked==initial_checked:
                    notice=('Space로 받을 작업을 체크하세요. 건너뛰기는 Esc' if index is not None else '작업을 연 뒤 [R] 새로고침')
                    paint(); continue
                return sorted(checked)
            elif key==' ':
                if index is None:
                    notice='AI에서 작업을 연 뒤 R로 새로고침하세요.'; paint(); continue
                code=view['rows'][index]['code']
                checked.symmetric_difference_update({code})
                typed=''; notice='Space · '+('체크됨' if code in checked else '체크 해제'); paint()
            elif key=='\b':
                typed=typed[:-1]; notice='Backspace · 지움'; paint()
            elif key.isprintable() and len(typed)<80:
                typed+=key; notice=''; paint()
    finally:
        screen.__exit__(None,None,None)
        view.pop('focused_code',None)
        view.pop('checked_codes',None)
