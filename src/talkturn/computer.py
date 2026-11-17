"""TalkTurn's display nickname for this computer; never change the OS hostname."""
from .runtime import read_json, write_json, locked


def _validated(value):
    if not isinstance(value, str) or not value.isprintable() or not 1 <= len(value.strip()) <= 40:
        raise ValueError('컴퓨터 이름은 줄바꿈·제어문자 없이 1~40자로 입력하세요.')
    return value.strip()


def computer_name():
    try:
        return _validated(read_json('delivery.json', {}).get('computer_name', '내 PC'))
    except ValueError:
        return '내 PC'


def set_computer_name(value):
    name = _validated(value)
    with locked('settings.lock'):
        settings = read_json('delivery.json', {})
        settings['computer_name'] = name
        write_json('delivery.json', settings)
    return name


def computer_label():
    return '컴퓨터: ' + computer_name()
