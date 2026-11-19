"""Readable, bounded result pages from source text. No model or remote resources."""
from functools import lru_cache
import hashlib
import os
from pathlib import Path
import re
import unicodedata
from PIL import Image, ImageDraw, ImageFont
from .runtime import ROOT

WIDTH, MARGIN, MAX_HEIGHT = 1080, 64, 1500


def _clusters(text):
    """Keep combining marks and joined emoji together at a line boundary."""
    parts = []
    for char in text:
        if parts and (unicodedata.category(char).startswith('M') or char == '\u200d'
                      or parts[-1].endswith('\u200d') or '\U0001f3fb' <= char <= '\U0001f3ff'):
            parts[-1] += char
        else:
            parts.append(char)
    return parts


def _blocks(text):
    """Style a small Markdown subset; unsupported syntax stays visible verbatim."""
    lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    result, index = [], 0
    while index < len(lines):
        line = lines[index]
        fence = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line)
        if fence:
            end = index + 1
            while end < len(lines) and not re.fullmatch(r' {0,3}' + re.escape(fence[1][0]) +
                                                        '{' + str(len(fence[1])) + r',}\s*', lines[end]):
                end += 1
            if end < len(lines):
                result.append(('code', (fence[2].strip(), lines[index + 1:end])))
                index = end + 1
                continue
        if index + 1 < len(lines) and '|' in line and re.fullmatch(
                r'\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*', lines[index + 1]):
            def cells(value):
                value = value.strip().removeprefix('|').removesuffix('|')
                return [cell.strip() for cell in value.split('|')]
            rows, end = [cells(line)], index + 2
            while end < len(lines) and '|' in lines[end] and lines[end].strip():
                rows.append(cells(lines[end]))
                end += 1
            if (len(cells(lines[index + 1])) == len(rows[0])
                    and all(len(row) == len(rows[0]) for row in rows)
                    and not any('\\|' in x or '`' in x for x in lines[index:end])):
                result.append(('table', rows))
                index = end
                continue
        match = re.match(r'^ {0,3}(#{1,6})\s+(.+)$', line)
        if match:
            result.append(('heading', match[2]))
        elif not line.strip():
            if result and result[-1][0] != 'space':
                result.append(('space', ''))
        else:
            result.append(('text', line.expandtabs(4)))
        index += 1
    while result and result[-1][0] == 'space':
        result.pop()
    return result


def column_widths(natural, total):
    """Short columns keep their own width; long ones share the rest and wrap."""
    if sum(natural) <= total:
        widths = list(natural)
        widths[natural.index(max(natural))] += total - sum(natural)
        return widths
    widths, open_columns = {}, set(range(len(natural)))
    while open_columns:
        share = (total - sum(widths.values())) / len(open_columns)
        small = {column for column in open_columns if natural[column] <= share}
        if not small:
            rest, wanted = total - sum(widths.values()), sum(natural[column] for column in open_columns)
            widths.update({column: natural[column] * rest / wanted for column in open_columns})
            break
        widths.update({column: natural[column] for column in small})
        open_columns -= small
    return [widths[column] for column in range(len(natural))]


