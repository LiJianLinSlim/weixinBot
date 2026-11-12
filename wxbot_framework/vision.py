"""短期图片关联与受限模型输入；不从聊天文本读取路径或下载网址。"""
from __future__ import annotations

import base64
from collections import OrderedDict
from io import BytesIO
import math
from pathlib import Path
import stat
import threading
import time
import warnings

from .images import MAX_IMAGE_BYTES
from .llm import LLMError
from .recall import message_id


class RecentImages:
    """近图五分钟；精确引用索引最多 1000 条、24 小时，均隔离当前连接和群。"""
    def __init__(self, directory, *, clock=time.monotonic):
        self.directory = Path(directory).resolve()
        self.clock = clock
        self._condition = threading.Condition()
        self._rows = OrderedDict()
        self._references = OrderedDict()

    def observe(self, message):
        if (message.type != 3 or not message.is_group or message.is_self or
                not message.sender_id or not message.session_id or message.data.get('bot_generated')):
            return
        stamp = message.data.get('image_received_at', message.received_at)
        if type(stamp) not in (int, float) or not math.isfinite(stamp):
            return
        status = message.data.get('image_status')
        if status not in ('pending', 'ready', 'failed'):
            return
        identifier = message.sequence or message.data.get('native_message_id')
        if not identifier:
            return
        key = (message.session_id, message.chat_id, message.sender_id)
        with self._condition:
            self._prune()
            rows = self._rows.setdefault(key, [])
            old = next((row for row in rows if row['id'] == identifier), None)
            if old is not None:
                stamp = old['time']
                rows.remove(old)
            entry = dict(id=identifier, time=stamp, status=status, path=message.image_path,
                         sender_id=message.sender_id)
            rows.append(entry)
            rows.sort(key=lambda row: row['time'])
            del rows[:-4]
            self._rows.move_to_end(key)
            while len(self._rows) > 128:
                self._rows.popitem(last=False)
            server_id = message_id(message.data.get('native_message_id'))
            if server_id:
                reference_key = (message.session_id, message.chat_id, server_id)
                self._references[reference_key] = entry
                self._references.move_to_end(reference_key)
                while len(self._references) > 1000:
                    self._references.popitem(last=False)
            self._condition.notify_all()

    def _prune(self):
        cutoff = self.clock() - 300
        for key in list(self._rows):
            rows = [row for row in self._rows[key] if row['time'] >= cutoff]
            if rows:
                self._rows[key] = rows
            else:
                del self._rows[key]
        for key in list(self._references):
            if self._references[key]['time'] < self.clock() - 86400:
                del self._references[key]

    def for_message(self, message, *, wait_seconds=8):
        if 'quoted_message' in message.data:
            return self._for_quote(message, wait_seconds=wait_seconds)
        key = (message.session_id, message.chat_id, message.sender_id)
        deadline = time.monotonic() + wait_seconds
        with self._condition:
            while True:
                self._prune()
                rows = [row for row in self._rows.get(key, ())
                        if 0 <= message.received_at - row['time'] <= 300]
                if not rows:
                    return ()
                row = rows[-1]
                if row['status'] == 'ready':
                    path = row['path']
                    break
                if row['status'] == 'failed':
                    raise LLMError('图片下载失败，请重新发送图片后再艾特')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LLMError('图片仍在下载，请稍后再艾特')
                self._condition.wait(remaining)
        return (image_data_url(path, self.directory),)

    def _for_quote(self, message, *, wait_seconds):
        quote = message.data['quoted_message']
        if not isinstance(quote, dict) or quote.get('status') != 'ready':
            raise LLMError('引用图片编号不完整，请重新发送原图再提问')
        if quote.get('type') != '3':
            raise LLMError('引用图片功能仅接受普通图片，请引用一张图片再提问')
        identifier = message_id(quote.get('message_id'))
        if not identifier or quote.get('room') != message.chat_id:
            raise LLMError('引用图片来源不匹配，请重新发送原图再提问')
        key = (message.session_id, message.chat_id, identifier)
        deadline = time.monotonic() + wait_seconds
        with self._condition:
            while True:
                self._prune()
                row = self._references.get(key)
                if (row is None or row['sender_id'] != quote.get('sender_id') or
                        row['time'] > message.received_at):
                    raise LLMError('引用图片未缓存，请重新发送原图再提问')
                if row['status'] == 'ready':
                    path = row['path']
                    break
                if row['status'] == 'failed':
                    raise LLMError('引用图片下载失败，请重新发送原图再提问')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LLMError('图片仍在下载，请稍后再次引用提问')
                self._condition.wait(remaining)
        return (image_data_url(path, self.directory),)


def image_data_url(path, directory):
    """只读当前账号接收目录的普通文件，重编码以移除元数据并限制上传量。"""
    try:
        target, root = Path(path), Path(directory).resolve(strict=True)
        resolved = target.resolve(strict=True)
        if target.is_symlink() or resolved.parent != root or resolved != target.absolute():
            raise ValueError
        info = resolved.stat()
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_IMAGE_BYTES:
            raise ValueError
        with resolved.open('rb') as source:
            raw = source.read(MAX_IMAGE_BYTES + 1)
        if not 0 < len(raw) <= MAX_IMAGE_BYTES:
            raise ValueError
        from PIL import Image, ImageOps
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as original:
                if original.format not in ('JPEG', 'PNG', 'GIF', 'WEBP', 'BMP'):
                    raise ValueError
                if original.width * original.height > 32_000_000:
                    raise ValueError
                original.seek(0)
                image = ImageOps.exif_transpose(original).convert('RGB')
                image.thumbnail((2048, 2048))
                for edge in (2048, 1536, 1024):
                    image.thumbnail((edge, edge))
                    output = BytesIO()
                    image.save(output, format='JPEG', quality=85)
                    body = output.getvalue()
                    if len(body) <= 2 * 1024 * 1024:
                        return 'data:image/jpeg;base64,' + base64.b64encode(body).decode('ascii')
                raise ValueError
    except Exception:
        raise LLMError('图片无法读取或尺寸过大，请重新发送一张普通图片') from None
