import json
from types import SimpleNamespace

import pytest
from telegram import MessageEntity, User

from socrat_bot.entities import command_body, normalize, units


def test_unicode_prefix_nested_and_semantic_clipping():
    prefix = '/mailing@mybot \n'
    text = '😀 bold\nlink'
    cut = units(prefix)
    raw = [
        MessageEntity('bold', 0, cut + units(text)),
        MessageEntity('italic', cut, 2),
        MessageEntity('text_link', cut + 8, 4, url='https://example.org/hidden'),
        MessageEntity('text_link', 0, cut + 2, url='https://example.org/wrong'),
    ]
    body, entities = command_body(SimpleNamespace(text=prefix + text, entities=raw))
    assert body == text
    assert entities == [
        {'type': 'bold', 'offset': 0, 'length': units(text)},
        {'type': 'italic', 'offset': 0, 'length': 2},
        {'type': 'text_link', 'offset': 8, 'length': 4, 'url': 'https://example.org/hidden'},
    ]
    assert command_body(SimpleNamespace(text='/mailing_edit 123 😀\nline', entities=[]), 2)[0] == '😀\nline'
    assert command_body(SimpleNamespace(text='/mailing_edit 😀\nline', entities=[]))[0] == '😀\nline'


def test_native_entity_json_roundtrip():
    text = '😀abcdef'
    raw = [MessageEntity(kind, 2, 3) for kind in ('bold', 'italic', 'underline', 'strikethrough', 'spoiler', 'code')]
    raw += [MessageEntity('pre', 2, 3, language='python'),
            MessageEntity('text_link', 2, 3, url='https://example.org/?a=1&b=2'),
            MessageEntity('text_mention', 2, 3, user=User(123, 'A 😀', False, last_name='B', username='someone')),
            MessageEntity('custom_emoji', 0, 2, custom_emoji_id='123')]
    encoded = normalize(text, raw)
    restored = [MessageEntity.de_json(item, None) for item in json.loads(json.dumps(encoded))]
    assert normalize(text, restored) == encoded
    assert encoded[-2]['user']['id'] == 123
    with pytest.raises(ValueError, match='boundary'):
        normalize(text, [{'type': 'bold', 'offset': 1, 'length': 2}])
    with pytest.raises(ValueError):
        normalize('😀' * 2049)
    assert normalize('😀' * 2048) == []
