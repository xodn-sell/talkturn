"""Discover and select actual existing work sessions, without opening replacement chats."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid
from .codex_rpc import CodexReader
from .runtime import executable,module_command,run_json,read_json,write_json,locked


def code(provider,session_id):
    digest=hashlib.sha256(f'{provider}:{session_id}'.encode()).hexdigest()[:8].upper()
    return {'claude':'C-', 'codex':'X-', 'agy':'G-'}[provider]+digest


def identity(row):
    if not row:
        return None
    target=('peer:'+str(row.get('peer_ref'))) if row.get('target_kind')=='claude_peer' else row.get('session_id')
    return row.get('provider'),target


def peer_row(peer):
    ref=peer.get('ref')
    if not ref or not peer.get('name'):
        return None
    kind=peer.get('type','')
    client='Claude 앱' if kind=='desktop' else '원격 Code' if kind in ('remote','rc','cloud') else '연결된 Code'
    state={'idle':'대기','waiting':'질문 대기','busy':'진행 중','running':'진행 중'}.get(peer.get('status'),'상태 미확인')
    return {'provider':'claude','session_id':None,'target_kind':'claude_peer','peer_ref':ref,
            'code':code('claude','peer:'+ref),'name':peer['name'],'state':state,'model':None,'effort':None,
            'model_source':None,'effort_source':None,
            'client':client,'project':'','cwd':'','updated_at':0,'live':True,'reachable':True}


def timestamp(value):
    if isinstance(value,(int,float)):
        return value/1000 if value>10**11 else value
    try:
        return datetime.fromisoformat(str(value).replace('Z','+00:00')).timestamp()
    except ValueError:
        return 0


def codex_row(row,shared):
    session=row.get('id')
    if not session or row.get('ephemeral') or row.get('parentThreadId') or row.get('canAcceptDirectInput') is False:
        return None
    status=row.get('status') or {}
    kind=status.get('type') if isinstance(status,dict) else status
    flags=status.get('activeFlags',[]) if isinstance(status,dict) else []
    state={'active':'진행 중','idle':'대기','notLoaded':'저장됨','systemError':'오류','error':'오류'}.get(kind,'상태 미확인')
    if 'waitingOnApproval' in flags:
        state='승인 대기'
    elif 'waitingOnUserInput' in flags:
        state='질문 대기'
    if not shared:
        state='실행 상태 미확인'
    source=row.get('source')
    client='앱/에디터' if source=='vscode' else '터미널' if source=='cli' else '연결된 Codex'
    live=shared and kind not in ('notLoaded',None)
    settings_source='thread_live' if live else 'thread_saved'
    return {'provider':'codex','session_id':session,'code':code('codex',session),'client':client,
            'name':row.get('name') or f'제목 없는 작업 · {session[:8]}','state':state,
            'model':row.get('model'),'effort':row.get('reasoningEffort'),
            'model_source':settings_source if row.get('model') else None,
            'effort_source':settings_source if row.get('reasoningEffort') else None,
            'cwd':row.get('cwd',''),'project':Path(row.get('cwd') or '').name,
            'updated_at':timestamp(row.get('updatedAt')),'live':live}


def codex_sessions(recent=False):
    with CodexReader(prefer_shared=True) as client:
        rows={}
        # The app's sidebar includes saved threads, even when this daemon has
        # unloaded them. Listing only loaded IDs hid most desktop work.
        cursor=None; cursors=set(); limit=300 if recent else 100
        while len(rows)<limit:
            params={'limit':min(100,limit-len(rows)),'sortKey':'updated_at','useStateDbOnly':True,'modelProviders':[]}
            if cursor:
                params['cursor']=cursor
            page=client.call('thread/list',params)
            for raw in page.get('data',[]):
                item=codex_row(raw,client.shared)
                if item:
                    rows[item['session_id']]=item
            cursor=page.get('nextCursor')
            if not cursor or cursor in cursors or not page.get('data'):
                break
            cursors.add(cursor)
        if client.shared:
            ids=[]; cursor=None; cursors=set()
            while True:
                params={'limit':100}
                if cursor:
                    params['cursor']=cursor
                page=client.call('thread/loaded/list',params)
                ids.extend(page.get('data',[]))
                cursor=page.get('nextCursor')
                if not cursor or cursor in cursors or not page.get('data') or len(ids)>=300:
                    break
                cursors.add(cursor)
            for session_id in ids:
                try:
                    result=client.call('thread/read',{'threadId':session_id,'includeTurns':False})
                    item=codex_row(result.get('thread',{}),True)
                    if item:
                        saved=rows.get(session_id,{})
                        if not result.get('thread',{}).get('name') and saved.get('name'):
                            item['name']=saved['name']
                        rows[session_id]=item
                except RuntimeError:
                    continue  # A thread can close between the list and metadata read.
        return list(rows.values()),None if client.shared else '현재 실행 서버에 연결되지 않아 최근 저장 세션을 표시함. 실행 상태 미확인.'


def claude_desktop_sessions():
    """Read only Code-tab index metadata, never conversation transcripts or auth."""
    root=Path(os.environ.get('APPDATA',Path.home()/'AppData/Roaming'))/'Claude/claude-code-sessions'
    result={}
    for path in root.glob('*/*/local_*.json'):
        try:
            if path.stat().st_size>1024*1024:
                continue
            data=json.loads(path.read_text(encoding='utf-8-sig'))
            if not isinstance(data,dict) or data.get('isArchived'):
                continue
            session=str(uuid.UUID(data['cliSessionId']))
            app_session=str(data.get('sessionId',''))
            if app_session!=path.stem:
                continue
            name=data.get('title') or f'제목 없는 앱 작업 · {session[:8]}'
            item={'provider':'claude','session_id':session,'app_session_id':app_session,
                  'code':code('claude',session),'name':str(name),'client':'Claude 앱',
                  'state':'앱 연결 대기','model':data.get('model'),'effort':data.get('effort'),
                  'model_source':'app_saved' if data.get('model') else None,
                  'effort_source':'app_saved' if data.get('effort') else None,'cwd':data.get('cwd') or '',
                  'project':Path(data.get('cwd') or '').name,'updated_at':timestamp(data.get('lastActivityAt')),
                  'live':False,'reachable':False,'peer_ref':None}
            if session not in result or item['updated_at']>result[session]['updated_at']:
                result[session]=item
        except (OSError,ValueError,TypeError,KeyError):
            continue
    return list(result.values())


def claude_targets():
    # The MCP SDK is needed only for this short query; keep it out of the idle TUI.
    return run_json(module_command('claude_transport'),timeout=35)


@lru_cache(maxsize=256)
def _claude_title_file(path, session_id, mtime_ns, size):
    """Parse only explicit title metadata; never parse prompts or assistant messages."""
    titles={}
    with Path(path).open('rb') as stream:
        while line:=stream.readline(65536):
            complete=line.endswith(b'\n') or len(line)<65536
            if complete and re.match(br'\s*\{\s*"type"\s*:\s*"(?:custom-title|ai-title)"',line[:100]):
                try:
                    data=json.loads(line)
                    field='customTitle' if data['type']=='custom-title' else 'aiTitle'
                    value=data.get(field)
                    if data.get('sessionId')==session_id and isinstance(value,str):
                        titles[field]=value.strip()
                except (ValueError,KeyError):
                    pass
            while not complete:
                rest=stream.readline(65536)
                complete=not rest or rest.endswith(b'\n') or len(rest)<65536
    return titles.get('customTitle') or titles.get('aiTitle') or ''


def claude_title(session_id):
    try:
        session_id=str(uuid.UUID(session_id))
        root=Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home()/'.claude')/'projects'
        paths=list(root.glob('*/'+session_id+'.jsonl'))
        if len(paths)==1:
            stat=paths[0].stat()
            return _claude_title_file(str(paths[0]),session_id,stat.st_mtime_ns,stat.st_size)
    except (OSError,ValueError,TypeError,AttributeError):
        pass
    return ''


def refresh_claude_titles(rows):
    """Keep transport names and UUIDs intact while showing the original task title."""
    for row in rows:
        if row.get('provider')!='claude' or not row.get('session_id'):
            continue
        agent_name=row.setdefault('agent_name',row.get('name',''))
        title=claude_title(row['session_id'])
        automatic=bool(re.fullmatch(re.escape(Path(row.get('cwd') or '').name)+r'-[0-9a-f]{2}',agent_name))
        if title:
            row['name']=title
        elif automatic:
            row['name']=row.get('app_title') or f"제목 없는 작업 · {row['session_id'][:8]}"
    return rows


def claude_sessions(recent=False):
    note=None
    try:
        command=[executable('claude'),'agents','--json']+(['--all'] if recent else [])
        rows=run_json(command,timeout=25)
    except (RuntimeError,TimeoutError,OSError):
        rows=[]
        note='Claude CLI 연결 조회 실패. 앱 저장 목록만 표시할 수 있음.'
    try:
        peers=claude_targets().get('targets',[])
    except Exception:
        peers=None
        note='Claude 메시지 수신 연결을 확인하지 못함.'
    result=[]
    for row in rows:
        session=row.get('sessionId')
        if not session:
            continue
        state=row.get('status') or row.get('state')
        label={'running':'진행 중','active':'진행 중','working':'진행 중','waiting':'질문 대기',
               'idle':'대기','blocked':'입력 대기','done':'완료','completed':'완료','failed':'오류','error':'오류'}.get(state,'상태 미확인')
        matching=[peer for peer in peers or [] if peer.get('name')==row.get('name')]
        unique=sum(other.get('name')==row.get('name') for other in rows)==1
        reachable=None if peers is None else len(matching)==1 and unique
        if reachable:
            label={'idle':'대기','waiting':'질문 대기','busy':'진행 중','running':'진행 중'}.get(matching[0].get('status'),label)
        elif peers is not None:
            label='수신 연결 없음' if unique else '이름 중복'
        connection=peer_row(matching[0]) if reachable else None
        result.append({'provider':'claude','session_id':session,'code':code('claude',session),
            'name':row.get('name') or f'제목 없는 작업 · {session[:8]}','state':label,
            'model':row.get('model'),'effort':row.get('effort'),
            'model_source':'session_registry' if row.get('model') else None,
            'effort_source':'session_registry' if row.get('effort') else None,
            'cwd':row.get('cwd',''),'project':Path(row.get('cwd') or '').name,
            'updated_at':timestamp(row.get('updatedAt') or row.get('startedAt')),'live':reachable is True,'reachable':reachable,
            'peer_ref':matching[0].get('ref') if reachable else None,'client':connection['client'] if connection else 'Claude Code'})
    # Desktop/remote Code peers may be exposed by MCP without a local CLI UUID.
    known={row.get('peer_ref') for row in result if row.get('reachable')}
    for peer in peers or []:
        if peer.get('ref') not in known and not any(row.get('session_id') and row.get('name')==peer.get('name') for row in result):
            item=peer_row(peer)
            if item:
                result.append(item)
                known.add(item['peer_ref'])
    by_session={row['session_id']:row for row in result if row.get('session_id')}
    for desktop in claude_desktop_sessions():
        existing=by_session.get(desktop['session_id'])
        if existing:
            # A native UUID match proves identity; a matching title alone does not.
            # A Desktop index can predate a rename or continuation in the CLI.
            # Preserve live identity/settings; mark missing fields filled from saved app settings.
            existing['app_session_id']=desktop['app_session_id']
            existing['app_title']=desktop['name']
            for field in ('model','effort'):
                if not existing.get(field) and desktop.get(field):
                    existing[field]=desktop[field]
                    existing[field+'_source']='app_saved'
        else:
            result.append(desktop)
    return refresh_claude_titles(result),note


def agy_sessions(recent=False):
    from .agy_transport import sessions as listing
    return listing(recent)


def discover(recent=False):
    rows=[]; errors={}
    jobs={'claude':claude_sessions,'codex':codex_sessions,'agy':agy_sessions}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures={provider:pool.submit(fn,recent) for provider,fn in jobs.items()}
        for provider,future in futures.items():
            try:
                found,note=future.result()
                rows.extend(found)
                if note:
                    errors[provider]=note
            except Exception as error:
                # CLI output and auth errors can contain credentials; expose only error types.
                errors[provider]=f'세션 조회 실패 ({type(error).__name__})'
    last={(row['provider'],row['session_id']):row for row in read_json('tasks.json',[])}
    for row in rows:
        if not row.get('model'):
            row['model']=last.get((row['provider'],row['session_id']),{}).get('model')
            row['model_source']='hook_reported' if row['model'] else None
    priority={'진행 중':0,'승인 대기':1,'질문 대기':1,'입력 대기':1,'대기':2}
    rows.sort(key=lambda row:({'claude':0,'codex':1,'agy':2}[row['provider']],priority.get(row['state'],3),-row['updated_at'],row['code']))
    catalog={'rows':rows,'errors':errors,'fetched_at':time.time(),'recent':recent}
    write_json('session-catalog.json',catalog)
    return catalog


def selected():
    item=read_json('ui.json',{}).get('selected_session')
    return item if isinstance(item,dict) and item.get('provider') in ('claude','codex','agy') and (item.get('session_id') or item.get('target_kind')=='claude_peer' and item.get('peer_ref')) else None


def watched():
    rows=read_json('ui.json',{}).get('watched_sessions')
    return rows if isinstance(rows,list) else []


def watch_many(codes,catalog=None):
    if not isinstance(codes,list) or len(codes)>300 or any(not isinstance(code,str) for code in codes):
        raise ValueError('체크할 세션 코드 목록이 필요합니다.')
    catalog=catalog or read_json('session-catalog.json',{})
    if codes and (not catalog.get('rows') or time.time()-catalog.get('fetched_at',0)>600):
        catalog=discover()
    candidates=catalog.get('rows',[])
    previous=watched() or []
    candidates=candidates+[item for item in previous if not any(identity(item)==identity(row) for row in candidates)]
    result=[]
    for code in dict.fromkeys(value.strip().upper() for value in codes):
        matches=[row for row in candidates if row['code']==code]
        if len(matches)!=1:
            raise ValueError('체크한 세션을 찾을 수 없습니다. 목록을 새로고침해 주세요.')
        result.append(matches[0])
    with locked('view.lock'):
        ui=read_json('ui.json',{})
        ui['watched_sessions']=result
        if len(result)==1:
            ui['selected_session']=result[0]
        elif not any(identity(ui.get('selected_session'))==identity(row) for row in result):
            ui.pop('selected_session',None)
        write_json('ui.json',ui)
    return result


def receives_notifications(provider,payload):
    rows=watched()
    session=payload.get('session_id'); peer=payload.get('peer_ref')
    return any(row['provider']==provider and (
        session and row.get('session_id')==session or peer and row.get('peer_ref')==peer) for row in rows)


def watch_text(rows=None):
    rows=watched() if rows is None else rows
    return f'결과 수신: 체크한 {len(rows)}개 작업\n'+'\n'.join(f"[✓] {row['code']} · {row['name']}" for row in rows)


def validate_target(row):
    if row.get('provider')=='agy':
        from .agy_transport import validate
        return validate(row)
    if row.get('target_kind')=='claude_peer' and row.get('provider')=='claude':
        peers=claude_targets().get('targets',[])
        matches=[peer for peer in peers if peer.get('ref')==row.get('peer_ref')]
        if len(matches)!=1:
            raise ValueError('선택한 Claude 앱 세션의 연결이 사라짐. 새로고침 후 다시 선택해 주세요.')
        return peer_row(matches[0])
    session_id=str(uuid.UUID(row['session_id']))
    if row['provider']=='codex':
        with CodexReader(prefer_shared=True) as client:
            result=client.call('thread/read',{'threadId':session_id,'includeTurns':False})
            verified=codex_row(result.get('thread',{}),client.shared)
        if not verified or verified['session_id']!=session_id:
            raise ValueError('원래 Codex 세션이 없거나 직접 입력할 수 없음.')
        return verified
    rows,_=claude_sessions(recent=True)
    matches=[item for item in rows if item['session_id']==session_id]
    if len(matches)!=1:
        raise ValueError('원래 Claude 세션을 찾지 못함. 세션 목록을 새로고침해 주세요.')
    if matches[0].get('reachable') is not True:
        raise ValueError('작업 목록은 불러왔지만 메시지 수신 연결이 없습니다. 원래 Claude 앱/터미널에서 해당 작업을 열고 새로고침해 주세요.')
    if row.get('peer_ref') and row['peer_ref']!=matches[0].get('peer_ref'):
        raise ValueError('Claude 수신 세션이 변경됨. 목록을 새로고침하고 다시 선택해 주세요.')
    return matches[0]


def choose(selector,catalog=None):
    catalog=catalog or read_json('session-catalog.json',{})
    if not catalog.get('rows') or time.time()-catalog.get('fetched_at',0)>600:
        catalog=discover()
    selector=selector.strip().upper()
    matches=[row for row in catalog['rows'] if row['code']==selector or row.get('session_id') and row['session_id'].upper()==selector]
    if len(matches)!=1:
        raise ValueError('세션 코드가 없거나 모호함. sessions로 확인 후 C-… / X-… / G-… 코드를 선택해 주세요.')
    row=validate_target(matches[0])
    with locked('view.lock'):
        ui=read_json('ui.json',{})
        ui['selected_session']=row
        write_json('ui.json',ui)
    return row


def selection_text(row):
    provider={'claude':'Claude Code','codex':'Codex','agy':'Antigravity'}[row['provider']]
    return f"선택됨 · {provider}\n{row['name']}\n{row['code']} · {row['state']} · 모델 {row.get('model') or '미제공'}\n보내기: 톡턴 보내 내용"


def clear():
    with locked('view.lock'):
        ui=read_json('ui.json',{})
        ui.pop('selected_session',None)
        write_json('ui.json',ui)
    return '선택 세션 해제됨.'


def send_selected(message,expected=None):
    name,receipt=forward_selected(message,expected)
    from .commands import forward_response
    return name+'\n'+forward_response(receipt)


def forward_selected(message,expected=None,*,still_current=None):
    from .reply_bridge import SessionUnavailable,forward
    if not isinstance(message,str) or not message.strip():
        raise ValueError('전달할 내용 필요.')
    try:
        row=selected()
        if not row:
            raise ValueError('먼저 세션을 선택해 주세요. 톡턴 세션 → 톡턴 선택 세션코드')
        if expected is not None and identity(expected)!=identity(row):
            raise ValueError('다른 화면에서 선택한 작업이 바뀌었습니다. 보낼 작업을 다시 클릭해 주세요.')
        row=validate_target(row)
    except (ValueError,RuntimeError,OSError) as error:
        if still_current is not None:
            raise SessionUnavailable('보낼 작업을 확인하지 못했습니다. 작업을 다시 선택하세요.') from error
        raise
    if still_current is not None and (not still_current() or identity(selected())!=identity(row)):
        raise SessionUnavailable('연결이나 선택한 작업이 바뀌었습니다.')
    receipt=forward({'source':row['provider'],'session_id':row['session_id'],'cwd':row.get('cwd',''),
                     'peer_ref':row.get('peer_ref'),'target_kind':row.get('target_kind'),'name':row.get('name')},message)
    if receipt.get('accepted') is not True:
        raise RuntimeError('원래 작업의 접수를 확인하지 못했습니다.')
    write_json('last-selected-send.json',{'provider':row['provider'],'session_id':row['session_id'],'accepted':True,
                                          'stage':'queued' if receipt.get('stage')=='queued' else 'accepted','at':time.time()})
    return row['name'],receipt


def phone_text(catalog):
    """Kakao view: sessions that can receive now, plus checked ones. The full list is mostly saved history."""
    checks=watched()
    lines=[]
    for row in catalog['rows']:
        checked=any(identity(item)==identity(row) for item in checks)
        if row.get('live') or checked or row['provider']=='agy' and row.get('reachable'):
            label={'claude':'Claude Code','codex':'Codex','agy':'Antigravity'}[row['provider']]
            lines.append(f"{'[✓]' if checked else '[ ]'} {row['code']} · {row['name']}\n  {label} · {row['state']}")
    lines=[f'지금 연결할 수 있는 세션 {len(lines)}개 · [✓] 결과 받는 중',*lines,*catalog['errors'].values(),
           '','체크 C-… X-… G-…  → 받을 세션 바꾸기\n화면  → PC 화면 스샷\n세션 all  → 저장된 세션까지 전부']
    return '\n'.join(lines)


def list_text(catalog=None,provider='all'):
    catalog=catalog if catalog is not None else discover()
    current=selected()
    checks=watched()
    lines=[]
    for key,label in [('claude','Claude Code'),('codex','Codex'),('agy','Antigravity')]:
        if provider!='all' and provider!=key:
            continue
        rows=[row for row in catalog['rows'] if row['provider']==key]
        lines.append(f'{label} · 세션 {len(rows)}개')
        for row in rows:
            checked=any(identity(item)==identity(row) for item in checks)
            mark=('[✓]' if checked else '[ ]')+(' →' if identity(current)==identity(row) else '')
            lines.append(f"{mark} {row['code']}  {row['name']}\n  {row['state']} · 모델 {row.get('model') or '미제공'}")
        if not rows:
            lines.append('열린 세션 없음')
        if catalog['errors'].get(key):
            lines.append(catalog['errors'][key])
        lines.append('')
    lines.append('결과 수신 체크: 톡턴 체크 C-… X-… G-…\n메시지 대상: 톡턴 선택 C-… / X-… / G-…\n전달: 톡턴 보내 내용\n최근 저장 세션: 톡턴 세션 recent')
    return '\n'.join(lines)