def text_images(text, key, *, heading=''):
    font_dir = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'
    font_path = Path(os.environ.get('TALKTURN_FONT') or os.environ.get('TOKTURN_FONT') or font_dir / 'malgun.ttf')
    if not font_path.is_file():
        raise RuntimeError('한국어 글꼴 없음. TALKTURN_FONT 설정 필요.')
    body = ImageFont.truetype(str(font_path), 42)
    bold_path = font_path.with_name(font_path.stem + 'bd' + font_path.suffix)
    bold_path = bold_path if bold_path.is_file() else font_path
    mono_path = font_dir / 'consola.ttf'
    fonts = {'body': body, 'bold': ImageFont.truetype(str(bold_path), 42),
             'heading': ImageFont.truetype(str(bold_path), 48),
             'title': ImageFont.truetype(str(bold_path), 38),
             'small': ImageFont.truetype(str(font_path), 26),
             'code': ImageFont.truetype(str(mono_path if mono_path.is_file() else font_path), 36),
             'cell': ImageFont.truetype(str(font_path), 34),
             'cellbold': ImageFont.truetype(str(bold_path), 34)}
    probe = ImageDraw.Draw(Image.new('RGB', (1, 1)))

    @lru_cache(maxsize=24)
    def fallback_font(path, size):
        return ImageFont.truetype(path, size)

    @lru_cache(maxsize=2048)
    def fallback(style, cluster):
        font = fonts[style]
        visible = [char for char in cluster if not char.isspace() and unicodedata.category(char) not in ('Cf', 'Mn')]
        if any(bytes(font.getmask(char)) == bytes(font.getmask('\u0378')) for char in visible):
            for path in (font_dir / 'seguiemj.ttf', font_dir / 'seguisym.ttf', font_path):
                if not path.is_file():
                    continue
                candidate = fallback_font(str(path), font.size)
                if all(bytes(candidate.getmask(char)) != bytes(candidate.getmask('\u0378')) for char in visible):
                    return candidate
        return font

    def spans(value, style, inline=True):
        # Links remain literal so an image never silently loses the destination URL.
        inline = inline and not any(token in value for token in ('\\*', '\\`', '``', '://', ']('))
        chunks = re.split(r'(\*\*[^*\s](?:[^*]*[^*\s])?\*\*|`[^`]+`)', value) if inline else [value]
        output = []
        for chunk in chunks:
            selected = style
            if inline and re.fullmatch(r'\*\*[^*\s](?:[^*]*[^*\s])?\*\*', chunk):
                chunk, selected = chunk[2:-2], 'bold' if style == 'body' else style
            elif inline and chunk.startswith('`') and chunk.endswith('`'):
                chunk, selected = chunk[1:-1], 'code'
            for cluster in _clusters(chunk):
                output.append((cluster, fallback(selected, cluster), selected))
        return output

    def wrap(value, style, width, inline=True):
        lines, current, size, last_space = [], [], 0, 0
        for unit in spans(value, style, inline):
            advance = probe.textlength(unit[0], font=unit[1])
            while current and size + advance > width:
                split = last_space if last_space and style != 'code' else len(current)
                lines.append(current[:split])
                current = current[split:]
                size = sum(probe.textlength(part[0], font=part[1]) for part in current)
                last_space = 0
            current.append(unit)
            size += advance
            if unit[0].isspace():
                last_space = len(current)
        lines.append(current)
        return lines

    content_width = WIDTH - 2 * MARGIN
    title_lines = heading.splitlines()
    title = wrap(title_lines[0], 'title', content_width, False) if title_lines else []
    subtitle = wrap(' · '.join(title_lines[1:]), 'small', content_width, False) if len(title_lines) > 1 else []
    header_height = 112 + len(title) * 54 + len(subtitle) * 40
    available = MAX_HEIGHT - header_height - 94
    if available < 160:
        raise ValueError('이미지 제목이 너무 깁니다. 세션 제목을 줄여 주세요.')
    blocks = []
    for kind, value in _blocks(text):
        if kind == 'space':
            blocks.append([{'kind': 'space', 'height': 20}])
        elif kind == 'code':
            language, source_lines = value
            rows = [{'kind': 'code_label', 'runs': line, 'height': 48}
                    for line in wrap(language or 'code', 'small', content_width - 40, False)]
            for source in source_lines:
                rows.extend({'kind': 'code', 'runs': line, 'height': 50}
                            for line in wrap(source.expandtabs(4), 'code', content_width - 40, False))
            rows.append({'kind': 'code', 'runs': [], 'height': 16})
            blocks.append(rows)
        elif kind == 'table':
            columns = len(value[0])
            if columns > 4:
                # Wide tables become labelled records instead of unreadable narrow columns.
                for number, source in enumerate(value[1:], 1):
                    rows = [{'kind': 'heading', 'runs': spans(str(number), 'small'), 'height': 46}]
                    for label, cell in zip(value[0], source):
                        rows.extend({'kind': 'text', 'runs': line, 'height': 62}
                                    for line in wrap(label + ': ' + cell, 'body', content_width))
                    blocks.append(rows)
                if len(value) == 1:
                    blocks.append([{'kind': 'text', 'runs': line, 'height': 62}
                                   for line in wrap(' | '.join(value[0]), 'body', content_width)])
            else:
                natural = [max(sum(probe.textlength(unit[0], font=unit[1])
                                   for unit in spans(source[column], 'cellbold' if number == 0 else 'cell')) + 28
                               for number, source in enumerate(value)) for column in range(columns)]
                widths = column_widths(natural, content_width)
                table_rows = []
                for number, source in enumerate(value):
                    cells = [wrap(cell, 'cellbold' if number == 0 else 'cell', width - 28)
                             for cell, width in zip(source, widths)]
                    chunk = min(8, max(1, int((available - 24) // 48)))
                    for offset in range(0, max(map(len, cells)), chunk):
                        part = [cell[offset:offset + chunk] for cell in cells]
                        table_rows.append({'kind': 'table', 'cells': part, 'widths': widths, 'header': number == 0,
                                           'height': max(map(len, part)) * 48 + 24})
                for row in table_rows[1:]:
                    if not row['header']:
                        row['table_header'] = table_rows[0]
                blocks.append(table_rows)
        else:
            style = 'heading' if kind == 'heading' else 'body'
            blocks.append([{'kind': kind, 'runs': line, 'height': 70 if kind == 'heading' else 62}
                           for line in wrap(value, style, content_width)])

    pages, page, used = [], [], 0
    for index, block in enumerate(blocks):
        height = sum(row['height'] for row in block)
        next_height = 0
        if block[0]['kind'] == 'heading':
            for following in blocks[index + 1:]:
                following_height = sum(row['height'] for row in following)
                if following[0]['kind'] == 'space':
                    next_height += following_height
                    continue
                next_height += (following_height if height + next_height + following_height <= available
                                else sum(row['height'] for row in following[:2]))
                break
        if page and height + next_height <= available and used + height + next_height > available:
            pages.append(page)
            page, used = [], 0
        # Large blocks may span pages, but their label/header must accompany real content.
        if block[0]['kind'] == 'code_label' or block[0].get('header'):
            lead = 0
            for row in block:
                lead += row['height']
                if row['kind'] != 'code_label' and not row.get('header'):
                    break
            if page and lead <= available and used + lead > available:
                pages.append(page)
                page, used = [], 0
        for row in block:
            if page and used + row['height'] > available:
                pages.append(page)
                page, used = [], 0
            if not page and row.get('table_header') and row['table_header']['height'] + row['height'] <= available:
                page.append(row['table_header'])
                used += row['table_header']['height']
            if not page and row['kind'] == 'space':
                continue
            page.append(row)
            used += row['height']
    if page or not pages:
        pages.append(page)

    def draw_runs(draw, xy, runs, fill='#202820'):
        x, y = xy
        for cluster, font, style in runs:
            draw.text((x, y), cluster, font=font, fill=fill, embedded_color=True)
            x += probe.textlength(cluster, font=font)

    folder = ROOT / 'rendered'
    folder.mkdir(parents=True, exist_ok=True)
    prefix = hashlib.sha256(key.encode()).hexdigest()[:20]
    files = []
    for number, page in enumerate(pages, 1):
        while page and page[-1]['kind'] == 'space':
            page.pop()
        height = max(340, header_height + sum(row['height'] for row in page) + 94)
        canvas = Image.new('RGB', (WIDTH, height), '#ffffff')
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle((MARGIN, 32, MARGIN + 126, 68), radius=10, fill='#fee500')
        draw.text((MARGIN + 12, 31), 'talkturn', font=fonts['small'], fill='#202820')
        counter = f'{number} / {len(pages)}'
        draw.text((WIDTH - MARGIN - probe.textlength(counter, font=fonts['small']), 34),
                  counter, font=fonts['small'], fill='#657062')
        y = 88
        for line in title:
            draw_runs(draw, (MARGIN, y), line)
            y += 54
        for line in subtitle:
            draw_runs(draw, (MARGIN, y), line, '#657062')
            y += 40
        draw.line((MARGIN, header_height - 12, WIDTH - MARGIN, header_height - 12), fill='#e1e6de', width=2)
        y = header_height
        for row in page:
            kind = row['kind']
            if kind.startswith('code'):
                draw.rectangle((MARGIN, y, WIDTH - MARGIN, y + row['height']), fill='#f2f5f0')
                draw_runs(draw, (MARGIN + 20, y + 2), row['runs'], '#53634a' if kind == 'code_label' else '#203226')
            elif kind == 'table':
                left = MARGIN
                for cell, cell_width in zip(row['cells'], row['widths']):
                    draw.rectangle((left, y, left + cell_width, y + row['height']),
                                   fill='#edf2e7' if row['header'] else '#fafbf8', outline='#d9e1d3', width=1)
                    for offset, line in enumerate(cell):
                        draw_runs(draw, (left + 14, y + 8 + offset * 48), line)
                    left += cell_width
            elif kind != 'space':
                draw_runs(draw, (MARGIN, y + 2), row['runs'])
            y += row['height']
        draw.line((MARGIN, height - 68, WIDTH - MARGIN, height - 68), fill='#e1e6de', width=1)
        draw.text((MARGIN, height - 49), '다음 장에 계속' if number < len(pages) else '여기까지가 결과입니다',
                  font=fonts['small'], fill='#657062')
        path = folder / f'{prefix}-{number}.png'
        canvas.save(path)
        canvas.close()
        files.append(str(path))
    return files
