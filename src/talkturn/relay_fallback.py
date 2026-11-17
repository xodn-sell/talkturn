"""Retry an exhausted relay once using the other already authenticated provider."""
import json
import re
import time
from . import providers
from .runtime import read_json, write_json, locked


MODELS = {'gpt-6-luna': 'codex', 'claude-haiku-5-5': 'claude'}
QUOTA_CODES = {
    'insufficient_quota', 'usage_limit_reached', 'usage_limit_exceeded',
    'quota_exceeded', 'quota_exhausted', 'billing_hard_limit_reached',
}
QUOTA_MESSAGE = re.compile(
    r"^(?:error:\s*)?(?:you(?:'ve| have) hit your (?:usage )?limit\b|"
    r'you have (?:reached|exceeded) your usage limit\b|'
    r'you exceeded your current quota\b|'
    r'(?:daily |weekly |monthly )?usage limit (?:reached|exceeded)\b|'
    r'quota (?:exceeded|exhausted)\b)', re.I,
)


class RelayQuotaExceeded(RuntimeError):
    """A provider explicitly reported exhausted usage, not transient rate limiting."""


def _quota_error(value):
    if isinstance(value, str):
        return value.lower() in QUOTA_CODES or bool(QUOTA_MESSAGE.match(value.strip()))
    if isinstance(value, list):
        return any(_quota_error(item) for item in value)
    if not isinstance(value, dict):
        return False
    return any(_quota_error(value.get(key)) for key in ('code', 'type', 'message', 'error', 'errors', 'result'))


def quota_exhausted(stdout, stderr):
    """Inspect error envelopes and explicit stderr diagnostics, never success content."""
    for stream, allow_plain in ((stdout, False), (stderr, True)):
        if isinstance(stream, bytes):
            stream = stream.decode('utf-8', errors='replace')
        if not isinstance(stream, str):
            continue
        try:
            records = [json.loads(stream)]
        except ValueError:
            records = []
            for line in stream.splitlines():
                try:
                    records.append(json.loads(line))
                except ValueError:
                    if allow_plain and QUOTA_MESSAGE.match(line.strip()):
                        return True
        for record in records:
            if not isinstance(record, dict):
                continue
            failed = (record.get('is_error') is True or
                      record.get('type') in ('error', 'turn.failed', 'response.failed') or
                      not record.get('type') and isinstance(record.get('error'), dict))
            if failed and _quota_error(record):
                return True
    return False


def run_relay(primary, invoke):
    """Return invoke(model)'s result; fallback changes no account or login state."""
    try:
        return invoke(primary)
    except RelayQuotaExceeded:
        if primary not in MODELS:
            raise
        if read_json('delivery.json', {}).get('relay_auto_switch', True) is False:
            raise RelayQuotaExceeded('중계 사용 한도 소진. 자동 전환이 꺼져 있습니다.') from None

    alternate = next(model for model in MODELS if model != primary)
    provider = MODELS[alternate]
    try:
        authenticated = providers.login_state(provider)
    except (RuntimeError, OSError, TimeoutError):
        raise RelayQuotaExceeded(f'중계 사용 한도 소진. {provider} 로그인 확인 실패로 자동 전환하지 못했습니다.') from None
    if not authenticated:
        raise RelayQuotaExceeded(f'중계 사용 한도 소진. {provider}에 먼저 로그인해야 {alternate}로 전환할 수 있습니다.')
    try:
        result = invoke(alternate)
    except RelayQuotaExceeded:
        raise RelayQuotaExceeded('Luna·Haiku 중계 사용 한도가 모두 소진됐습니다. 한도 초기화 후 다시 시도하세요. 기존 모델 설정은 유지됩니다.') from None

    with locked('settings.lock'):
        current = read_json('delivery.json', {})
        saved = current.get('model', 'gpt-6-luna') == primary
        if saved:
            current['model'] = alternate
            write_json('delivery.json', current)
        write_json('relay-status.json', {'from_model': primary, 'used_model': alternate,
                   'model_saved': saved, 'attempts': 2, 'at': time.time()})
    return result
