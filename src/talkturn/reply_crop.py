"""Locally isolate one complete quoted reply before any model sees an image.

isolate_reply(message, image_path, cache=None) returns None on ambiguity, or:
  path: absolute PNG path containing only the selected reply bubble;
  body: the original clipboard body (never OCR-generated instructions);
  quote_kind: local hint, one of text/image/unknown;
  quote_thumbnail: an optional absolute PNG path inside the quoted block.
  body_path / quoted_path: 2x crops of the body and header+quote, respectively.

The caller must match quoted photos locally. Candidate/history images must not
be added to a model request. No viewport fallback is provided.
"""
import difflib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from PIL import Image, ImageChops, ImageDraw

from .runtime import ROOT, NO_WINDOW


def _normalize(text):
    return ''.join(text.split())


def _ocr(image_path):
    if sys.platform == 'darwin':
        from . import mac_kakao
        return mac_kakao.ocr(image_path)
    powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    with tempfile.TemporaryDirectory(prefix='talkturn-local-ocr-') as temporary:
        with Image.open(image_path) as source:
            scale = 2 if max(source.size) <= 5000 else 1
            local_path = Path(temporary) / 'ocr.png'
            with source.resize((source.width * scale, source.height * scale), Image.Resampling.LANCZOS) as enlarged:
                enlarged.save(local_path)
        result = subprocess.run(
            [str(powershell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
             '-File', str(Path(__file__).with_name('reply_ocr.ps1')), '-ImagePath', str(local_path.resolve())],
            # Recognition takes under a second; starting PowerShell took 12-15 s on a busy PC.
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            encoding='utf-8-sig', timeout=45, creationflags=NO_WINDOW,
        )
    if result.returncode:
        return None
    result = json.loads(result.stdout)
    for word in result.get('words', []):
        for key in ('x', 'y', 'width', 'height'):
            word[key] /= scale
    return result


def _one_hangul_difference(actual, expected):
    return (len(actual) == len(expected)
            and sum(a != b for a, b in zip(actual, expected)) == 1
            and all(a == b or '\uac00' <= a <= '\ud7a3' and '\uac00' <= b <= '\ud7a3'
                    for a, b in zip(actual, expected)))


def _errors(actual, expected):
    same = sum(block.size for block in difflib.SequenceMatcher(None, actual, expected, autojunk=False).get_matching_blocks())
    return max(len(actual), len(expected)) - same


def _slack(expected):
    # Windows OCR misreads a few glyphs of a long body (흠→홈, a lone ㅇ→0): allow one in ten.
    return len(expected) // 10 if len(expected) >= 20 else 0


def _body_match(actual, expected):
    # Selection only: the model transcription MUST still exactly match the clipboard.
    return (actual == expected or len(expected) >= 8 and _one_hangul_difference(actual, expected)
            or 0 < _slack(expected) >= _errors(actual, expected))


def _command_match(actual, expected):
    # A command bubble must read exactly as the clipboard text, except for glyphs Windows OCR cannot tell
    # apart in Kakao's font: l/I/1/|, rn/m and 톡/특 ("Talkturn help" is read "TaIkturnhelp", "talkturn"
    # "talktum", "톡턴" "특턴").
    actual, expected = (re.sub('[i1|!]', 'l', text.lower()).replace('rn', 'm').replace('특', '톡')
                        for text in (actual, expected))
    return actual == expected


def plainly_other(read, body):
    """Position needs no exact reading, but a body that plainly reads as another text
    (fewer than a quarter of the characters in common) is not this message."""
    read, body = _normalize(read), _normalize(body)
    longer = max(len(read), len(body))
    return longer >= 8 and (longer - _errors(read, body)) * 4 < longer


def _photo_label(text):
    # OCR can glue the thumbnail's edge to Kakao's label as a stray mark ("-사진").
    text = _normalize(text)
    return len(text) <= 4 and re.sub('[^가-힣]', '', text) == '사진'


def _reply_header(text):
    # A photo thumbnail beside the label is read as stray marks in front of it ("-나에게답장").
    text = re.sub(r'^[\W_]+', '', _normalize(text))
    # Small Kakao fonts can corrupt the known UI label. Keep recognizable tokens;
    # arbitrary short first lines are not reply headers.
    return text == '나에게답장' or (5 <= len(text) <= 8 and text[0] in ('나', '니')
                                 and text.endswith('장') and ('에' in text or '예' in text))


def _lines(ocr, width, height):
    if not isinstance(ocr, dict) or abs(ocr.get('angle') or 0) > 1:
        return None
    words = ocr.get('words')
    if not isinstance(words, list) or len(words) > 5000:
        return None
    groups = {}
    for word in words:
        if not isinstance(word, dict) or not isinstance(word.get('text'), str):
            return None
        try:
            x, y, w, h = (float(word[key]) for key in ('x', 'y', 'width', 'height'))
            line = int(word['line'])
        except (KeyError, ValueError, TypeError, OverflowError):
            return None
        if not (0 <= x < x + w <= width and 0 <= y < y + h <= height):
            return None
        groups.setdefault(line, []).append({'text': word['text'], 'box': (x, y, x + w, y + h)})
    lines = []
    for words in groups.values():
        words.sort(key=lambda word: word['box'][0])
        box = (min(word['box'][0] for word in words), min(word['box'][1] for word in words),
               max(word['box'][2] for word in words), max(word['box'][3] for word in words))
        lines.append({'text': ''.join(word['text'] for word in words), 'box': box, 'words': words})
    return sorted(lines, key=lambda line: (line['box'][1], line['box'][0]))


def _inside(inner, outer):
    return inner[0] >= outer[0] and inner[1] >= outer[1] and inner[2] <= outer[2] and inner[3] <= outer[3]


def _body_runs(lines, expected, match=_body_match):
    matches = []
    for start in range(len(lines)):
        text = ''
        for end in range(start, len(lines)):
            text += _normalize(lines[end]['text'])
            if match(text, expected):
                matches.append((_errors(text, expected), start, end))
            if len(text) >= len(expected) + _slack(expected):
                break
    # Overlapping runs read one bubble: keep its closest reading. Between bubbles only the
    # strictly closest wins, so two equally close copies of a body stay ambiguous.
    runs = []
    for match in sorted(matches):
        if all(match[2] < other[1] or match[1] > other[2] for other in runs):
            runs.append(match)
    return [lines[start:end + 1] for errors, start, end in runs if errors == runs[0][0]]


def _bubble(frame, anchor):
    x1, y1, x2, y2 = anchor
    # Uniform pixels beside the header belong to its bubble, not to OCR glyphs.
    seeds = [(int(x1) - 4, int((y1 + y2) / 2)), (int(x2) + 4, int((y1 + y2) / 2)),
             (int((x1 + x2) / 2), int(y1) - 4)]
    candidates = {}
    for x, y in seeds:
        if not (1 <= x < frame.width - 1 and 1 <= y < frame.height - 1):
            continue
        color = frame.getpixel((x, y))
        if any(max(abs(a - b) for a, b in zip(frame.getpixel((x + dx, y + dy)), color)) > 8
               for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1))):
            continue
        probe = frame.copy()
        try:
            ImageDraw.floodfill(probe, (x, y), tuple(255 - channel for channel in color), thresh=10)
            with ImageChops.difference(frame, probe) as difference:
                box = difference.getbbox()
        finally:
            probe.close()
        if (not box or box[0] < 3 or box[1] < 3 or box[2] > frame.width - 3
                or box[3] > frame.height - 3 or not _inside(anchor, box)):
            continue  # A bubble touching the viewport edge is incomplete.
        if box[2] - box[0] < 100 or y1 - box[1] > max(24, 2.5 * (y2 - y1)):
            continue
        candidates[box] = color
    return next(iter(candidates.items())) if len(candidates) == 1 else None


