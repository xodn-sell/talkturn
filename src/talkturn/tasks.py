"""Show provider work and limits; keep transport diagnostics out of the main screen."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import time
from .runtime import read_json, write_json, locked


def record(provider,payload,state):
    session=payload.get('session_id')
    if not session or payload.get('agent_id'):
        return
    with locked('tasks.lock'):
        rows=read_json('tasks.json',[])
        existing=next((item for item in rows if item['provider']==provider and item['session_id']==session),{})
        current={**existing,'provider':provider,'session_id':session,'state':state,'updated_at':time.time()}
        if payload.get('model'):
            current['model']=payload['model']
        if payload.get('cwd'):
            current['project']=Path(payload['cwd']).name
        rows=[item for item in rows if not(item['provider']==provider and item['session_id']==session)]
        write_json('tasks.json',(rows+[current])[-100:])


def account_counts():
    """Report each CLI's current login once; stored account slots are not logins."""
    from .providers import login_state
    counts={'claude':None,'codex':None}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures={provider:pool.submit(login_state,provider) for provider in counts}
        for provider,future in futures.items():
            try:
                logged_in=future.result()
                counts[provider]=int(logged_in) if type(logged_in) is bool else None
            except (RuntimeError,TimeoutError,OSError,ValueError):
                pass
    return counts


def snapshot(provider='all'):
    from .providers import usage,model_settings
    from .questions import pending
    from .sessions import discover,selected,watched
    catalog=discover()
    rows=[row for row in catalog['rows'] if provider=='all' or row['provider']==provider]
    waiting=[request for request in pending() if provider=='all' or request['provider']==provider]
    settings=model_settings()
    data=usage(provider)
    counts=account_counts()
    if 'accounts' in data.get('claude',{}):
        data['claude']['accounts']=[row for row in data['claude']['accounts'] if row.get('active')]
    return {'provider':provider,'rows':rows,'session_errors':catalog['errors'],'selected':selected(),'watched':watched(),
            'waiting':waiting,'models':settings,'usage':data,'account_counts':counts}


def status_text(provider='all'):
    from .commands import usage_text,SECTION_LABELS
    view=snapshot(provider)
    lines=[f'{SECTION_LABELS[provider]} · 실제 작업 세션']
    for row in view['rows']:
        label=row['name']
        lines.append(f"{row['code']} · {row['provider']} · {label}\n{row['state']} · 모델 {row.get('model') or '미제공'}")
    if not view['rows']:
        lines.append('열린 세션 없음.')
    for key,error in view['session_errors'].items():
        if provider=='all' or provider==key:
            lines.append(f'{SECTION_LABELS[key]}: {error}')
    if view['waiting']:
        lines.append('대기 질문: '+', '.join(request['id'] for request in view['waiting']))
    settings=view['models']
    keys=('codex','claude') if provider=='all' else (provider,)
    lines.append('\n새 작업 기본 모델: '+' / '.join(f"{SECTION_LABELS[key]} {settings[key]['model']}" for key in keys))
    lines.append('\n'+usage_text(view['usage']))
    return '\n'.join(lines)
