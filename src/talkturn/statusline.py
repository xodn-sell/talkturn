"""Claude status line filter: keep each account's rate limits, hand the JSON on unchanged.

Claude Code gives subscription limits (5-hour and 7-day) to the status line command only.
No tokens are read: the account comes from the metadata in .claude.json.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time


def account_key(email, organization):
    return hashlib.sha256(f'{str(email).lower()}|{organization or ""}'.encode()).hexdigest()[:16]


def current_account():
    # ponytail: a session started before an account switch counts as the newly saved login until it restarts.
    folder = os.environ.get('CLAUDE_CONFIG_DIR')
    path = Path(folder) / '.claude.json' if folder else Path.home() / '.claude.json'
    account = json.loads(path.read_text(encoding='utf-8-sig')).get('oauthAccount') or {}
    return account_key(account['emailAddress'], account.get('organizationUuid')) if account.get('emailAddress') else None


def record(data):
    from .runtime import locked, read_json, write_json
    limits = data.get('rate_limits') if isinstance(data, dict) else None
    windows = {}
    for name in ('five_hour', 'seven_day'):
        window = limits.get(name) if isinstance(limits, dict) else None
        if isinstance(window, dict) and type(window.get('used_percentage')) in (int, float):
            windows[name] = {'used': window['used_percentage'], 'resets_at': window.get('resets_at')}
    key = windows and current_account()
    if not key:
        return
    with locked('claude-usage.lock', timeout=2):
        seen = read_json('claude-usage.json', {})
        seen = seen if isinstance(seen, dict) else {}
        previous = seen.get(key, {})
        for name, window in windows.items():
            # Use only grows inside one window: an idle session's older value must not undo a newer one.
            old = previous.get('windows', {}).get(name)
            if old and old.get('resets_at') == window['resets_at'] and old['used'] > window['used']:
                windows[name] = old
        if previous.get('windows') != windows:
            seen[key] = {'windows': windows, 'order': previous.get('order', len(seen) + 1), 'at': time.time()}
            write_json('claude-usage.json', seen)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--home')
    parser.add_argument('--pass', dest='pass_on', action='store_true', help='print the JSON for the next status line')
    args = parser.parse_args()
    if args.home:
        os.environ['TALKTURN_HOME'] = args.home
        from . import runtime
        runtime.ROOT = Path(args.home).resolve()
    raw = sys.stdin.buffer.read()
    try:
        data = json.loads(raw)
        record(data)
        # ASCII survives PowerShell 5.1 pipes, which Claude Code uses when Git Bash is absent.
        raw = json.dumps(data, ensure_ascii=True).encode()
    except Exception:  # noqa: BLE001 - a recording failure must never blank the user's status line
        pass
    if args.pass_on:
        sys.stdout.buffer.write(raw)


if __name__ == '__main__':
    main()