def _bands(frame, bubble, color, top, bottom, font_height):
    left, _, right, _ = bubble
    left += 8
    right -= 8
    minimum = (right - left) * 0.65
    rows = []
    for y in range(int(top), int(bottom)):
        run = longest = 0
        for x in range(left, right):
            pixel = frame.getpixel((x, y))
            run = run + 1 if max(abs(a - b) for a, b in zip(pixel, color)) >= 12 else 0
            longest = max(longest, run)
        if longest >= minimum:
            rows.append(y)
    bands = []
    for y in rows:
        if bands and y == bands[-1][-1] + 1:
            bands[-1].append(y)
        else:
            bands.append([y])
    return [band for band in bands if len(band) <= max(3, round(font_height * 0.2))]


def _divider(frame, bubble, color, header_bottom, body_top, font_height, lines):
    left, right = bubble[0] + 8, bubble[2] - 8
    bands = _bands(frame, bubble, color, header_bottom + 4, body_top - 2, font_height)
    if len(bands) == 1:
        return bands[0][0], bands[0][-1] + 1
    if bands:
        return None
    # Kakao also separates the gray quoted block from the body with a clear gap.
    quoted = [line for line in lines if _inside(line['box'], bubble)
              and header_bottom < line['box'][1] and line['box'][3] < body_top]
    if not quoted:
        return None
    quote_bottom = max(line['box'][3] for line in quoted)
    gap = body_top - quote_bottom
    if not max(6, font_height * 0.6) <= gap <= max(30, font_height * 3):
        return None
    middle = round((quote_bottom + body_top) / 2)
    dark = sum(max(abs(a - b) for a, b in zip(frame.getpixel((x, y)), color)) >= 45
               for y in range(middle - 1, middle + 2) for x in range(left, right))
    return (middle, middle + 1) if dark / (3 * (right - left)) < 0.04 else None


