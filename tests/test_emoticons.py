"""仅使用合成账号目录、密钥和媒体；不读取微信或真实表情。"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, asdict
from io import BytesIO
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image

from wxbot_framework import emoticons


KEY = bytes(range(16))  # 合成测试材料，与任何运行中的账号无关。
ACCOUNT = "wxid_synthetic"
IDENTIFIER = "0123456789abcdef0123456789abcdef"
SECOND_IDENTIFIER = "fedcba9876543210fedcba9876543210"


def raster(kind="GIF", size=(3, 2), color=(20, 120, 230, 255)):
    output = BytesIO()
    Image.new("RGBA", size, color).save(output, format=kind)
    return output.getvalue()


def encrypted(data, key=KEY):
    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(key)).encryptor()
    return cipher.update(padded) + cipher.finalize()


def invalid_padding_ciphertext():
    # 最后一个 padding count=2，但倒数第二个字节不是 2，必须严格拒绝。
    padded = b"A" * 14 + b"\x01\x02"
    cipher = Cipher(algorithms.AES(KEY), modes.CBC(KEY)).encryptor()
    return cipher.update(padded) + cipher.finalize()


def wxgf():
    # 只用于解析/受控解码替身，不声称这是可解码的 HEVC 视频。
    return b"wxgf" + b"synthetic-header" + b"\0\0\0\x01\x40\x01" + b"synthetic-vps"


def animation(count=47, size=(32, 24)):
    frames = [Image.new("RGBA", size, (index * 5 % 256, index * 11 % 256, index * 17 % 256, 255))
              for index in range(count)]
    durations = [(round((index + 1) * 100 / 30) - round(index * 100 / 30)) * 10
                 for index in range(count)]
    return frames, durations


def padded_gif(data, size):
    difference = size - len(data)
    blocks = (difference - 3 + 255) // 256
    payload = b"x" * (difference - 3 - blocks)
    comment = b"\x21\xfe" + b"".join(bytes([len(payload[index:index + 255])]) + payload[index:index + 255]
                                    for index in range(0, len(payload), 255)) + b"\0"
    result = data[:-1] + comment + b";"
    assert len(result) == size
    return result


class EmoticonTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="helper-emoticon-test-")
        self.root = Path(self.temporary.name).resolve()
        self.account = self.root / (ACCOUNT + "_a1b2c3")
        self.cache = self.account / "business" / "emoticon"
        self.cache.mkdir(parents=True)
        self.output = self.root / "exports"

    def tearDown(self):
        self.temporary.cleanup()

    def source(self, data, *, name=IDENTIFIER, cipher=True):
        path = self.cache / name
        path.write_bytes(encrypted(data) if cipher else data)
        return path

    def sharded_source(self, data, *, identifier=IDENTIFIER, thumbnail=False):
        category = "Thumb" if thumbnail else "Persist"
        directory = self.cache / category / identifier[:2]
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (identifier + (".thumb" if thumbnail else ""))
        path.write_bytes(encrypted(data))
        return path

    def export(self, **kwargs):
        return emoticons.export_local_emoticons(self.account, self.output, KEY, ACCOUNT, **kwargs)

    def validate_file(self, data, name="synthetic.gif"):
        path = self.root / name
        path.write_bytes(data)
        return emoticons.validate_emoticon_path(path)

    def test_validator_accepts_complete_gif_and_resolves_path(self):
        actual = self.validate_file(raster(), "合成表情.gif")
        self.assertEqual(actual, self.root / "合成表情.gif")
        self.assertTrue(actual.is_absolute())

    def test_validator_uses_contents_instead_of_extension(self):
        actual = self.validate_file(raster(), "synthetic.bin")
        self.assertEqual(actual.suffix, ".bin")
        with self.assertRaises(ValueError):
            self.validate_file(raster("PNG"))

    def test_validator_rejects_fake_or_truncated_gif(self):
        for data in (b"GIF89a", b"GIF89a" + b"\0" * 32 + b";", raster()[:-1], raster()[:20] + b";"):
            with self.subTest(data=data[:6]), self.assertRaises(ValueError):
                self.validate_file(data)

    def test_validator_size_limit_is_inclusive(self):
        self.validate_file(padded_gif(raster(), emoticons.MAX_EMOTICON_BYTES))
        with self.assertRaises(ValueError):
            self.validate_file(padded_gif(raster(), emoticons.MAX_EMOTICON_BYTES + 1))

    def test_validator_dimension_limit_is_inclusive(self):
        self.validate_file(raster(size=(1024, 1)))
        with self.assertRaises(ValueError):
            self.validate_file(raster(size=(1025, 1)))

    def test_validator_rejects_missing_directory_and_nul(self):
        for path in (self.root / "missing.gif", self.root, "", "bad\0.gif", None):
            with self.subTest(path=path), self.assertRaises(ValueError):
                emoticons.validate_emoticon_path(path)

    def test_validator_rejects_hardlink(self):
        original = self.root / "source.gif"
        original.write_bytes(raster())
        linked = self.root / "linked.gif"
        os.link(original, linked)
        with self.assertRaises(ValueError):
            emoticons.validate_emoticon_path(linked)

    def test_validator_rejects_symbolic_link(self):
        original = self.root / "source.gif"
        original.write_bytes(raster())
        linked = self.root / "linked.gif"
        try:
            linked.symlink_to(original)
        except OSError:
            self.skipTest("Windows 当前权限未允许创建 symlink")
        with self.assertRaises(ValueError):
            emoticons.validate_emoticon_path(linked)

    def test_validator_rejects_windows_reparse_attribute(self):
        path = self.root / "source.gif"
        path.write_bytes(raster())
        original = Path.lstat

        def information(candidate):
            real = original(candidate)
            if candidate == path:
                return SimpleNamespace(st_mode=real.st_mode, st_file_attributes=0x400)
            return real

        with patch.object(Path, "lstat", information), self.assertRaises(ValueError):
            emoticons.validate_emoticon_path(path)

    def test_decrypt_exports_original_and_thumbnail_byte_for_byte(self):
        original, thumbnail = raster(), raster("PNG", size=(2, 2))
        first = self.source(original)
        second = self.source(thumbnail, name=IDENTIFIER + ".thumb")
        before = first.read_bytes(), second.read_bytes()
        result, = self.export(convert=False)
        self.assertEqual(result.identifier, IDENTIFIER)
        self.assertEqual(result.original_path.read_bytes(), original)
        self.assertEqual(result.thumbnail_path.read_bytes(), thumbnail)
        self.assertEqual((result.format, result.width, result.height, result.frame_count), ("GIF", 3, 2, 1))
        self.assertTrue(result.sendable)
        self.assertEqual(result.gif_path, result.original_path)
        self.assertEqual(result.error, "")
        self.assertEqual(before, (first.read_bytes(), second.read_bytes()))
        self.assertNotIn(ACCOUNT, str(result.original_path))
        with self.assertRaises(FrozenInstanceError):
            result.sendable = False
        self.assertNotIn("key", asdict(result))

    def test_export_only_bound_business_directory(self):
        self.source(raster())
        received = self.account / "cache" / "synthetic-date" / "Emoticon"
        received.mkdir(parents=True)
        (received / SECOND_IDENTIFIER).write_bytes(encrypted(raster("PNG")))
        sibling = self.root / "wxid_other" / "business" / "emoticon"
        sibling.mkdir(parents=True)
        (sibling / SECOND_IDENTIFIER).write_bytes(encrypted(raster("PNG")))
        self.source(b"not an image", name="unrelated.db")
        (self.cache / "nested").mkdir()
        (self.cache / "nested" / SECOND_IDENTIFIER).write_bytes(encrypted(raster("PNG")))
        result, = self.export(convert=False)
        self.assertEqual(result.identifier, IDENTIFIER)

    def test_confirmed_persist_thumb_shards_merge_into_one_item(self):
        original, thumbnail = raster(), raster("PNG", size=(2, 2))
        self.sharded_source(original)
        self.sharded_source(thumbnail, thumbnail=True)
        result, = self.export(convert=False)
        self.assertEqual(result.identifier, IDENTIFIER)
        self.assertEqual(result.original_path.read_bytes(), original)
        self.assertEqual(result.thumbnail_path.read_bytes(), thumbnail)
        self.assertTrue(result.sendable)

    def test_shard_prefix_and_part_category_must_match(self):
        for category, shard, name in (("Persist", "aa", IDENTIFIER),
                                      ("Persist", "01", IDENTIFIER + ".thumb"),
                                      ("Thumb", "01", IDENTIFIER)):
            directory = self.cache / category / shard
            directory.mkdir(parents=True, exist_ok=True)
            file = directory / name
            file.write_bytes(encrypted(raster()))
            with self.subTest(category=category, shard=shard), self.assertRaises(ValueError):
                self.export()
            file.unlink()
        self.assertFalse(self.output.exists())

    def test_duplicate_part_across_direct_and_shard_is_rejected(self):
        self.source(raster())
        self.sharded_source(raster())
        with self.assertRaises(ValueError):
            self.export()
        self.assertFalse(self.output.exists())

    def test_unknown_category_and_deeper_directories_are_not_followed(self):
        self.sharded_source(raster())
        for directory in (self.cache / "Other" / "fe", self.cache / "Persist" / "01" / "deeper"):
            directory.mkdir(parents=True)
            (directory / SECOND_IDENTIFIER).write_bytes(encrypted(raster("PNG")))
        result, = self.export(convert=False)
        self.assertEqual(result.identifier, IDENTIFIER)

    def test_reparse_shard_is_rejected_before_output(self):
        self.sharded_source(raster())
        shard = self.cache / "Persist" / "01"
        original = Path.lstat

        def information(candidate):
            real = original(candidate)
            if candidate == shard:
                return SimpleNamespace(st_mode=real.st_mode, st_file_attributes=0x400)
            return real

        with patch.object(Path, "lstat", information), self.assertRaises(ValueError):
            self.export()
        self.assertFalse(self.output.exists())

    def test_directory_entry_limit_is_bounded_even_for_unknown_files(self):
        for index in range(20):
            (self.cache / ("ignored-" + str(index))).write_bytes(b"ignored")
        with patch.object(emoticons, "MAX_CACHE_DIRECTORIES", 1), self.assertRaises(ValueError):
            self.export(max_files=1)
        self.assertFalse(self.output.exists())

    def test_exact_account_directory_name_is_allowed(self):
        exact = self.root / ACCOUNT
        (exact / "business" / "emoticon").mkdir(parents=True)
        self.assertEqual(emoticons.export_local_emoticons(exact, self.output, KEY, ACCOUNT), ())

    def test_other_account_or_misleading_prefix_is_rejected(self):
        for name in ("wxid_other", ACCOUNT + "evil", ACCOUNT + "_", ACCOUNT + "_bad_suffix",
                     ACCOUNT + "_" + "a" * 33):
            account = self.root / name
            (account / "business" / "emoticon").mkdir(parents=True)
            with self.subTest(name=name), self.assertRaises(ValueError):
                emoticons.export_local_emoticons(account, self.output, KEY, ACCOUNT)
        self.assertFalse(self.output.exists())

    def test_invalid_account_or_key_is_rejected(self):
        for account in ("..", "filehelper", "bad/account", None):
            with self.subTest(account=account), self.assertRaises(ValueError):
                emoticons.export_local_emoticons(self.account, self.output, KEY, account)
        for key in (b"", b"a" * 15, b"a" * 17, bytearray(KEY), None):
            with self.subTest(length=len(key) if key is not None else None), self.assertRaises(ValueError):
                emoticons.export_local_emoticons(self.account, self.output, key, ACCOUNT)

    def test_output_cannot_modify_account_directory(self):
        self.source(raster())
        for output in (self.account, self.cache, self.account / "exports"):
            with self.subTest(output=output), self.assertRaises(ValueError):
                emoticons.export_local_emoticons(self.account, output, KEY, ACCOUNT)

    def test_empty_cache_does_not_create_output(self):
        self.assertEqual(self.export(), ())
        self.assertFalse(self.output.exists())

    def test_bad_padding_wrong_key_and_nonimage_fail_safely(self):
        cases = (invalid_padding_ciphertext(), encrypted(raster(), key=bytes(reversed(KEY))),
                 b"not block aligned", encrypted(b"GIF89a\0\0;"), encrypted(b"private unrecognized text"))
        for ciphertext in cases:
            with self.subTest(length=len(ciphertext)):
                self.source(ciphertext, cipher=False)
                result, = self.export()
                self.assertFalse(result.sendable)
                self.assertIsNone(result.original_path)
                self.assertIsNone(result.gif_path)
                self.assertRegex(result.error, r"^original:(decrypt_failed|invalid_image|unsupported_format)$")
                self.assertNotIn("private", repr(result))
                self.assertFalse(any(path.is_file() for path in self.output.rglob("*")))

    def test_thumbnail_can_export_when_original_is_invalid(self):
        self.source(b"invalid")
        thumbnail = raster("PNG")
        self.source(thumbnail, name=IDENTIFIER + ".thumb")
        result, = self.export()
        self.assertEqual(result.thumbnail_path.read_bytes(), thumbnail)
        self.assertIsNone(result.original_path)
        self.assertIsNone(result.gif_path)
        self.assertFalse(result.sendable)

    def test_thumbnail_only_is_not_implicitly_sendable(self):
        self.source(raster(), name=IDENTIFIER + ".thumb")
        result, = self.export()
        self.assertTrue(result.thumbnail_path.is_file())
        self.assertFalse(result.sendable)

    def test_wxgf_is_preserved_without_conversion(self):
        data = wxgf()
        self.source(data)
        result, = self.export(convert=False)
        self.assertEqual(result.format, "WXGF")
        self.assertEqual(result.original_path.read_bytes(), data)
        self.assertFalse(result.sendable)
        self.assertEqual(result.error, "")

    def test_wxgf_missing_vps_is_rejected(self):
        self.source(b"wxgf" + b"not hevc")
        result, = self.export(convert=False)
        self.assertEqual(result.error, "original:invalid_wxgf")
        self.assertIsNone(result.original_path)

    def test_vps_extraction_has_no_fixed_header_offset(self):
        for header_size, start_code in ((4, b"\0\0\x01"), (28, b"\0\0\0\x01")):
            data = b"wxgf" + b"x" * header_size + start_code + b"\x40\x01synthetic"
            self.assertEqual(emoticons._extract_hevc(data), start_code + b"\x40\x01synthetic")
        with self.assertRaises(ValueError):
            emoticons._extract_hevc(b"wxgf" + b"x" * 4096 + b"\0\0\0\x01\x40\x01")

    def test_mock_wxgf_conversion_preserves_47_frames_at_30fps(self):
        self.source(wxgf())
        with patch.object(emoticons, "_decode_wxgf_frames", return_value=animation()) as decoder:
            result, = self.export()
        decoder.assert_called_once()
        self.assertTrue(result.sendable)
        self.assertEqual(result.frame_count, 47)
        self.assertLessEqual(result.gif_path.stat().st_size, 1024 * 1024)
        with Image.open(result.gif_path) as image:
            self.assertEqual(image.n_frames, 47)
            self.assertEqual(image.size, (32, 24))
            durations = []
            for index in range(image.n_frames):
                image.seek(index)
                durations.append(image.info["duration"])
            self.assertLess(abs(sum(durations) - 47 * 1000 / 30), 10)
        self.assertEqual(result.error, "")

    def test_identical_frames_are_not_silently_coalesced(self):
        self.source(wxgf())
        frames = [Image.new("RGBA", (8, 8), (255, 100, 0, 255)) for _ in range(47)]
        with patch.object(emoticons, "_decode_wxgf_frames", return_value=(frames, [30] * 47)):
            result, = self.export()
        self.assertTrue(result.sendable, result.error)
        with Image.open(result.gif_path) as image:
            self.assertEqual(image.n_frames, 47)
            for index in range(47):
                image.seek(index)
                self.assertEqual(image.convert("RGB").getpixel((0, 0)), (255, 100, 0))

    def test_transparent_frames_preserve_transparent_pixels(self):
        self.source(wxgf())
        frames, durations = animation(count=3, size=(4, 4))
        for frame in frames:
            frame.putpixel((0, 0), (0, 0, 0, 0))
        with patch.object(emoticons, "_decode_wxgf_frames", return_value=(frames, durations)):
            result, = self.export()
        self.assertTrue(result.sendable, result.error)
        with Image.open(result.gif_path) as image:
            self.assertEqual(image.n_frames, 3)
            for index in range(3):
                image.seek(index)
                self.assertEqual(image.convert("RGBA").getpixel((0, 0))[3], 0)

    def test_png_conversion_keeps_original(self):
        data = raster("PNG")
        self.source(data)
        result, = self.export()
        self.assertEqual(result.original_path.read_bytes(), data)
        self.assertEqual(result.format, "PNG")
        self.assertTrue(result.sendable)
        self.assertEqual(emoticons.validate_emoticon_path(result.gif_path), result.gif_path)

    def test_conversion_failure_keeps_original_and_sanitizes_error(self):
        self.source(wxgf())
        with patch.object(emoticons, "_decode_wxgf_frames", side_effect=RuntimeError("sensitive-key https://private.test")):
            result, = self.export()
        self.assertTrue(result.original_path.is_file())
        self.assertIsNone(result.gif_path)
        self.assertFalse(result.sendable)
        self.assertEqual(result.error, "convert:conversion_failed")
        self.assertNotIn("sensitive", repr(result))
        self.assertNotIn("https://", repr(result))

    def test_oversized_conversion_is_never_marked_sendable(self):
        self.source(wxgf())
        with patch.object(emoticons, "_decode_wxgf_frames", return_value=animation()), \
                patch.object(emoticons, "_gif_bytes", side_effect=emoticons._ExportError("conversion_size_limit")):
            result, = self.export()
        self.assertFalse(result.sendable)
        self.assertIsNone(result.gif_path)
        self.assertEqual(result.error, "convert:conversion_size_limit")
        self.assertEqual([path.name for path in result.original_path.parent.iterdir()], ["original.wxgf"])

    def test_actual_oversized_gif_is_reduced_and_remains_47_frames(self):
        self.source(wxgf())
        frames = []
        for index in range(47):
            noise = Image.effect_noise((240, 240), 80).convert("RGB")
            noise.putpixel((0, 0), (index * 5, 255, 0))
            frames.append(noise.convert("RGBA"))
        with patch.object(emoticons, "_decode_wxgf_frames", return_value=(frames, [30] * 47)):
            result, = self.export()
        self.assertTrue(result.sendable, result.error)
        self.assertEqual(result.frame_count, 47)
        self.assertLessEqual(result.gif_path.stat().st_size, emoticons.MAX_EMOTICON_BYTES)
        self.assertLess(result.width, 240)

    def test_cancelled_before_export_creates_nothing(self):
        self.source(raster())
        with self.assertRaises(emoticons.EmoticonSessionExpired):
            self.export(is_current=lambda: False)
        self.assertFalse(self.output.exists())

    def test_cancelled_before_atomic_write_does_not_write_plaintext(self):
        self.source(raster())
        active = [True]
        original = emoticons._decrypt

        def decrypt(*args):
            data = original(*args)
            active[0] = False
            return data

        with patch.object(emoticons, "_decrypt", side_effect=decrypt), self.assertRaises(emoticons.EmoticonSessionExpired):
            self.export(is_current=lambda: active[0])
        self.assertFalse(any(path.is_file() for path in self.output.rglob("*")))

    def test_cancelled_after_conversion_does_not_write_preview(self):
        self.source(wxgf())
        self.source(raster(), name=SECOND_IDENTIFIER)
        active = [True]

        def decode(*args):
            active[0] = False
            return animation()

        with patch.object(emoticons, "_decode_wxgf_frames", side_effect=decode), self.assertRaises(emoticons.EmoticonSessionExpired):
            self.export(is_current=lambda: active[0])
        self.assertEqual([path.name for path in self.output.rglob("*") if path.is_file()], ["original.wxgf"])

    def test_callback_error_is_a_session_expiration(self):
        self.source(raster())
        with self.assertRaisesRegex(emoticons.EmoticonSessionExpired, "^emoticon_session_expired$"):
            self.export(is_current=lambda: 1 / 0)

    def test_limits_are_checked_before_any_output(self):
        first = self.source(raster())
        self.source(raster(), name=SECOND_IDENTIFIER)
        for options in ({"max_files": 1}, {"max_file_bytes": first.stat().st_size - 1},
                        {"max_total_bytes": first.stat().st_size}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.export(**options)
            self.assertFalse(self.output.exists())

    def test_invalid_limits_cannot_raise_the_hard_ceiling(self):
        for options in ({"max_files": 257}, {"max_file_bytes": 20 * 1024 * 1024 + 1},
                        {"max_total_bytes": 100 * 1024 * 1024 + 1}, {"max_files": 0}, {"max_files": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.export(**options)

    def test_convert_requires_a_boolean(self):
        for value in (0, 1, "false", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.export(convert=value)

    def test_encrypted_plaintext_is_not_accepted_without_decryption(self):
        self.source(raster(), cipher=False)
        result, = self.export()
        self.assertFalse(result.sendable)
        self.assertIsNone(result.original_path)

    def test_concurrent_exports_do_not_overwrite(self):
        original = raster()
        self.source(original)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.export(convert=False)[0], range(8)))
        paths = {result.original_path for result in results}
        self.assertEqual(len(paths), 8)
        self.assertTrue(all(path.read_bytes() == original for path in paths))
        self.assertEqual(len({path.parent.parent for path in paths}), 1)
        self.assertFalse(any(path.name.startswith(".part-") for path in self.output.rglob("*")))

    def test_distinct_accounts_have_distinct_hash_namespaces(self):
        self.source(raster())
        first, = self.export(convert=False)
        second_account = "wxid_synthetic_other"
        other = self.root / second_account
        cache = other / "business" / "emoticon"
        cache.mkdir(parents=True)
        (cache / IDENTIFIER).write_bytes(encrypted(raster()))
        second, = emoticons.export_local_emoticons(other, self.output, KEY, second_account, convert=False)
        self.assertNotEqual(first.original_path.parent.parent, second.original_path.parent.parent)
        self.assertNotIn(second_account, str(second.original_path))

    def test_atomic_failure_leaves_no_partial_file_or_sensitive_error(self):
        self.source(raster())
        with patch.object(emoticons.os, "replace", side_effect=OSError("sensitive path/key")):
            result, = self.export()
        self.assertEqual(result.error, "original:file_unavailable")
        self.assertFalse(any(path.is_file() for path in self.output.rglob("*")))

    def test_source_hardlink_is_rejected_before_output(self):
        original = self.root / "outside-encrypted"
        original.write_bytes(encrypted(raster()))
        os.link(original, self.cache / IDENTIFIER)
        with self.assertRaises(ValueError):
            self.export()
        self.assertFalse(self.output.exists())

    def test_source_directory_reparse_is_rejected(self):
        self.source(raster())
        original = Path.lstat

        def information(candidate):
            real = original(candidate)
            if candidate == self.account / "business":
                return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_file_attributes=0x400)
            return real

        with patch.object(Path, "lstat", information), self.assertRaises(ValueError):
            self.export()
        self.assertFalse(self.output.exists())

    def test_output_directory_reparse_is_rejected(self):
        self.source(raster())
        self.output.mkdir()
        original = Path.lstat

        def information(candidate):
            real = original(candidate)
            if candidate == self.output:
                return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_file_attributes=0x400)
            return real

        with patch.object(Path, "lstat", information), self.assertRaises(ValueError):
            self.export()
        self.assertFalse(any(self.output.iterdir()))

    def test_real_ffmpeg_synthetic_hevc_roundtrip_preserves_47_frames(self):
        # 从数学测试图生成，无实际账号或表情字节。编码器缺失应给出明确测试失败。
        executable = emoticons._ffmpeg_executable()
        encoded = subprocess.run(
            [executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
             "testsrc2=size=32x24:rate=30", "-frames:v", "47", "-c:v", "libx265", "-preset", "ultrafast",
             "-x265-params", "pools=1:frame-threads=1:log-level=error", "-pix_fmt", "yuv420p",
             "-f", "hevc", "pipe:1"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=20, shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(encoded.returncode, 0, "合成 HEVC 编码失败")
        self.assertTrue(encoded.stdout)
        data = b"wxgf" + b"synthetic-container" + encoded.stdout
        self.sharded_source(data)
        self.sharded_source(raster("PNG", size=(16, 16)), thumbnail=True)
        result, = self.export()
        self.assertTrue(result.sendable, result.error)
        self.assertEqual((result.width, result.height, result.frame_count), (32, 24, 47))
        self.assertEqual(result.original_path.read_bytes(), data)
        self.assertEqual(emoticons.validate_emoticon_path(result.gif_path), result.gif_path)
        self.assertFalse(any(path.name.startswith((".decode-", ".part-")) for path in self.output.rglob("*")))

    def test_real_decoder_failure_removes_owned_temporary_files(self):
        self.source(wxgf())
        result, = self.export()
        self.assertFalse(result.sendable)
        self.assertEqual(result.error, "convert:decode_failed")
        self.assertEqual([path.name for path in result.original_path.parent.iterdir()], ["original.wxgf"])


if __name__ == "__main__":
    unittest.main()
