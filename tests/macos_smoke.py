"""Installed-wheel smoke only: no native Kakao UI, real agent, or account access."""
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

from PIL import Image, ImageFont
import talkturn
from talkturn import render, runtime


def main():
    assert sys.platform == 'darwin', 'This smoke check requires a real macOS runner.'
    assert 'site-packages' in Path(talkturn.__file__).parts, 'Test the installed wheel, not the source tree.'
    font_path = Path(os.environ['TALKTURN_FONT'])
    font = ImageFont.truetype(str(font_path), 42)
    for glyph in '한글검증':
        assert bytes(font.getmask(glyph)) != bytes(font.getmask('\u0378')), 'Missing Korean glyph: ' + glyph
    checks = []
    for args, expected in [(['--version'], talkturn.__version__), (['--help'], 'TalkTurn'),
                           (['display', 'text'], '긴 글 표시: text'),
                           (['computer-name', 'macOS 검증 PC'], 'macOS 검증 PC')]:
        result = subprocess.run([sys.executable, '-m', 'talkturn', *args], capture_output=True,
                                encoding='utf-8', input='', timeout=15)
        assert result.returncode == 0 and expected in result.stdout, (args, result.returncode, result.stderr)
        checks.append({'args': args, 'exit': result.returncode})
    # No Kakao on the runner: setup must refuse cleanly (missing permission or window) and start nothing.
    result = subprocess.run([sys.executable, '-m', 'talkturn', 'setup', '--self-chat', 'Synthetic self chat',
                             '--relay-model', 'luna', '--no-startup'], input='', capture_output=True,
                            encoding='utf-8', timeout=15)
    assert result.returncode == 1 and ('맥 권한 필요' in result.stdout or '채팅 창' in result.stdout), (result.stdout, result.stderr)
    assert 'Traceback' not in result.stderr
    assert not (runtime.ROOT / 'enabled').exists()
    assert not (runtime.ROOT / 'reply-bridge.pid').exists()
    checks.append({'args': ['setup'], 'exit': result.returncode, 'no_kakao_refused': True})
    text = '## 한글 결과 이미지\nmacOS 공통 기능 검증입니다.\n**강조 문장**과 `literal code`를 표시합니다.\n\n| 항목 | 결과 |\n| --- | --- |\n| 한글 | 정상 표시 |\n| 페이지 | 크기 확인 |'
    paths = render.text_images(text, 'macos-core-smoke', heading='[TalkTurn macOS 공통 기능]')
    dimensions = []
    for path in paths:
        with Image.open(path) as image:
            assert image.width == 1080 and 0 < image.height <= 1500
            dimensions.append(image.size)
    assert paths
    report = {'system': platform.platform(), 'machine': platform.machine(), 'python': sys.version,
              'package': talkturn.__file__, 'font': str(font_path), 'cli_checks': checks,
              'images': [str(path) for path in paths], 'dimensions': dimensions,
              'native_kakao_tested': False, 'real_provider_tested': False}
    runtime.write_json('macos-smoke.json', report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