def _lowest_divider(frame, bubble, color, header_bottom, font_height):
    """Divider of the reply bubble under which the capture shows nothing but chat background."""
    edge = bubble[3] + 3
    if edge >= frame.height:
        return None
    # Kakao paints its scrollbar thumb right of the bubbles, so look only up to the bubble's right edge.
    background = frame.getpixel((3, frame.height - 3))
    with frame.crop((0, edge, bubble[2], frame.height)) as below, Image.new('RGB', (bubble[2], frame.height - edge), background) as blank:
        with ImageChops.difference(below, blank) as difference:
            if max(high for _, high in difference.getextrema()) > 12:
                return None  # Another bubble, photo or link card is newer than this one.
    # A link's underline in the body is a second band, so only the highest one divides quote and body.
    bands = _bands(frame, bubble, color, header_bottom + 4, bubble[3] - 4, font_height)
    return (bands[0][0], bands[0][-1] + 1) if bands else None


def _thumbnail(frame, quote_box, color, quote_words, labelled=True):
    # Only offer a thumbnail when the quote explicitly has Kakao's photo label.
    if labelled and not any(_photo_label(word['text']) for word in quote_words):
        return None
    with frame.crop(quote_box) as quote, Image.new('RGB', quote.size, color) as background:
        with ImageChops.difference(quote, background) as difference:
            mask = difference.convert('L').point(lambda value: 255 if value >= 20 else 0)
        try:
            draw = ImageDraw.Draw(mask)
            for word in quote_words:
                x1, y1, x2, y2 = word['box']
                draw.rectangle((x1 - quote_box[0] - 2, y1 - quote_box[1] - 2,
                                x2 - quote_box[0] + 2, y2 - quote_box[1] + 2), fill=0)
            # Bubble borders can leave a thin edge component that expands the box
            # across the whole quote. A complete inset photo never touches this crop's edge.
            for x in range(mask.width):
                for y in (0,mask.height-1):
                    if mask.getpixel((x,y)):ImageDraw.floodfill(mask,(x,y),0)
            for y in range(mask.height):
                for x in (0,mask.width-1):
                    if mask.getpixel((x,y)):ImageDraw.floodfill(mask,(x,y),0)
            box = mask.getbbox()
            if not box:
                return None
            width, height = box[2] - box[0], box[3] - box[1]
            if not (16 <= width <= 180 and 16 <= height <= 180 and 0.5 <= width / height <= 2):
                return None
            with mask.crop(box) as region:
                if region.histogram()[255] / (width * height) < 0.55:
                    return None
            return (quote_box[0] + box[0], quote_box[1] + box[1],
                    quote_box[0] + box[2], quote_box[1] + box[3])
        finally:
            mask.close()


