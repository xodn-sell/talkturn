"""Allowlisted provider operations. Never accept shell commands or credentials from chat."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
import tomllib
from .codex_rpc import CodexReader
from .statusline import account_key
from .runtime import ROOT, NO_WINDOW, executable, read_json, write_json, run_json, locked, atomic_write


def remaining(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return round(max(0, min(100, 100 - value)), 1)


def reset_time(value):
    if value is None:
        return '미제공'
    try:
        instant = datetime.fromtimestamp(value).astimezone() if isinstance(value, (int, float)) else datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone()
        return instant.strftime('%m/%d %H:%M')  # local time; Windows spells %Z out ("대한민국 표준시")
    except (ValueError, TypeError, OverflowError, OSError):
        return '미제공'


def ccswap_accounts():
    return run_json([executable('ccswap'), 'list', '--json'], timeout=45)


def public_account(account):
    # Never return raw email, organization identifiers, credential fields or CLI errors.
    alias = account.get('alias') or account.get('label') or f"계정 {account.get('number', '?')}"
    return {'number': account.get('number'), 'label': str(alias), 'active': bool(account.get('active')),
            'disabled': bool(account.get('disabled')), 'plan': account.get('planType')}


def accounts():
    raw = ccswap_accounts()
    return {provider: [public_account(row) for row in raw.get(provider, {}).get('accounts', [])] for provider in ('claude', 'codex')}


def login_state(provider):
    if provider == 'agy':
        return None  # Session metadata is not proof of a current authenticated account.
    if provider not in ('claude','codex'):
        raise ValueError('로그인 대상은 claude 또는 codex입니다.')
    command=[executable(provider)]+(['auth','status','--json'] if provider=='claude' else ['login','status'])
    try:
        result=subprocess.run(command,capture_output=True,encoding='utf-8',timeout=15,creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise TimeoutError('로그인 상태 확인 시간이 초과됐습니다. 잠시 후 다시 시도하세요.') from None
    if provider=='codex':
        return result.returncode==0
    try:
        return result.returncode==0 and json.loads(result.stdout).get('loggedIn') is True
    except (ValueError,AttributeError):
        raise RuntimeError('Claude 로그인 상태를 확인하지 못했습니다. Claude 터미널에서 로그인 상태를 확인하세요.') from None


def add_account(provider,alias=None):
    """Register the current login through ccswap, without reading credentials here."""
    if provider not in ('claude','codex'):
        raise ValueError('계정 종류는 claude 또는 codex입니다.')
    if alias is not None and (not isinstance(alias,str) or not 1<=len(alias)<=40 or any(ord(char)<32 or ord(char)==127 for char in alias)):
        raise ValueError('계정 별칭은 1~40자로 입력하세요.')
    with locked('settings.lock'):
        command=[executable('ccswap')]+(['codex'] if provider=='codex' else [])+['add']
        if alias and provider=='claude':
            command+=['--alias='+alias]
        # No slot/force options: ccswap chooses a free slot or refreshes the same login.
        try:
            result=subprocess.run(command,stdin=subprocess.DEVNULL,capture_output=True,
                timeout=45,creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise TimeoutError('계정 등록 확인 시간이 초과됐습니다. 계정 목록에서 등록 여부를 먼저 확인하세요.') from None
        if result.returncode:
            detail=((result.stdout or b'')+(result.stderr or b'')).decode('utf-8',errors='replace').lower()
            if 'api key' in detail or 'api-key' in detail:
                raise RuntimeError('현재 인증은 API 키입니다. 이 메뉴에서는 Claude 구독 계정으로 로그인한 뒤 등록하세요.')
            if 'alias' in detail and ('used' in detail or 'exist' in detail):
                raise RuntimeError('이미 쓰는 계정 별칭입니다. 다른 별칭으로 등록하세요.')
            if 'login' in detail or 'log in' in detail or 'credentials' in detail or 'active claude' in detail:
                raise RuntimeError('저장할 로그인을 찾지 못했습니다. A → 새 계정 로그인 후 추가를 선택하세요.')
            raise RuntimeError('ccswap 계정 등록 실패. 기존 계정은 유지됩니다. ccswap add에서 상세 원인을 확인하세요.')
        current=ccswap_accounts().get(provider,{})
        active=[row for row in current.get('accounts',[]) if row.get('number')==current.get('activeAccountNumber')]
        if len(active)!=1:
            raise RuntimeError('등록 후 활성 계정 확인 실패. 계정 목록을 새로고침해 주세요.')
    return public_account(active[0])


def session_accounts():
    """Limits the Claude status line last saw, one row per account, windows not yet reset."""
    now, rows = time.time(), {}
    for key, entry in (read_json('claude-usage.json', {}) or {}).items():
        windows = entry.get('windows', {})
        limits = [{'name': name, 'remaining_pct': remaining(window.get('used')), 'resets_at': window['resets_at']}
                  for name, window in (('5시간', windows.get('five_hour')), ('7일', windows.get('seven_day')))
                  if isinstance(window, dict) and isinstance(window.get('resets_at'), (int, float)) and window['resets_at'] > now]
        if limits:
            rows[key] = {'order': entry.get('order', 0), 'limits': limits}
    return rows


def claude_usage():
    seen = session_accounts()
    try:
        rows = ccswap_accounts().get('claude', {}).get('accounts', [])
    except (RuntimeError, TimeoutError, OSError):
        if not seen:
            raise RuntimeError('아직 기록 없음. Claude 창에서 답을 한 번 받으면 표시됩니다.') from None
        rows = []  # ccswap is optional: the status line already covers the accounts in use.
    result = []
    for row in rows:
        public = public_account(row)
        session = seen.pop(account_key(row['email'], row.get('organizationUuid')), None) if row.get('email') else None
        good = row.get('usageStatus') == 'ok' and isinstance(row.get('usage'), dict)
        usage = row.get('usage') if good else row.get('lastGoodUsage')
        public.update({'source': 'ccswap', 'state': 'ok' if good else ('cached' if usage else 'unavailable'),
                       'fetched_at': row.get('usageFetchedAt') if good else row.get('lastGoodFetchedAt'),
                       'age_seconds': row.get('usageAgeSeconds') if good else row.get('lastGoodAgeSeconds'), 'limits': []})
        public['active'] = public['active'] or session is not None
        if session and not good:
            public.update({'source': 'session', 'state': 'ok', 'limits': session['limits']})
            usage = None
        if isinstance(usage, dict):
            windows = [('5시간', usage.get('fiveHour')), ('7일', usage.get('sevenDay'))]
            windows += [(str(item.get('name', '모델별')), item) for item in (usage.get('scoped') or []) if isinstance(item, dict)]
            for name, item in windows:
                if isinstance(item, dict):
                    public['limits'].append({'name': name, 'remaining_pct': remaining(item.get('pct')), 'resets_at': item.get('resetsAt')})
        result.append(public)
    for session in sorted(seen.values(), key=lambda row: row['order']):
        result.append({'label': '등록 안 된 계정' if rows else f"계정 {session['order']}", 'active': True,
                       'source': 'session', 'state': 'ok', 'limits': session['limits']})
    return {'accounts': result, 'exact_remaining_tokens': None}


def codex_usage():
    with CodexReader() as client:
        limits = client.call('account/rateLimits/read')
    if not isinstance(limits,dict):
        raise RuntimeError('Codex 사용량 응답 형식을 확인할 수 없음.')
    groups = limits.get('rateLimitsByLimitId') or {'codex': limits.get('rateLimits', {})}
    if not isinstance(groups,dict):
        raise RuntimeError('Codex 사용량 한도 응답 형식을 확인할 수 없음.')
    windows = []
    for key, group in groups.items():
        if not isinstance(group, dict):
            continue
        for period in ('primary', 'secondary'):
            item = group.get(period)
            if not isinstance(item, dict):
                continue
            minutes = item.get('windowDurationMins')
            duration = '7일' if minutes == 10080 else '5시간' if minutes == 300 else (f'{minutes}분' if minutes else period)
            windows.append({'name': f'{key} {duration}', 'remaining_pct': remaining(item.get('usedPercent')), 'resets_at': item.get('resetsAt')})
    return {'source': 'Codex account API', 'limits': windows, 'fetched_at': datetime.now().astimezone().isoformat(),
            'exact_remaining_tokens': None}


def usage(provider='all'):
    jobs = {'claude': claude_usage, 'codex': codex_usage,
            'agy': lambda: {'state':'unavailable','error':'AGY 사용량은 원래 agy 창의 /usage에서 확인하세요.'}}
    if provider != 'all' and provider not in jobs:
        raise ValueError('사용량 대상: claude, codex, agy 또는 all')
    if provider != 'all':
        jobs = {provider: jobs[provider]}
    results = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {key: pool.submit(fn) for key, fn in jobs.items()}
        for key, future in futures.items():
            try:
                results[key] = future.result()
            except (RuntimeError, TimeoutError, OSError, ValueError, TypeError, AttributeError) as error:
                results[key] = {'state': 'unavailable', 'error': str(error) if isinstance(error, RuntimeError) else type(error).__name__}
    return results


def switch_account(provider, number, *, expected_label=None):
    if provider not in ('claude', 'codex') or not re.fullmatch(r'[1-9][0-9]{0,4}', str(number)):
        raise ValueError('전환 형식: switch claude 2 또는 switch codex 1')
    with locked('settings.lock'):
        rows = ccswap_accounts().get(provider, {}).get('accounts', [])
        matching = [row for row in rows if row.get('number') == int(number)]
        if len(matching) != 1 or matching[0].get('disabled'):
            raise ValueError('등록되지 않았거나 비활성화된 계정 번호.')
        if expected_label is not None and public_account(matching[0])['label']!=expected_label:
            raise ValueError('계정 목록이 바뀌었습니다. 새로고침한 뒤 전환할 계정을 다시 골라주세요.')
        command = [executable('ccswap')] + (['codex'] if provider == 'codex' else []) + ['switch', str(number), '--json']
        result = run_json(command, timeout=45)
        # Verify the persisted active account, never infer success from exit code alone.
        current = ccswap_accounts().get(provider, {})
        if current.get('activeAccountNumber') != int(number):
            raise RuntimeError('계정 전환 후 활성 계정 확인 실패.')
    return {'provider': provider, 'account': int(number), 'switched': bool(result.get('switched')),
            'scope': '저장된 로그인 전환. 이미 실행 중인 세션은 재시작이 필요할 수 있음. 환경변수 인증은 별도.'}


def config_path(provider):
    if provider == 'claude':
        return Path(os.environ.get('CLAUDE_CONFIG_DIR', Path.home() / '.claude')) / 'settings.json'
    if provider == 'codex':
        return Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')) / 'config.toml'
    if provider == 'agy':
        return Path.home() / '.gemini/antigravity-cli/settings.json'
    raise ValueError('지원하지 않는 설정 대상입니다.')


def provider_config(provider):
    path = config_path(provider)
    if not path.exists():
        return {}
    text = path.read_text(encoding='utf-8-sig')
    return tomllib.loads(text) if provider == 'codex' else json.loads(text)


def replace_toml_root(text, key, value):
    """Change one root key and assert all other parsed settings are preserved."""
    before = tomllib.loads(text)
    replacement = f'{key} = {json.dumps(value, ensure_ascii=False)}\n'
    table = re.search(r'^\s*\[', text, re.M)
    boundary = table.start() if table else len(text)
    root, rest = text[:boundary], text[boundary:]
    pattern = re.compile(r'^' + re.escape(key) + r'\s*=.*(?:\n|$)', re.M)
    if key in before:
        match = pattern.search(root)
        if not match:
            raise RuntimeError(f'{key} 설정 형식을 자동 변경할 수 없음.')
        # Supports multiline notify arrays without touching later settings.
        end = match.end()
        while True:
            try:
                candidate = root[:match.start()] + replacement + root[end:] + rest
                parsed = tomllib.loads(candidate)
                if {k:v for k,v in parsed.items() if k != key} == {k:v for k,v in before.items() if k != key} and parsed.get(key) == value:
                    return candidate
            except tomllib.TOMLDecodeError:
                pass
            newline = root.find('\n', end)
            if newline < 0:
                raise RuntimeError(f'{key} 변경 시 다른 설정 보존 확인 실패.')
            end = newline + 1
    result = replacement + text
    assert tomllib.loads(result) == {**before, key: value}
    return result


def update_provider_setting(provider, key, value):
    if provider not in ('claude','codex'):
        raise ValueError('AGY 설정은 원래 agy 창의 /config에서 변경하세요.')
    path = config_path(provider)
    original = path.read_text(encoding='utf-8-sig') if path.exists() else ('{}' if provider == 'claude' else '')
    if provider == 'claude':
        data = json.loads(original)
        data[key] = value
        updated = json.dumps(data, ensure_ascii=False, indent=2) + '\n'
    else:
        updated = replace_toml_root(original, key, value)
    if path.exists():
        atomic_write(ROOT / 'backups' / f'{provider}-{key}-{time.time_ns()}.bak', original)
    atomic_write(path, updated)


def model_settings():
    settings = {}
    for provider in ('codex', 'claude'):
        data = provider_config(provider)
        settings[provider] = {'model': data.get('model', 'provider default'),
            'effort': data.get('model_reasoning_effort' if provider == 'codex' else 'effortLevel', 'provider default')}
    try:
        agy = provider_config('agy')
        settings['agy'] = {'model': agy.get('model') or 'provider default', 'effort': agy.get('effort') or 'provider default'}
    except (OSError, ValueError, AttributeError):
        settings['agy'] = {'model':'설정 미확인', 'effort':'미확인'}
    settings['relay'] = {'model': read_json('delivery.json', {}).get('model', 'gpt-6-luna')}
    settings['scope'] = 'Codex·Claude는 새 작업의 사용자 기본값. 세션·프로젝트·환경변수 설정이 우선할 수 있음.'
    return settings


def models(provider):
    if provider == 'agy':
        try:
            result = subprocess.run([executable('agy'), 'models'], capture_output=True, encoding='utf-8',
                                    timeout=20, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        except subprocess.TimeoutExpired:
            raise RuntimeError('AGY 모델 조회 시간이 초과됐습니다. 원래 터미널에서 agy models를 확인하세요.') from None
        if result.returncode:
            raise RuntimeError('AGY 모델 조회 실패. 원래 터미널에서 agy models를 확인하세요.')
        values = [match[1] for line in result.stdout.splitlines()
                  if (match := re.match(r'^([a-z][a-z0-9._:/-]+)\s+\S', line.strip()))]
        if not values:
            raise RuntimeError('AGY 모델 목록 형식 미확인. 원래 터미널에서 agy models를 확인하세요.')
        return list(dict.fromkeys(values))
    if provider in ('codex', 'relay'):
        try:
            with CodexReader() as client:
                result = client.call('model/list')
            available=[row['model'] for row in result.get('data', []) if row.get('model') and not row.get('hidden')]
        except (RuntimeError,TimeoutError,OSError):
            if provider!='relay':
                raise
            available=[]
        return available+(['claude-haiku-5-5'] if provider=='relay' else [])
    if provider == 'claude':
        allowed = provider_config('claude').get('availableModels')
        return allowed or ['default', 'best', 'fable', 'opus', 'sonnet', 'haiku', 'opusplan', 'claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5']
    raise ValueError('모델 대상: codex, claude, relay')


def set_model(provider, model):
    if provider == 'agy':
        raise ValueError('AGY 모델은 원래 창의 /model 또는 talkturn agy --model 모델ID로 선택하세요.')
    if provider not in ('codex', 'claude', 'relay') or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._:/\[\]-]{0,150}', model):
        raise ValueError('올바른 모델 이름 필요.')
    if provider in ('codex', 'relay') and not(provider=='relay' and model=='claude-haiku-5-5') and model not in models(provider):
        raise ValueError('현재 Codex 계정의 모델 목록에 없음. models 명령으로 확인.')
    with locked('settings.lock'):
        if provider == 'relay':
            data = read_json('delivery.json', {})
            data['model'] = model
            write_json('delivery.json', data)
        else:
            update_provider_setting(provider, 'model', model)
    return f'{provider} 모델 저장: {model}\n' + ('다음 답장 판별부터 적용.' if provider == 'relay' else '새 작업의 기본값에 적용. 현재 열린 세션의 모델은 유지됨.')


def set_effort(provider, effort):
    if provider == 'agy':
        raise ValueError('AGY 노력도는 원래 창의 /effort 또는 talkturn agy --effort 값으로 선택하세요.')
    choices = {'codex': {'minimal','low','medium','high','xhigh','max','ultra'}, 'claude': {'low','medium','high','xhigh','max'}}
    if provider not in choices or effort not in choices[provider]:
        raise ValueError('지원 노력도 확인: codex low/medium/high/xhigh/max/ultra, claude low/medium/high/xhigh/max. 모델별 지원 범위는 다름.')
    with locked('settings.lock'):
        update_provider_setting(provider, 'model_reasoning_effort' if provider == 'codex' else 'effortLevel', effort)
    return f'{provider} 노력도 저장: {effort}\n새 작업 기본값에 적용. 모델별 지원 범위에 따라 실행 시 거절될 수 있음.'
