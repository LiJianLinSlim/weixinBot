"""图片 XML 与本地存储测试，不连接微信、网络或原生服务。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wxbot_framework.image_metadata import (
    ImageMetadata, ImageMetadataError, ImageStore, MAX_IMAGE_DIMENSION,
    MAX_IMAGE_XML_BYTES, MAX_METADATA_NUMBER, parse_image_metadata,
)
from wxbot_framework.images import MAX_IMAGE_BYTES


MD5 = "0123456789abcdef0123456789abcdef"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
ACCOUNT = "wxid_image_metadata_robot"
SCRATCH = Path(__file__).resolve().parent.parent / ".runtime/tests"


class MetadataTests(unittest.TestCase):
    def test_extracts_known_fields_and_normalizes_md5(self):
        result = parse_image_metadata(
            f'<msg><img md5="{MD5.upper()}" width="1920" height="1080" '
            'length="12345" hdlength="45678" cdnthumbwidth="120" cdnthumbheight="80" '
            'cdnthumblength="234" cdnmidwidth="960" cdnmidheight="540" encryver="1" '
            'cdnthumburl="https://example.test/thumb?a=1&amp;b=2" '
            'cdnmidimgurl="https://example.test/mid" cdnbigimgurl="https://example.test/big" '
            'aeskey="secret-image-key" cdnthumbaeskey="secret-thumb-key" '
            'filekey="secret-file-key" /></msg>'
        )
        self.assertEqual((result.md5, result.width, result.height, result.length), (MD5, 1920, 1080, 12345))
        self.assertEqual((result.hdlength, result.cdnthumbwidth, result.cdnthumbheight,
                          result.cdnthumblength, result.cdnmidwidth, result.cdnmidheight, result.encryver),
                         (45678, 120, 80, 234, 960, 540, 1))
        self.assertEqual(result.cdnthumburl, "https://example.test/thumb?a=1&b=2")
        self.assertEqual(result.cdnmidimgurl, "https://example.test/mid")
        self.assertEqual(result.cdnbigimgurl, "https://example.test/big")
        self.assertEqual((result.aeskey, result.cdnthumbaeskey, result.filekey),
                         ("secret-image-key", "secret-thumb-key", "secret-file-key"))

    def test_result_is_immutable_and_repr_does_not_expose_secrets_or_urls(self):
        result = parse_image_metadata('<msg><img aeskey="secret-a" cdnthumbaeskey="secret-b" '
                                      'filekey="secret-c" cdnthumburl="private-thumb" '
                                      'cdnmidimgurl="private-mid" cdnbigimgurl="private-big" /></msg>')
        for private in ("secret-a", "secret-b", "secret-c", "private-thumb", "private-mid", "private-big"):
            self.assertNotIn(private, repr(result))
            self.assertNotIn(private, str(result))
        with self.assertRaises(FrozenInstanceError):
            result.width = 1
        self.assertFalse(hasattr(result, "__dict__"))

    def test_missing_fields_are_explicit_and_unknown_attributes_do_not_create_paths(self):
        result = parse_image_metadata('<msg><img path="../../outside.png" arbitrary="ignored" /></msg>')
        self.assertEqual(result, ImageMetadata())
        self.assertFalse(hasattr(result, "path"))

    def test_xml_declaration_and_safe_escaped_values_are_supported(self):
        result = parse_image_metadata('<?xml version="1.0" encoding="utf-8"?><msg><img '
                                      'filekey="a&amp;b&lt;c&quot;d" /></msg>')
        self.assertEqual(result.filekey, 'a&b<c"d')

    def test_rejects_dtd_entity_declarations_and_undefined_entities(self):
        malicious = (
            '<!DOCTYPE msg [<!ENTITY x "expanded">]><msg><img aeskey="&x;" /></msg>',
            '<!DOCTYPE msg SYSTEM "file:///C:/private.txt"><msg><img /></msg>',
            '<!doctype msg [<!entity x SYSTEM "https://example.test/secret">]><msg><img /></msg>',
            '<!ENTITY x "expanded"><msg><img /></msg>',
            '<msg><img aeskey="&undefined;" /></msg>',
        )
        for value in malicious:
            with self.subTest(value=value), self.assertRaises(ImageMetadataError):
                parse_image_metadata(value)

    def test_rejects_nul_oversize_bad_unicode_and_non_strings(self):
        invalid = (None, b"<msg><img /></msg>", 1, "", "  ", "<msg>\0<img /></msg>",
                   "<msg>\ud800<img /></msg>", "<msg><img /></msg>" + " " * MAX_IMAGE_XML_BYTES,
                   '<msg><img filekey="' + "图" * (MAX_IMAGE_XML_BYTES // 2) + '" /></msg>')
        for value in invalid:
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ImageMetadataError):
                parse_image_metadata(value)

    def test_xml_byte_limit_accepts_boundary(self):
        body = "<msg><img /></msg>"
        result = parse_image_metadata(body + " " * (MAX_IMAGE_XML_BYTES - len(body)))
        self.assertEqual(result, ImageMetadata())

    def test_rejects_malformed_missing_nested_duplicate_or_wrong_root(self):
        invalid = ("<msg><img></msg>", "<msg />", "<img />", "<msg><img /><img /></msg>",
                   "<msg><wrapper><img /></wrapper></msg>", "<msg><img /></msg><msg />",
                   "wxid_member:\n<msg><img /></msg>", '<msg xmlns="other"><img /></msg>',
                   "<msg><img /><wrapper><img /></wrapper></msg>")
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ImageMetadataError):
                parse_image_metadata(value)

    def test_rejects_excessive_structure_and_attributes(self):
        invalid = (
            "<msg><img>" + "<x>" * 17 + "</x>" * 17 + "</img></msg>",
            "<msg><img />" + "<x />" * 128 + "</msg>",
            "<msg><img " + " ".join(f'a{i}="x"' for i in range(65)) + " /></msg>",
        )
        for value in invalid:
            with self.subTest(length=len(value)), self.assertRaises(ImageMetadataError):
                parse_image_metadata(value)

    def test_rejects_invalid_md5_without_echoing_xml_or_secret(self):
        for value in ("", "0" * 31, "0" * 33, "z" * 32, "../outside.png", "a\u3000" * 16):
            with self.subTest(value=value):
                with self.assertRaises(ImageMetadataError) as raised:
                    parse_image_metadata(f'<msg><img md5="{value}" aeskey="private-secret" /></msg>')
                self.assertNotIn("private-secret", str(raised.exception))
                self.assertNotIn("<msg", str(raised.exception))

    def test_numeric_limits_and_zero_remain_explicit(self):
        result = parse_image_metadata(f'<msg><img width="{MAX_IMAGE_DIMENSION}" height="0" '
                                      f'length="{MAX_METADATA_NUMBER}" encryver="0" /></msg>')
        self.assertEqual((result.width, result.height, result.length, result.encryver),
                         (MAX_IMAGE_DIMENSION, 0, MAX_METADATA_NUMBER, 0))
        for name, maximum in (("width", MAX_IMAGE_DIMENSION), ("height", MAX_IMAGE_DIMENSION),
                              ("cdnthumbwidth", MAX_IMAGE_DIMENSION), ("cdnthumbheight", MAX_IMAGE_DIMENSION),
                              ("cdnmidwidth", MAX_IMAGE_DIMENSION), ("cdnmidheight", MAX_IMAGE_DIMENSION),
                              ("length", MAX_METADATA_NUMBER), ("hdlength", MAX_METADATA_NUMBER),
                              ("cdnthumblength", MAX_METADATA_NUMBER), ("encryver", MAX_METADATA_NUMBER)):
            for value in ("", "-1", "+1", "1.5", "1e3", "１２", " 1", str(maximum + 1), "9" * 500):
                with self.subTest(name=name, value=value), self.assertRaises(ImageMetadataError):
                    parse_image_metadata(f'<msg><img {name}="{value}" /></msg>')

    def test_oversized_private_fields_are_rejected_without_leaking_the_value(self):
        for name, limit in (("aeskey", 1024), ("cdnthumbaeskey", 1024), ("filekey", 1024),
                            ("cdnthumburl", 8192), ("cdnmidimgurl", 8192), ("cdnbigimgurl", 8192)):
            with self.subTest(name=name), self.assertRaises(ImageMetadataError) as raised:
                parse_image_metadata(f'<msg><img {name}="' + "private-" * (limit // 8 + 1) + '" /></msg>')
            self.assertNotIn("private-", str(raised.exception))


class StoreTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="image-metadata-", dir=SCRATCH)
        self.root = Path(self.temporary.name).resolve()
        self.addCleanup(self._cleanup)
        self.store = ImageStore(self.root / "images", ACCOUNT)

    def _cleanup(self):
        self.assertTrue(self.root.is_relative_to(SCRATCH.resolve()))
        self.temporary.cleanup()

    def test_supported_signatures_choose_extensions_and_save_exact_bytes(self):
        examples = ((b"\xff\xd8\xff\xe0", ".jpg"), (PNG, ".png"), (b"GIF87a", ".gif"),
                    (b"GIF89a", ".gif"), (b"RIFF\x10\0\0\0WEBP", ".webp"), (b"BM", ".bmp"))
        for data, extension in examples:
            with self.subTest(extension=extension):
                path = self.store.save(data + b"\0" * 32, sequence=17)
                self.assertTrue(path.is_absolute())
                self.assertTrue(path.is_file())
                self.assertEqual(path.parent, self.store.directory)
                self.assertTrue(path.is_relative_to(self.store.root))
                self.assertTrue(path.name.startswith("image-17-"))
                self.assertEqual(path.suffix, extension)
                self.assertEqual(path.read_bytes(), data + b"\0" * 32)
        self.assertFalse(list(self.store.root.rglob("*.tmp")))
        self.assertFalse(list(self.store.root.rglob("*.reserve")))

    def test_size_limit_accepts_twenty_mib_and_rejects_over_limit(self):
        data = PNG + b"\0" * (MAX_IMAGE_BYTES - len(PNG))
        path = self.store.save(data)
        self.assertEqual(path.stat().st_size, MAX_IMAGE_BYTES)
        with self.assertRaises(ValueError):
            self.store.save(data + b"\0")
        self.assertEqual(list(self.store.directory.iterdir()), [path])

    def test_encrypted_or_disguised_non_images_are_not_saved(self):
        invalid = (b"", None, "image.png", bytearray(PNG), b"<msg><img /></msg>",
                   b"encrypted-wechat-dat-content", b"RIFF\x10\0\0\0WAVE", b"RIFFWEBP")
        for data in invalid:
            with self.subTest(data_type=type(data).__name__), self.assertRaises(ValueError):
                self.store.save(data)
        self.assertFalse(self.store.root.exists())

    def test_account_isolation_includes_case_and_reserved_or_dot_ids(self):
        others = [ImageStore(self.store.root, account) for account in (ACCOUNT.upper(), "..", "CON", "wxid_other")]
        paths = [self.store.save(PNG)] + [store.save(PNG) for store in others]
        self.assertEqual(len({path.parent for path in paths}), len(paths))
        for path in paths:
            self.assertEqual(path.parent.parent, self.store.root)
            self.assertTrue(path.name.endswith(".png"))

    def test_invalid_accounts_and_roots_are_rejected(self):
        for account in (None, "", "../outside", "C:\\outside", "filehelper", "123@chatroom", "gh_public"):
            with self.subTest(account=account), self.assertRaises(ValueError):
                ImageStore(self.root, account)
        for root in (None, b"images", "", " ", "bad\0path"):
            with self.subTest(root=root), self.assertRaises(ValueError):
                ImageStore(root, ACCOUNT)

    def test_sequence_must_be_bounded_integer_and_cannot_control_paths(self):
        for sequence in ("../outside", "7", True, 1.5, -1, 1 << 64):
            with self.subTest(sequence=sequence), self.assertRaises(ValueError):
                self.store.save(PNG, sequence=sequence)
        self.assertFalse(self.store.root.exists())
        self.assertTrue(self.store.save(PNG, sequence=0).exists())
        self.assertTrue(self.store.save(PNG, sequence=(1 << 64) - 1).exists())

    def test_parallel_same_sequence_never_overwrites_and_keeps_payloads(self):
        stores = [ImageStore(self.store.root, ACCOUNT) for _ in range(4)]
        payloads = [PNG + index.to_bytes(4, "little") for index in range(48)]
        def save(index):
            return stores[index % len(stores)].save(payloads[index], sequence=8)
        with ThreadPoolExecutor(max_workers=8) as pool:
            paths = list(pool.map(save, range(len(payloads))))
        self.assertEqual(len(set(paths)), len(payloads))
        self.assertEqual([path.read_bytes() for path in paths], payloads)
        self.assertEqual(set(self.store.directory.iterdir()), set(paths))

    def test_filename_collision_retries_and_keeps_previous_file(self):
        with patch("wxbot_framework.image_metadata.secrets.token_hex", return_value="a" * 32):
            first = self.store.save(PNG, sequence=2)
        with patch("wxbot_framework.image_metadata.secrets.token_hex", side_effect=["a" * 32, "b" * 32]):
            second = self.store.save(PNG + b"second", sequence=2)
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), PNG)
        self.assertEqual(second.read_bytes(), PNG + b"second")
        self.assertEqual(set(self.store.directory.iterdir()), {first, second})

    def test_exhausted_collisions_leave_no_partial_files(self):
        with patch("wxbot_framework.image_metadata.secrets.token_hex", return_value="a" * 32):
            first = self.store.save(PNG)
            with self.assertRaises(FileExistsError):
                self.store.save(PNG + b"another")
        self.assertEqual(first.read_bytes(), PNG)
        self.assertEqual(list(self.store.directory.iterdir()), [first])

    def test_atomic_replace_failure_removes_temporary_and_reservation(self):
        previous = self.store.save(PNG)
        with patch("wxbot_framework.image_metadata.os.replace", side_effect=OSError("simulated write failure")):
            with self.assertRaises(OSError):
                self.store.save(PNG + b"new")
        self.assertEqual(previous.read_bytes(), PNG)
        self.assertEqual(list(self.store.directory.iterdir()), [previous])

    def test_flush_failure_removes_half_written_file(self):
        with patch("wxbot_framework.image_metadata.os.fsync", side_effect=OSError("simulated disk failure")):
            with self.assertRaises(OSError):
                self.store.save(PNG)
        self.assertEqual(list(self.store.directory.iterdir()), [])

    def test_file_wrapper_failure_closes_descriptor_and_removes_temporary_file(self):
        with patch("wxbot_framework.image_metadata.os.fdopen", side_effect=OSError("simulated open failure")):
            with self.assertRaises(OSError):
                self.store.save(PNG)
        self.assertEqual(list(self.store.directory.iterdir()), [])

    def test_official_path_appears_only_when_complete_bytes_are_committed(self):
        import os
        original_replace = os.replace
        observed = []
        def replace(source, destination):
            self.assertFalse(destination.exists())
            self.assertEqual(source.read_bytes(), PNG)
            original_replace(source, destination)
            observed.append(destination)
        with patch("wxbot_framework.image_metadata.os.replace", side_effect=replace):
            saved = self.store.save(PNG)
        self.assertEqual(observed, [saved])
        self.assertEqual(saved.read_bytes(), PNG)

    def test_account_directory_resolving_elsewhere_cannot_redirect_writes(self):
        outside = self.root / "outside"
        original_resolve = Path.resolve
        def redirected_resolve(path, *args, **kwargs):
            if path == self.store.directory:
                return outside
            return original_resolve(path, *args, **kwargs)
        # Windows 创建真实链接可能需要管理员权限；直接模拟解析后的目标，
        # 验证阻断发生在临时文件和正式图片创建之前。
        with patch.object(Path, "resolve", redirected_resolve):
            with self.assertRaises(ValueError):
                self.store.save(PNG)
        self.assertFalse(outside.exists())
        self.assertEqual(list(self.store.directory.iterdir()), [])

    def test_invalid_write_directory_leaves_existing_file_unchanged(self):
        self.store.root.mkdir()
        self.store.directory.write_bytes(b"not-a-directory")
        with self.assertRaises(OSError):
            self.store.save(PNG)
        self.assertEqual(self.store.directory.read_bytes(), b"not-a-directory")


if __name__ == "__main__":
    unittest.main()
