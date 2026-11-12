"""解析引用卡片，只取本次提问和引用编号，不读取引用内路径、URL 或密钥。"""
import re
from xml.etree import ElementTree as ET

from .recall import message_id
from .text import WXID


class QuoteError(ValueError):
    pass


def parse_quote(content, room):
    if not isinstance(content, str) or not content.strip():
        return None
    if (len(content) >= 256 * 1024 or len(content.encode('utf-8')) >= 256 * 1024 or '\0' in content or
            re.search(r'<!\s*(DOCTYPE|ENTITY)', content, re.I)):
        raise QuoteError('引用消息超过限制或含实体声明')
    try:
        root = ET.fromstring(content)
    except (ET.ParseError, ValueError):
        raise QuoteError('引用消息 XML 无效') from None
    stack, count = [(root, 0)], 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > 20000 or depth > 32:
            raise QuoteError('引用消息结构超过限制')
        stack.extend((child, depth + 1) for child in node)
    if root.tag != 'msg':
        return None
    apps = root.findall('appmsg')
    if len(apps) != 1:
        return None
    app = apps[0]

    def scalar(node, name, maximum=4096):
        fields = node.findall(name)
        if len(fields) > 1 or (fields and len(fields[0])):
            raise QuoteError('引用消息字段重复或嵌套')
        result = fields[0].text or '' if fields else ''
        if len(result) > maximum:
            raise QuoteError('引用消息字段超过限制')
        return result

    if scalar(app, 'type', 10).strip() != '57':
        return None
    title = scalar(app, 'title')
    if any(ord(char) < 32 and char not in '\n\r\t' for char in title):
        raise QuoteError('引用提问含控制字符')
    references = app.findall('refermsg')
    quote = {'status': 'unavailable', 'type': '', 'message_id': '', 'room': room, 'sender_id': ''}
    if len(references) != 1:
        return title, quote
    reference = references[0]
    kind = scalar(reference, 'type', 10).strip()
    identifier = message_id(scalar(reference, 'svrid', 20).strip())
    origin = scalar(reference, 'fromusr', 127).strip()
    author = scalar(reference, 'chatusr', 127).strip()
    if not room.endswith('@chatroom') or not WXID.fullmatch(room):
        return title, quote
    # 群引用通常 fromusr=群，chatusr=原作者；部分客户端 fromusr=原作者。
    if origin.endswith('@chatroom') and origin != room:
        return title, quote
    if author.endswith('@chatroom') and author != room:
        return title, quote
    sender = author if author and not author.endswith('@chatroom') else origin
    if (not WXID.fullmatch(sender) or sender.endswith('@chatroom') or not identifier or
            (origin and not WXID.fullmatch(origin))):
        return title, quote
    quote.update(status='ready', type=kind, message_id=identifier, sender_id=sender)
    return title, quote