def _write_crops(frame, bubble, thumbnail, divider, quoted_top, key):
    folder = ROOT / 'reply-crops'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'{key}-reply.png'
    with frame.crop(bubble) as cropped:
        cropped.save(path)
    quote_path = None
    if thumbnail:
        quote_path = folder / f'{key}-quote.png'
        with frame.crop(thumbnail) as cropped:
            cropped.save(quote_path)
    body_path = folder / f'{key}-body.png'
    quoted_path = folder / f'{key}-quoted.png'
    for destination, box in (
            (body_path, (bubble[0], divider[1], bubble[2], bubble[3])),
            (quoted_path, (bubble[0], quoted_top, bubble[2], divider[0]))):
        with frame.crop(box) as cropped:
            with cropped.resize((cropped.width * 2, cropped.height * 2), Image.Resampling.LANCZOS) as enlarged:
                enlarged.save(destination)
    owned = [item for item in folder.iterdir()
             if item.is_file() and re.fullmatch(r'[0-9a-f]{32}-(reply|quote|body|quoted)\.png', item.name)]
    current = {path, quote_path, body_path, quoted_path} - {None}
    older = sorted((item for item in owned if item not in current),
                   key=lambda item: item.stat().st_mtime, reverse=True)
    for stale in older[16 - len(current):]:
        stale.unlink(missing_ok=True)
    return {'path': str(path.resolve()), 'quote_thumbnail': str(quote_path.resolve()) if quote_path else None,
            'body_path': str(body_path.resolve()), 'quoted_path': str(quoted_path.resolve())}


def _query_with_antialiased_edge(body, frame, bubble, color, outside):
    """Accept the thin blended outline of an isolated receipt/original query."""
    from .commands import parse
    command=parse(body,remote=True)
    if command not in (['receipt'],['original']) and not (
            command and len(command)==2 and command[0]=='original' and re.fullmatch(r'T[0-9a-f]{8,64}',command[1],re.I)):
        return False
    left,top,right,_=bubble
    if left<6 or top<6 or right+5>=frame.width:
        return False
    background=frame.getpixel((3,frame.height-3))
    # In scaled Kakao captures the two outer pixels blend the bubble into the wallpaper.
    # Require continuous wallpaper directly above, and on both sides past that outline.
    checks=[(x,y) for y in range(top-5,top-2) for x in range(left+6,right-6)]
    checks.extend((x,top+3) for x in (left-5,left-4,left-3,right+2,right+3,right+4))
    if any(max(abs(a-b) for a,b in zip(frame.getpixel(point),background))>4 for point in checks):
        return False
    channel=max(range(3),key=lambda i:abs(color[i]-background[i]))
    contrast=color[channel]-background[channel]
    if abs(contrast)<32:
        return False
    for pixel in outside:
        alpha=(pixel[channel]-background[channel])/contrast
        if not 0<=alpha<=0.25 or any(abs(value-(base+alpha*(ink-base)))>4 for value,base,ink in zip(pixel,background,color)):
            return False
    return True


