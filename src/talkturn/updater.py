"""`talkturn update [버전]`: reinstall a version the user picks from GitHub Releases. Terminal only, never from KakaoTalk."""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

from . import __version__
from .runtime import ROOT

RELEASES = 'https://api.github.com/repos/xodn-sell/talkturn/releases'


def releases():
    request = urllib.request.Request(RELEASES, headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'talkturn'})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.load(response)
    except (OSError, ValueError) as error:
        raise RuntimeError('GitHub 릴리스 목록을 받지 못했습니다. 인터넷 연결을 확인하세요.') from error
    rows = []
    for item in data:  # GitHub lists the newest release first.
        wheel = next((asset['browser_download_url'] for asset in item.get('assets', []) if asset['name'].endswith('.whl')), None)
        if wheel and not item.get('draft'):
            rows.append({'version': item['tag_name'].removeprefix('v'), 'date': (item.get('published_at') or '')[:10], 'wheel': wheel})
    if not rows:
        raise RuntimeError('설치할 수 있는 릴리스가 없습니다.')
    return rows


def label(row, rows):
    marks = ['최신'] * (row is rows[0]) + ['지금 사용 중'] * (row['version'] == __version__)
    return ' · '.join([row['version'], row['date'], *marks])


def pick(rows, version):
    if version.lower() in ('latest', '최신'):
        return rows[0]
    match = next((row for row in rows if row['version'] == version.removeprefix('v')), None)
    if not match:
        raise ValueError(f"{version} 버전은 없습니다. 고를 수 있는 버전: {', '.join(row['version'] for row in rows)}")
    return match


def command(wheel):
    uv = shutil.which('uv')
    if uv:
        return [uv, 'pip', 'install', '--python', sys.executable, '--reinstall-package', 'talkturn', wheel]
    return [sys.executable, '-m', 'pip', 'install', '--force-reinstall', wheel]


def _service(code):
    # A fresh interpreter, so the receiver restarts with the newly installed code.
    return subprocess.run([sys.executable, '-c', 'from talkturn import service, mouse_lock; ' + code], check=True,
                          capture_output=True, text=True, encoding='utf-8', env={**os.environ, 'PYTHONIOENCODING': 'utf-8'}).stdout.strip()


def install(row):
    from . import mouse_lock, service
    was_running = service.process_running(service.worker_pid())
    was_locked = mouse_lock.active()
    notifications = (ROOT / 'enabled').exists()
    try:
        if was_running:
            service.stop()
            deadline = time.monotonic() + 90
            while service.process_running(service.worker_pid()):
                if time.monotonic() > deadline:
                    raise RuntimeError('수신기가 90초 안에 멈추지 않아 설치하지 않았습니다.')
                time.sleep(1)
        result = subprocess.run(command(row['wheel']))
        if result.returncode:
            raise RuntimeError(f"{row['version']} 설치 실패(종료 코드 {result.returncode}). 기존 버전을 그대로 씁니다.")
        if (ROOT / 'installed-hooks.json').exists():
            _service('service.install_hooks()')  # New releases may add hooks (e.g. the usage status line).
    finally:
        # Stopping the receiver also releases the mouse lock; leave both as the user had them.
        if was_running:
            _service('service.start(connect=False)')
            if not notifications:
                (ROOT / 'enabled').unlink(missing_ok=True)
            if was_locked:
                _service('mouse_lock.engage()')
    return f"TalkTurn {row['version']} 설치 완료. 열려 있는 talkturn 창은 닫고 다시 실행하세요."


def update(console, version=None):
    rows = releases()
    if version:
        return install(pick(rows, version))
    listing = '\n'.join(['TalkTurn 버전 (지금 ' + __version__ + ')'] + [label(row, rows) for row in rows]
                        + ['설치: talkturn update 버전  (예: talkturn update ' + rows[0]['version'] + ')'])
    if not sys.stdin.isatty():
        return listing
    from .terminal import choice_menu
    try:
        chosen = choice_menu(console, f'설치할 TalkTurn 버전 · 지금 {__version__}', [(row['version'], label(row, rows)) for row in rows])
    except EOFError:  # Windows reports NUL stdin as a terminal.
        return listing
    return install(pick(rows, chosen)) if chosen else '업데이트 취소.'
