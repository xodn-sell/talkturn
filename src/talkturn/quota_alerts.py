"""Ask before switching accounts when a fresh included quota reaches zero."""
import hashlib
import json
import re
import time
import uuid
from .runtime import ROOT, read_json, write_json, locked


def exhausted(data):
    """Only main quota windows; stale/error/missing values never count as zero."""
    result={}
    claude=data.get('claude',{})
    for account in claude.get('accounts',[]):
        age=account.get('age_seconds')
        if not account.get('active') or account.get('state')!='ok' or not isinstance(age,(int,float)) or not 0<=age<=600:
            continue
        windows=[item for item in account.get('limits',[]) if item.get('name') in ('5시간','7일') and item.get('remaining_pct')==0]
        if windows:
            result['claude']={'account':account.get('number'),'windows':windows}
    codex=data.get('codex',{})
    if codex.get('state')!='unavailable':
        windows=[item for item in codex.get('limits',[]) if item.get('name','').startswith('codex ') and item.get('remaining_pct')==0]
        if windows:
            result['codex']={'windows':windows}
    return result


def reply_command(quote,body):
    """The command a reply to an alert bubble stands for. None: the quote is not an alert.
    True: an alert, the body is an ordinary command. Local text only; nothing is switched here."""
    quote=''.join(quote.split()).lower()
    # ponytail: the first line alone names the alert; a note of the user's own that starts the same way
    # would count too. Results and session cards start with "[title]", so they never match.
    if not re.match(r'c[li1|!]audecode|chatgpt.{0,2}codex',quote) or not re.search('잔여|한도|0%',quote):
        return None
    provider='claude' if quote.startswith('c') and not quote.startswith('chat') else 'codex'
    offered=read_json('quota-alerts.json',{}).get('offers',{}).get(provider) or []
    word=''.join(body.split()).lower().rstrip('.!~?')
    if word in ('유지','아니','아니야','ㄴㄴ','no','n'):
        return ['keep']
    if word.isdigit() and int(word) in offered:
        return ['switch',provider,word]
    if word and re.fullmatch(r'(어|응|ㅇ+|네|예|그래|좋아|ㅇㅋ|ok|okay|yes|y)?(해줘|해|전환|전환해줘|전환해|바꿔|바꿔줘)?',word):
        # "Yes" means the account the alert named for it; with nothing offered, show the quota instead.
        return ['switch',provider,str(offered[0])] if offered else ['usage',provider]
    return True


def _options(provider,accounts,claude_usage):
    """Accounts the alert offers, the one with the most confirmed quota first."""
    candidates=[row for row in accounts if not row.get('active') and not row.get('disabled')]
    fresh={row['number']:row for row in claude_usage.get('accounts',[]) if row.get('state')=='ok' and isinstance(row.get('age_seconds'),(int,float)) and 0<=row['age_seconds']<=600}
    options=[]
    for row in candidates:
        remaining=None
        if provider=='claude' and row['number'] in fresh:
            quotas=[item.get('remaining_pct') for item in fresh[row['number']].get('limits',[]) if item.get('name') in ('5시간','7일')]
            if any(value==0 for value in quotas):
                continue
            if len(quotas)==2 and all(isinstance(value,(int,float)) for value in quotas):
                remaining=min(quotas)
        options.append((row,remaining))
    options.sort(key=lambda item:item[1] if item[1] is not None else -1,reverse=True)
    return options[:5]


def prompt_text(provider,zero,accounts,claude_usage):
    label='Claude Code' if provider=='claude' else 'ChatGPT · Codex'
    current=next((row for row in accounts if row.get('active')),None)
    lines=[f"{label} · 잔여 한도 0%",'소진 구간: '+', '.join(item['name'] for item in zero['windows'])]
    if current:
        lines.append(f"현재 계정: {current['number']} {current['label']}")
    options=_options(provider,accounts,claude_usage)
    if options:
        lines.append('\n다른 계정으로 전환할까?')
        lines.append(f"이 말풍선에 답장: 어 = {options[0][0]['number']}번으로 전환 · 번호 = 그 계정 · 유지 = 그대로")
        for row,value in options[:5]:
            note=f'주요 한도 최소 잔여 {value:g}%' if value is not None else '잔여 한도 미확인'
            lines.append(f"{row['number']} {row['label']} · {note}\n  톡턴 전환 {provider} {row['number']}")
        lines.append('현재 계정 유지: 톡턴 유지')
    else:
        lines.append('전환 후보가 없거나 다른 계정 한도도 소진됨. 초기화를 기다리거나 PC에서 계정 추가 필요.')
    return '\n'.join(lines)


def maybe_prompt(*,now=None):
    from . import providers
    from .plain_delivery import deliver
    now=time.time() if now is None else now
    if not (ROOT/'enabled').exists() or read_json('delivery.json',{}).get('quota_prompt_enabled',True) is False:
        return 0
    with locked('quota.lock'):
        state=read_json('quota-alerts.json',{})
        if now-state.get('checked_at',0)<300:
            return 0
        state['checked_at']=now
        write_json('quota-alerts.json',state)
        data=providers.usage()
        zeros=exhausted(data)
        active=state.setdefault('active_episodes',{})
        # A confirmed recovery re-arms the next exhaustion. Retrieval failures do not.
        for provider in ('claude','codex'):
            block=data.get(provider,{})
            if provider=='codex':
                values=[window.get('remaining_pct') for window in block.get('limits',[]) if window.get('name','').startswith('codex ')]
                recovered=bool(block.get('fetched_at') and values) and all(isinstance(value,(int,float)) and value>0 for value in values)
            else:
                recovered=False
                for row in block.get('accounts',[]):
                    values=[window.get('remaining_pct') for window in row.get('limits',[]) if window.get('name') in ('5시간','7일')]
                    if row.get('active') and row.get('state')=='ok' and isinstance(row.get('age_seconds'),(int,float)) and 0<=row['age_seconds']<=600:
                        recovered=len(values)==2 and all(isinstance(value,(int,float)) and value>0 for value in values)
            if recovered and provider not in zeros:
                active.pop(provider,None)
                state.get('offers',{}).pop(provider,None)
        accounts=providers.accounts() if zeros else {}
        sent=0
        for provider,zero in zeros.items():
            rows=accounts.get(provider,[])
            current=next((row.get('number') for row in rows if row.get('active')),zero.get('account'))
            # Window names only: `resets_at` is recomputed on every usage fetch, which re-sent the alert every check.
            episode=hashlib.sha256(json.dumps({'account':current,'windows':sorted(item['name'] for item in zero['windows'])},sort_keys=True).encode()).hexdigest()
            if active.get(provider)==episode:
                continue
            if not (ROOT/'enabled').exists() or not (ROOT/'reply-enabled').exists():
                break
            # Persist before delivery: do not duplicate a message after an uncertain send.
            active[provider]=episode
            # What a reply of "yes" or a bare number to this alert may switch to.
            state.setdefault('offers',{})[provider]=[row['number'] for row,_ in _options(provider,rows,data.get('claude',{}))]
            write_json('quota-alerts.json',state)
            deliver(prompt_text(provider,zero,rows,data.get('claude',{})),
                    'quota-'+uuid.uuid4().hex,render_long=False)
            sent+=1
        write_json('quota-alerts.json',state)
        return sent