def message_kind(message,image_path,cache=None):
    """Prove one quoted or ordinary bubble locally; uncertainty never authorizes a control."""
    cache=cache if cache is not None else {}
    cache.pop('plain_stop',None)
    if isolate_reply(message,image_path,cache):
        return 'reply'
    body=message.get('text') if isinstance(message,dict) else None
    if not isinstance(body,str) or not _normalize(body):
        return None
    try:
        raw=Path(image_path).read_bytes()
        digest=hashlib.sha256(raw).hexdigest()
        ocr=cache.get('reply_crop_ocr',{}).get(digest)
        with Image.open(io.BytesIO(raw)) as source:
            if max(source.size)>10000 or source.width*source.height>20_000_000:
                return None
            with source.convert('RGB') as frame:
                lines=_lines(ocr,frame.width,frame.height)
                if not lines:return None
                runs=_body_runs(lines,_normalize(body),_command_match)
                if len(runs)!=1:
                    cache['plain_stop']='command_read_twice' if runs else 'command_not_read'
                    return None
                found=_bubble(frame,runs[0][0]['box'])
                if not found:
                    cache['plain_stop']='no_complete_command_bubble'
                    return None
                bubble,color=found
                outside=[frame.getpixel(point) for point in (
                    (bubble[0]-2,bubble[1]+3),(bubble[2]+1,bubble[1]+3),
                    ((bubble[0]+bubble[2])//2,bubble[1]-2))]
                if any(max(abs(a-b) for a,b in zip(outside[0],sample))>8 for sample in outside[1:]):
                    if not _query_with_antialiased_edge(body,frame,bubble,color,outside):
                        cache['plain_stop']='command_border_unproven'
                        return None  # A differently colored quoted block is not a separate plain bubble.
                visible=[line for line in lines if _inside(line['box'],bubble)]
                if (any(_reply_header(line['text']) for line in visible)
                        or not _command_match(_normalize(''.join(line['text'] for line in visible)),_normalize(body))):
                    return None
                top=runs[0][0]['box'][1];bottom=runs[0][-1]['box'][3]
                padding=max(24,1.5*(runs[0][0]['box'][3]-top))
                if top-bubble[1]>padding or bubble[3]-bottom>padding:
                    return None
                # OCR can omit a quote header or thumbnail. Any visible content above
                # the matched body prevents treating its bubble as an ordinary command.
                band=(bubble[0]+8,bubble[1]+5,bubble[2]-8,int(top)-2)
                if band[2]<=band[0] or band[3]<=band[1]:return None
                with frame.crop(band) as region,Image.new('RGB',(band[2]-band[0],band[3]-band[1]),color) as blank:
                    with ImageChops.difference(region,blank).convert('L') as difference:
                        if sum(difference.histogram()[20:])/(region.width*region.height)>.01:
                            return None
        return 'plain' if hashlib.sha256(Path(image_path).read_bytes()).hexdigest()==digest else None
    except (OSError,ValueError,TypeError,KeyError,subprocess.SubprocessError):
        return None


def reply_header_visible(image_path, cache):
    """True when the OCR already cached for this capture shows a quoted-reply header."""
    try:
        raw = Path(image_path).read_bytes()
        ocr = cache.get('reply_crop_ocr', {}).get(hashlib.sha256(raw).hexdigest())
        with Image.open(io.BytesIO(raw)) as frame:
            lines = _lines(ocr, frame.width, frame.height)
        return any(_reply_header(line['text']) for line in lines or [])
    except (OSError, ValueError, TypeError, KeyError):
        return False


def isolate_reply(message, image_path, cache=None, last=False, *, strict=False):
    """Return a verified single-bubble crop, or None; never return the viewport.

    last=True selects the bubble by chat order instead of by reading its body: a capture scrolled to
    the latest message shows the chat's newest message as its lowest bubble. Only the caller can
    prove "newest" (same message list before and after the capture), so it must do so before
    showing the crop to a model. The model still has to confirm the clipboard body.
    strict=True requires the command's exact local reading; fuzzy reply selection cannot authorize controls.
    """
    cache = cache if cache is not None else {}

    def stop(reason):
        # Left in cache['isolate_stop'] for the caller's log: a bare None hid which check failed.
        cache['isolate_stop'] = reason
        return None
    cache.pop('isolate_stop', None)
    body = message.get('text') if isinstance(message, dict) else None
    if not isinstance(body, str) or not _normalize(body):
        return stop('empty_body')
    body_match = _command_match if strict else _body_match
    try:
        raw = Path(image_path).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        with Image.open(io.BytesIO(raw)) as source:
            if max(source.size) > 10000 or source.width * source.height > 20_000_000:
                return stop('capture_too_large')
            frame = source.convert('RGB')
        try:
            ocr_cache = cache.setdefault('reply_crop_ocr', {})
            if digest not in ocr_cache:
                try:
                    ocr_cache[digest] = _ocr(image_path)
                except subprocess.SubprocessError:
                    return stop('ocr_unavailable')  # Timed out; not cached, so a later call may read it.
                while len(ocr_cache) > 8:
                    del ocr_cache[next(iter(ocr_cache))]
            if ocr_cache[digest] is None:
                return stop('ocr_unavailable')
            lines = _lines(ocr_cache[digest], frame.width, frame.height)
            if not lines:
                return stop('ocr_gave_no_lines')
            runs = [] if last else _body_runs(lines, _normalize(body), body_match)
            if len(runs) != 1 and not last:
                return stop('body_read_twice' if runs else 'body_not_read')
            body_lines = runs[0] if runs else []
            anchors = [line for line in lines if _reply_header(line['text'])]
            candidates = []
            rejected = []  # Why each reply header's bubble was not this message.
            for anchor in anchors:
                found = _bubble(frame, anchor['box'])
                if not found:
                    rejected.append('no_bubble_around_header')
                    continue
                bubble, color = found
                if not all(_inside(line['box'], bubble) for line in body_lines):
                    rejected.append('body_in_another_bubble')
                    continue
                if sum(_inside(line['box'], bubble) for line in anchors) != 1:
                    rejected.append('two_headers_in_bubble')
                    continue
                if last:
                    divider = _lowest_divider(frame, bubble, color, anchor['box'][3], anchor['box'][3] - anchor['box'][1])
                    if not divider:
                        rejected.append('not_lowest_or_no_divider')
                        continue
                    with frame.crop((bubble[0] + 8, divider[1] + 2, bubble[2] - 8, bubble[3] - 4)) as region:
                        if sum(max(abs(a - b) for a, b in zip(pixel, color)) >= 45 for pixel in region.getdata()) < 8:
                            rejected.append('nothing_under_divider')
                            continue
                    if plainly_other(''.join(line['text'] for line in lines
                                             if _inside(line['box'], bubble) and line['box'][1] >= divider[1]), body):
                        rejected.append('another_text_under_divider')
                        continue
                else:
                    # The run's first line is only a character-count guess: when OCR drops body characters
                    # (a short "=1" line of a wrapped URL), a quote line such as the "사진" label stands in
                    # for them. The divider, not the count, decides where the body starts.
                    for first in body_lines:
                        divider = _divider(frame, bubble, color, anchor['box'][3], first['box'][1],
                                           anchor['box'][3] - anchor['box'][1], lines)
                        if divider and body_match(_normalize(''.join(
                                line['text'] for line in lines
                                if _inside(line['box'], bubble) and line['box'][1] >= divider[1])), _normalize(body)):
                            break
                    else:
                        rejected.append('no_divider_above_body')
                        continue
                quote_words = [word for line in lines for word in line['words']
                               if _inside(word['box'], bubble) and word['box'][1] > anchor['box'][3]
                               and word['box'][3] < divider[0]]
                quote_box = (bubble[0] + 4, bubble[1] + 4, bubble[2] - 4, divider[0] - 1)
                header_words = [word for line in lines for word in line['words']
                                if _inside(word['box'], bubble) and word['box'][3] < divider[0]]
                # A photo quote's only text is Kakao's pale "사진" label, and OCR drops it beside some
                # thumbnails (a white page of small print: 10 of 112 sent results). The picture left of
                # the header is the same evidence; the photo match still has to name one session.
                thumbnail = (_thumbnail(frame, quote_box, color, header_words) if quote_words else
                             _thumbnail(frame, (quote_box[0], quote_box[1], int(anchor['box'][0]) - 2, quote_box[3]),
                                        color, header_words, labelled=False))
                if not (quote_words or thumbnail) or divider[0] - anchor['box'][3] < 12:
                    rejected.append('quote_block_unreadable')
                    continue
                has_photo_label = any(_photo_label(word['text']) for word in quote_words)
                kind = 'image' if thumbnail else 'unknown' if has_photo_label else 'text'
                quoted_top = int(anchor['box'][3]) + 2 if kind == 'text' else bubble[1]
                candidates.append((bubble, thumbnail, divider, quoted_top, kind,
                                   ''.join(word['text'] for word in quote_words)))
            if len(candidates) != 1:
                return stop('several_reply_bubbles' if candidates else
                            '+'.join(dict.fromkeys(rejected)) or 'no_reply_header')
            # The OCR and pixels must describe the same unchanged capture.
            if hashlib.sha256(Path(image_path).read_bytes()).hexdigest() != digest:
                return stop('capture_changed')
            bubble, thumbnail, divider, quoted_top, kind, quote_text = candidates[0]
            key = hashlib.sha256((digest + '\0' + body).encode('utf-8')).hexdigest()[:32]
            paths = _write_crops(frame, bubble, thumbnail, divider, quoted_top, key)
            return {**paths, 'body': body, 'quote_kind': kind, 'quote_text': quote_text}
        finally:
            frame.close()
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        return stop('error:' + type(error).__name__)
