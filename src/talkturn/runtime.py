"""User state, bounded process calls, and executable discovery."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

# Legacy environment names remain input aliases; never rewrite a user-supplied path.
ROOT = Path(os.environ.get('TALKTURN_HOME') or os.environ.get('TOKTURN_HOME') or Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local/share')) / 'TalkTurn').expanduser().resolve()
PYTHON = Path(sys.executable)
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def read_json(name, default=None):
    path = ROOT / name
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else default


def write_json(name, value):
    atomic_write(ROOT / name, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
        for attempt in range(6):
            try:
                os.replace(temp, path)
                break
            except PermissionError as error:
                # Windows readers may briefly deny replacement. Keep the old file intact;
                # permanent permission errors still surface after at most 620 ms of waiting.
                if getattr(error, 'winerror', None) not in (5, 32, 33) or attempt == 5:
                    raise
                time.sleep(.02 * 2**attempt)
    finally:
        Path(temp).unlink(missing_ok=True)


def executable(provider):
    # Intentional legacy alias for existing private executable overrides.
    override = os.environ.get('TALKTURN_' + provider.upper() + '_BIN') or os.environ.get('TOKTURN_' + provider.upper() + '_BIN')
    if provider=='agy' and override:
        path=Path(override)
        if not path.is_file() or path.suffix.lower()!='.exe':
            raise RuntimeError('TALKTURN_AGY_BIN은 설치된 agy.exe 경로여야 합니다.')
        return str(path)
    candidates = [Path(override)] if override else []
    found = shutil.which(provider + '.exe') or shutil.which(provider)
    if found:
        candidates.append(Path(found))
    candidates.append(Path.home() / '.local/bin' / (provider + '.exe'))
    if provider == 'agy':
        candidates.append(Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'agy/bin/agy.exe')
    # npm's cmd/PowerShell shims must never receive chat text through a shell.
    npm_roots = [Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming')) / 'npm/node_modules']
    if found:
        npm_roots.append(Path(found).parent / 'node_modules')
    for npm in npm_roots:
        if provider == 'claude':
            candidates.append(npm / '@anthropic-ai/claude-code/bin/claude.exe')
        elif provider == 'codex':
            candidates.extend((npm / '@openai/codex').glob('node_modules/@openai/codex-*/vendor/*/bin/codex.exe'))
    for candidate in candidates:
        if candidate.is_file() and candidate.suffix.lower() not in {'.cmd', '.bat', '.ps1'}:
            return str(candidate)
    raise RuntimeError(f'{provider} 실행 파일 없음. 설치 후 PATH 또는 TALKTURN_{provider.upper()}_BIN 설정 필요.')


def run_json(command, *, timeout=40, payload=None):
    try:
        result = subprocess.run(command, input=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                                capture_output=True, text=True, encoding='utf-8', timeout=timeout, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise TimeoutError('연동 명령 시간 초과') from None
    try:
        value = json.loads(result.stdout)
    except ValueError:
        raise RuntimeError('연동 명령이 JSON 응답을 반환하지 않음.') from None
    if result.returncode or isinstance(value, dict) and value.get('error'):
        # Raw CLI errors can include account data or credentials; do not echo them.
        raise RuntimeError(f'연동 명령 실패 (exit {result.returncode}). PC에서 로그인 상태 확인 필요.')
    return value


def module_command(name, *args):
    return [str(PYTHON), '-m', 'talkturn.' + name, *args]


@contextmanager
def locked(name='send.lock', timeout=100):
    import msvcrt
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / name).open('a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('TalkTurn 작업이 진행 중임.')
                time.sleep(.1)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
