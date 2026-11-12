"""导出显式绑定账号的本地表情缓存，不枚举其他账号或访问网络。

business/emoticon 是本机已有文件集合，并不等同于完整、权威的收藏列表。
AES 密钥仅在内存中使用。WXGF 中的 HEVC 没有已验证的播放时间信息，
所以生成的 GIF 使用 30 fps 预览时间；不会丢帧来满足发送大小限制。
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
from typing import Callable
import warnings

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import GifImagePlugin, Image

from .account import validate_account

MAX_EMOTICON_BYTES = 1024 * 1024
MAX_EMOTICON_DIMENSION = 1024
MAX_CACHE_FILE_BYTES = 20 * 1024 * 1024
MAX_CACHE_TOTAL_BYTES = 100 * 1024 * 1024
MAX_CACHE_FILES = 256
MAX_CACHE_DIRECTORIES = 514  # 两个 category 以及各自最多 256 个十六进制分片。
MAX_FRAMES = 300
MAX_DECODED_PIXELS = 64 * 1024 * 1024
_CACHE_NAME = re.compile(r"([0-9a-fA-F]{32})(\.thumb)?\Z")
_FRAME_NAME = re.compile(r"frame-[0-9]{4}\.png\Z")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


@dataclass(frozen=True, slots=True)
class ExportedEmoticon:
    """路径只指向本次成功写入的文件；identifier 不是内容哈希验证结果。"""

    identifier: str
    original_path: Path | None = None
    thumbnail_path: Path | None = None
    gif_path: Path | None = None
    format: str = ""
    width: int = 0
    height: int = 0
    frame_count: int = 0
    sendable: bool = False
    error: str = ""


class EmoticonSessionExpired(RuntimeError):
    """当前账号/连接已经改变，本次导出必须立即停止。"""


class _ExportError(ValueError):
    pass


def _current(is_current: Callable[[], bool] | None) -> None:
    if is_current is not None:
        try:
            ready = bool(is_current())
        except Exception:
            ready = False
        if not ready:
            raise EmoticonSessionExpired("emoticon_session_expired") from None


def _absolute_path(value) -> Path:
    try:
        text = os.fspath(value)
    except TypeError:
        raise ValueError("表情路径必须是本地文件路径") from None
    if not isinstance(text, str) or not text.strip() or "\0" in text:
        raise ValueError("表情路径必须是非空本地文件路径")
    path = Path(text)
    if ".." in path.parts:
        raise ValueError("表情路径不能包含上级目录跳转")
    return Path(os.path.abspath(path))


def _no_links(path: Path, *, directory: bool | None = None) -> os.stat_result:
    """逐级 lstat，避免 resolve 先跟随符号链接或 Windows junction。"""
    information = None
    for component in (*reversed(path.parents), path):
        information = component.lstat()
        if stat.S_ISLNK(information.st_mode) or getattr(information, "st_file_attributes", 0) & _REPARSE_POINT:
            raise ValueError("表情路径不能包含链接或重解析点")
        if component != path and not stat.S_ISDIR(information.st_mode):
            raise ValueError("表情路径的父目录无效")
    if directory is True and not stat.S_ISDIR(information.st_mode):
        raise ValueError("表情目录必须是普通目录")
    if directory is False and not stat.S_ISREG(information.st_mode):
        raise ValueError("表情路径必须指向普通文件")
    if directory is False and information.st_nlink != 1:
        raise ValueError("表情文件不能是硬链接")
    return information


def _make_directory(path: Path) -> Path:
    for component in (*reversed(path.parents), path):
        try:
            _no_links(component, directory=True)
        except FileNotFoundError:
            try:
                component.mkdir()
            except FileExistsError:  # 同时导出时另一个 worker 可能已创建。
                pass
        _no_links(component, directory=True)
    if path.resolve(strict=True) != path:
        raise ValueError("表情输出目录发生重定向")
    return path


def _read_bounded(path: Path, limit: int, is_current=None) -> bytes:
    _current(is_current)
    before = _no_links(path, directory=False)
    if not 0 < before.st_size <= limit:
        raise _ExportError("file_size_limit")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as source:
        opened = os.fstat(source.fileno())
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or
                (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino) or
                not 0 < opened.st_size <= limit):
            raise _ExportError("file_changed")
        chunks = []
        length = 0
        while True:
            _current(is_current)
            chunk = source.read(min(1024 * 1024, limit + 1 - length))
            if not chunk:
                break
            chunks.append(chunk)
            length += len(chunk)
            if length > limit:
                raise _ExportError("file_size_limit")
        after = os.fstat(source.fileno())
    final = _no_links(path, directory=False)
    if (length != opened.st_size or
            (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns) or
            (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns) !=
            (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)):
        raise _ExportError("file_changed")
    _current(is_current)
    return b"".join(chunks)


def _raster_info(data: bytes, *, keep_frames=False):
    if data.startswith((b"GIF87a", b"GIF89a")):
        expected = "GIF"
        if not data.endswith(b";"):
            raise _ExportError("invalid_image")
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        expected = "PNG"
    else:
        raise _ExportError("unsupported_format")
    frames = []
    durations = []
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as probe:
                if probe.format != expected:
                    raise _ExportError("invalid_image")
                probe.verify()
            with Image.open(BytesIO(data)) as image:
                width, height = image.size
                if not 1 <= width <= 4096 or not 1 <= height <= 4096:
                    raise _ExportError("image_dimension_limit")
                count = 0
                total_pixels = 0
                for index in range(MAX_FRAMES + 1):
                    try:
                        image.seek(index)
                    except EOFError:
                        break
                    if index == MAX_FRAMES:
                        raise _ExportError("image_frame_limit")
                    frame_width, frame_height = image.size
                    total_pixels += frame_width * frame_height
                    if (not 1 <= frame_width <= 4096 or not 1 <= frame_height <= 4096 or
                            total_pixels > MAX_DECODED_PIXELS):
                        raise _ExportError("image_dimension_limit")
                    image.load()
                    count += 1
                    if keep_frames:
                        frames.append(image.convert("RGBA"))
                        durations.append(max(10, min(60000, int(image.info.get("duration", 100)))))
                if not count:
                    raise _ExportError("invalid_image")
    except _ExportError:
        raise
    except Exception:
        raise _ExportError("invalid_image") from None
    return expected, width, height, count, frames, durations


def validate_emoticon_path(path) -> Path:
    """返回可按原生 type 47 发送的真实 GIF：最多 1 MiB、宽高至多 1024。"""
    resolved = _absolute_path(path)
    try:
        if len(str(resolved).encode("utf-8")) > 1023:
            raise ValueError("表情路径不能超过 1023 个 UTF-8 字节")
        data = _read_bounded(resolved, MAX_EMOTICON_BYTES)
        kind, width, height, _, _, _ = _raster_info(data)
        if kind != "GIF":
            raise ValueError("原生表情发送仅支持真实 GIF 文件")
        if width > MAX_EMOTICON_DIMENSION or height > MAX_EMOTICON_DIMENSION:
            raise ValueError("表情 GIF 宽高不能超过 1024")
        _no_links(resolved, directory=False)
        if resolved.resolve(strict=True) != resolved:
            raise ValueError("表情文件路径发生重定向")
        return resolved
    except (OSError, RuntimeError):
        raise ValueError("表情文件不存在、无法读取或路径不安全") from None
    except _ExportError as error:
        messages = {
            "file_size_limit": "表情 GIF 必须非空且不能超过 1 MiB",
            "image_frame_limit": "表情 GIF 帧数超过本地处理上限",
            "image_dimension_limit": "表情 GIF 尺寸超过本地处理上限",
        }
        raise ValueError(messages.get(str(error), "表情文件不是完整有效的 GIF")) from None


def _decrypt(ciphertext: bytes, key: bytes) -> bytes:
    if not ciphertext or len(ciphertext) % 16:
        raise _ExportError("decrypt_failed")
    try:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(key)).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()
    except ValueError:
        raise _ExportError("decrypt_failed") from None


def _extract_hevc(data: bytes) -> bytes:
    if not data.startswith(b"wxgf"):
        raise _ExportError("unsupported_format")
    # 已验证样本含 Annex-B VPS；不猜测自定义头的固定长度。
    for offset in range(4, min(len(data) - 5, 4096)):
        length = 4 if data[offset:offset + 4] == b"\0\0\0\x01" else 3
        if length == 3 and data[offset:offset + 3] != b"\0\0\x01":
            continue
        header = data[offset + length:offset + length + 2]
        if len(header) == 2 and not header[0] & 0x80 and (header[0] >> 1) & 0x3f == 32 and header[1] & 7:
            return data[offset:]
    raise _ExportError("invalid_wxgf")


def _atomic_write(directory: Path, name: str, data: bytes, is_current) -> Path:
    _current(is_current)
    _no_links(directory, directory=True)
    destination = directory / name
    if destination.parent != directory or destination.exists() or destination.is_symlink():
        raise _ExportError("output_conflict")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".part-", dir=directory)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        _current(is_current)
        _no_links(directory, directory=True)
        if directory.resolve(strict=True) != directory or destination.exists() or destination.is_symlink():
            raise _ExportError("output_conflict")
        os.replace(temporary, destination)
        _no_links(destination, directory=False)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def _ffmpeg_executable() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        raise _ExportError("decoder_unavailable") from None


def _decode_wxgf_frames(data: bytes, directory: Path, is_current):
    payload = _extract_hevc(data)
    executable = _ffmpeg_executable()
    with tempfile.TemporaryDirectory(prefix=".decode-", dir=directory) as temporary_name:
        temporary = Path(temporary_name)
        source = _atomic_write(temporary, "stream.hevc", payload, is_current)
        command = [
            executable, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-protocol_whitelist", "file,pipe", "-max_alloc", "134217728",
            "-threads", "1", "-f", "hevc", "-i", str(source), "-an", "-sn",
            "-frames:v", str(MAX_FRAMES + 1), "-vf",
            "scale=w='min(1024,iw)':h='min(1024,ih)':force_original_aspect_ratio=decrease",
            "-fps_mode", "passthrough", "-threads", "1", str(temporary / "frame-%04d.png"),
        ]
        _current(is_current)
        try:
            with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, shell=False,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as process:
                deadline = time.monotonic() + 30
                try:
                    while True:
                        _current(is_current)
                        files = [file for file in temporary.iterdir() if _FRAME_NAME.fullmatch(file.name)]
                        if (len(files) > MAX_FRAMES or
                                sum(_no_links(file, directory=False).st_size for file in files) > MAX_CACHE_TOTAL_BYTES):
                            raise _ExportError("decode_resource_limit")
                        if time.monotonic() >= deadline:
                            raise _ExportError("decode_timeout")
                        try:
                            result = process.wait(timeout=0.05)
                            break
                        except subprocess.TimeoutExpired:
                            pass
                    if result:
                        raise _ExportError("decode_failed")
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=2)
        except EmoticonSessionExpired:
            raise
        except _ExportError:
            raise
        except Exception:
            raise _ExportError("decode_failed") from None
        _current(is_current)
        files = sorted(file for file in temporary.iterdir() if _FRAME_NAME.fullmatch(file.name))
        if not 0 < len(files) <= MAX_FRAMES:
            raise _ExportError("decode_failed")
        frames = []
        pixels = 0
        total_bytes = 0
        for file in files:
            encoded = _read_bounded(file, MAX_CACHE_FILE_BYTES, is_current)
            total_bytes += len(encoded)
            if total_bytes > MAX_CACHE_TOTAL_BYTES:
                raise _ExportError("decode_resource_limit")
            _, width, height, count, decoded, _ = _raster_info(encoded, keep_frames=True)
            pixels += width * height
            if count != 1 or width > 1024 or height > 1024 or pixels > MAX_DECODED_PIXELS:
                raise _ExportError("decode_resource_limit")
            frames.append(decoded[0])
        durations = [(round((index + 1) * 100 / 30) - round(index * 100 / 30)) * 10
                     for index in range(len(frames))]
        return frames, durations


def _palette_frame(frame: Image.Image, size: tuple[int, int]) -> Image.Image:
    rgba = frame.convert("RGBA")
    if rgba.size != size:
        rgba = rgba.resize(size, Image.Resampling.LANCZOS)
    alpha = rgba.getchannel("A")
    transparent = alpha.getextrema()[0] < 128
    rgb = Image.new("RGB", size, (255, 255, 255))
    rgb.paste(rgba, mask=alpha)
    result = rgb.quantize(colors=31 if transparent else 32, method=Image.Quantize.MEDIANCUT,
                          dither=Image.Dither.NONE)
    if transparent:
        result.paste(255, mask=alpha.point(lambda value: 255 if value < 128 else 0))
        palette = result.getpalette()
        palette.extend([0] * (768 - len(palette)))
        result.putpalette(palette)
        result.info["transparency"] = 255
    return result


def _gif_bytes(frames: list[Image.Image], durations: list[int], is_current) -> bytes:
    if not frames or not 0 < len(frames) <= MAX_FRAMES or len(durations) != len(frames):
        raise _ExportError("decode_failed")
    width, height = frames[0].size
    if any(frame.size != (width, height) for frame in frames):
        raise _ExportError("decode_failed")
    scale = min(1.0, MAX_EMOTICON_DIMENSION / max(width, height))
    previous_size = None
    for reduction in (1.0, 0.8, 0.64, 0.5, 0.4, 0.32, 0.25, 0.2, 0.125):
        _current(is_current)
        size = (max(1, round(width * scale * reduction)), max(1, round(height * scale * reduction)))
        if size == previous_size:
            continue
        previous_size = size
        palettes = []
        for frame in frames:
            _current(is_current)
            palettes.append(_palette_frame(frame, size))
        output = BytesIO()
        # save_all 即使 optimize=False 仍合并相同帧；逐帧使用 Pillow 编码器，
        # 保留真实帧数和每帧时间，不为满足大小约束删帧。
        header, _ = GifImagePlugin.getheader(palettes[0], info={"loop": 0, "optimize": False})
        for block in header:
            output.write(block)
        for index, frame in enumerate(palettes):
            _current(is_current)
            options = {"duration": durations[index], "disposal": 2, "include_color_table": index > 0}
            if "transparency" in frame.info:
                options["transparency"] = frame.info["transparency"]
            for block in GifImagePlugin.getdata(frame, **options):
                output.write(block)
        output.write(b";")
        candidate = output.getvalue()
        _current(is_current)
        if len(candidate) <= MAX_EMOTICON_BYTES:
            kind, _, _, count, _, _ = _raster_info(candidate)
            if kind != "GIF" or count != len(frames):
                raise _ExportError("conversion_frame_mismatch")
            return candidate
    raise _ExportError("conversion_size_limit")


def _bound_account(account_directory, expected_account: str) -> tuple[Path, Path]:
    try:
        validate_account(expected_account)
    except (TypeError, ValueError):
        raise ValueError("必须绑定有效的当前微信账号") from None
    if expected_account in {".", ".."}:
        raise ValueError("必须绑定有效的当前微信账号")
    account = _absolute_path(account_directory)
    if account.name != expected_account:
        prefix = expected_account + "_"
        if not account.name.startswith(prefix) or not re.fullmatch(r"[A-Za-z0-9]{1,32}", account.name[len(prefix):]):
            raise ValueError("表情数据目录不属于绑定的当前微信账号")
    try:
        _no_links(account, directory=True)
        source = account / "business" / "emoticon"
        _no_links(source, directory=True)
        if account.resolve(strict=True) != account or source.resolve(strict=True) != source:
            raise ValueError("表情数据目录发生重定向")
    except OSError:
        raise ValueError("当前账号的本地表情目录不存在或无法读取") from None
    return account, source


def _cache_files(source: Path, max_files: int, is_current):
    """兼容直接文件及已确认的 Persist/Thumb 两层分片，不做任意递归。"""
    stack = [(source, None, None)]
    directories = 0
    entries = 0
    while stack:
        directory, category, shard = stack.pop()
        _current(is_current)
        _no_links(directory, directory=True)
        for entry in directory.iterdir():
            _current(is_current)
            entries += 1
            if entries > max_files + MAX_CACHE_DIRECTORIES + 16:
                raise ValueError("本地表情缓存目录条目超过本次处理限额")
            information = _no_links(entry)
            if stat.S_ISDIR(information.st_mode):
                if category is None and entry.name in {"Persist", "Thumb"}:
                    child = (entry, entry.name, None)
                elif category is not None and shard is None and re.fullmatch(r"[0-9a-fA-F]{2}", entry.name):
                    child = (entry, category, entry.name.lower())
                else:
                    # 未验证的 category/布局不被当作已支持的完整收藏来源。
                    continue
                directories += 1
                if directories > MAX_CACHE_DIRECTORIES:
                    raise ValueError("本地表情缓存分片目录超过本次处理限额")
                stack.append(child)
                continue
            if not stat.S_ISREG(information.st_mode):
                continue
            match = _CACHE_NAME.fullmatch(entry.name)
            if match is None:
                continue
            if category is not None:
                if (shard is None or match.group(1)[:2].lower() != shard or
                        bool(match.group(2)) != (category == "Thumb")):
                    raise ValueError("本地表情缓存的分类或分片与文件标识不一致")
            yield entry, information, match


def export_local_emoticons(
    account_directory, output_directory, key: bytes, expected_account: str, *,
    convert: bool = True, is_current: Callable[[], bool] | None = None,
    max_files: int = MAX_CACHE_FILES, max_file_bytes: int = MAX_CACHE_FILE_BYTES,
    max_total_bytes: int = MAX_CACHE_TOTAL_BYTES,
) -> tuple[ExportedEmoticon, ...]:
    """只读当前账号 business/emoticon 中已确认布局，返回本次导出的缓存集合。

    支持直接文件、Persist/<前两位hex>/<标识> 和 Thumb/<前两位hex>/<标识>.thumb。
    其他分类/更深层级不遍历，不将本地缓存声称为完整收藏列表。
    原始缓存名必须是 32 个十六进制字符，可另带 .thumb；该标识不承诺是
    明文内容的 MD5。仅支持经严格解密确认的 PNG/GIF/WXGF。单文件失败以
    固定 error code 返回，账号/路径/限流错误抛 ValueError，会话变化立即抛
    EmoticonSessionExpired。已成功导出的原件在转换失败时仍保留。
    """
    if not isinstance(key, bytes) or len(key) != 16:
        raise ValueError("本地表情解密需要 16 字节 AES-128 密钥")
    if not isinstance(convert, bool):
        raise ValueError("convert 必须是 bool")
    for value, ceiling in ((max_files, MAX_CACHE_FILES), (max_file_bytes, MAX_CACHE_FILE_BYTES),
                           (max_total_bytes, MAX_CACHE_TOTAL_BYTES)):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= ceiling:
            raise ValueError("表情导出限额无效或超过安全上限")
    _current(is_current)
    account, source = _bound_account(account_directory, expected_account)
    output = _absolute_path(output_directory)
    if output == account or account in output.parents:
        raise ValueError("表情输出目录不能位于微信账号数据目录内")
    # 先检查文件数、总量和所有路径，再创建输出；不访问兄弟账号或 cache。
    grouped = {}
    total_bytes = 0
    count = 0
    try:
        for entry, information, match in _cache_files(source, max_files, is_current):
            if information.st_nlink != 1:
                raise ValueError("表情缓存不能是硬链接")
            count += 1
            total_bytes += information.st_size
            if (count > max_files or not 0 < information.st_size <= max_file_bytes or
                    total_bytes > max_total_bytes):
                raise ValueError("本地表情缓存超过本次导出限额")
            identifier = match.group(1).lower()
            part = "thumbnail" if match.group(2) else "original"
            files = grouped.setdefault(identifier, {})
            if part in files:
                raise ValueError("本地表情缓存存在重复标识")
            files[part] = entry
    except OSError:
        raise ValueError("当前账号的本地表情目录无法读取") from None
    _current(is_current)
    if not grouped:
        return ()
    namespace = _make_directory(output / ("account-" + sha256(expected_account.encode("ascii")).hexdigest()[:24]))
    results = []
    for identifier, files in sorted(grouped.items()):
        _current(is_current)
        _no_links(source, directory=True)
        item_directory = Path(tempfile.mkdtemp(prefix=identifier + "-", dir=namespace))
        _no_links(item_directory, directory=True)
        values = {"identifier": identifier}
        errors = []
        original_data = None
        for part in ("original", "thumbnail"):
            if part not in files:
                continue
            try:
                data = _decrypt(_read_bounded(files[part], max_file_bytes, is_current), key)
                if data.startswith(b"wxgf"):
                    _extract_hevc(data)
                    kind, width, height, frames = "WXGF", 0, 0, 0
                else:
                    kind, width, height, frames, _, _ = _raster_info(data)
                name = ("original" if part == "original" else "thumbnail") + "." + kind.lower()
                values["original_path" if part == "original" else "thumbnail_path"] = _atomic_write(
                    item_directory, name, data, is_current)
                if part == "original":
                    original_data = data
                    values.update(format=kind, width=width, height=height, frame_count=frames)
            except EmoticonSessionExpired:
                raise
            except _ExportError as error:
                errors.append(part + ":" + str(error))
            except (OSError, ValueError):
                errors.append(part + ":file_unavailable")
        if original_data is not None:
            try:
                if (values["format"] == "GIF" and len(original_data) <= MAX_EMOTICON_BYTES and
                        values["width"] <= MAX_EMOTICON_DIMENSION and values["height"] <= MAX_EMOTICON_DIMENSION):
                    values.update(gif_path=values["original_path"], sendable=True)
                elif convert:
                    if values["format"] == "WXGF":
                        frames, durations = _decode_wxgf_frames(original_data, item_directory, is_current)
                    else:
                        _, _, _, _, frames, durations = _raster_info(original_data, keep_frames=True)
                    _current(is_current)
                    candidate = _gif_bytes(frames, durations, is_current)
                    _current(is_current)
                    gif = _atomic_write(item_directory, "preview.gif", candidate, is_current)
                    validate_emoticon_path(gif)
                    _, width, height, count, _, _ = _raster_info(candidate)
                    values.update(gif_path=gif, sendable=True, width=width, height=height, frame_count=count)
            except EmoticonSessionExpired:
                raise
            except _ExportError as error:
                errors.append("convert:" + str(error))
            except Exception:
                errors.append("convert:conversion_failed")
        _current(is_current)
        values["error"] = ";".join(errors)
        results.append(ExportedEmoticon(**values))
        try:
            item_directory.rmdir()  # 仅清理本函数新建且仍为空的目录。
        except OSError:
            pass
    return tuple(results)
