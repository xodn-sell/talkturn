"""Result headings from cached session metadata, without provider calls."""
import unicodedata
from .runtime import read_json


def _title(value):
    if not isinstance(value, str):
        return ''
    value = ' '.join(''.join(
        ' ' if char.isspace() or unicodedata.category(char) in {'Cc', 'Cf', 'Cs'} else char
        for char in value
    ).split())
    value = value.translate(str.maketrans({'[':'［', ']':'］'}))
    return value if len(value) <= 100 else value[:99].rstrip() + '…'


def session_heading(origin):
    """Return one display line; never mutate the origin's routing identity."""
    if not isinstance(origin, dict) or origin.get('source') not in ('codex', 'claude'):
        return ''
    session_id = origin.get('session_id')
    if not isinstance(session_id, str) or not session_id.strip():
        return ''
    try:
        catalog = read_json('session-catalog.json', {})
    except (OSError, ValueError):
        catalog = {}
    rows = catalog.get('rows', []) if isinstance(catalog, dict) else []
    matches = [row for row in rows if isinstance(row, dict)
               and row.get('provider') == origin['source']
               and row.get('session_id') == session_id] if isinstance(rows, list) else []
    title = ''
    if origin['source'] == 'claude':
        # The cached list can predate the title or be rewritten by an older running dashboard.
        from .sessions import claude_title
        title = _title(claude_title(session_id))
    if not title and len(matches) == 1:
        title = _title(matches[0].get('name')) or _title(matches[0].get('title'))
    title = title or _title(origin.get('title')) or _title(origin.get('name'))
    title = title or '제목 없는 작업 · ' + _title(session_id)[:8]
    return '[' + title + ']'
