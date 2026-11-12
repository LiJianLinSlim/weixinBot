"""4.2.0.50 text-only boundaries; no live process I/O."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "native"))
from text_service import NativeTextService
from filehelper_service import ReceivedMediaMessageV6

SELF, FRIEND, ROOM = "wxid_self", "wxid_friend", "123456789@chatroom"

class Text42050Tests(unittest.TestCase):
    def setUp(self):
        self.service = NativeTextService()
        self.service.wechat_version = "4.2.0.50"
        self.service.router.set_account(SELF)
        self.service._channel = Mock()
        self.addCleanup(self.service.stop)

    def message(self, sender=FRIEND, body=b"hello", kind=1):
        msg = ReceivedMediaMessageV6()
        msg.type = kind
        msg.sender = sender.encode()
        msg.receiver = SELF.encode()
        msg.text = body
        return msg

    def test_private_text_without_cache_lookup(self):
        with patch("text_service.read_contact_name", side_effect=AssertionError("unverified cache")):
            data = self.service._map_message(self.message())
        self.assertEqual(data["chat_id"], FRIEND)
        self.assertEqual(data["msg"], "hello")
        self.assertFalse(data["is_group"] if "is_group" in data else bool(data["room_wxid"]))

    def test_group_text_keeps_real_incoming_mention(self):
        msg = self.message(ROOM, (FRIEND + ":\n@bot hello").encode())
        msg.mention_source = ("<msgsource><atuserlist>" + SELF + "</atuserlist></msgsource>").encode()
        data = self.service._map_message(msg)
        self.assertEqual(data["sender_id"], FRIEND)
        self.assertEqual(data["chat_id"], ROOM)
        self.assertTrue(data["is_at_self"])

    def test_nontext_never_enters_media_or_cache_paths(self):
        for kind in (3, 34, 37, 43, 47, 49, 62, 10000, 10002):
            self.assertIsNone(self.service._map_message(self.message(kind=kind)))
        self.service._channel.read_memory.assert_not_called()

    def test_unsupported_cache_and_mentions_fail_before_memory_or_send(self):
        self.assertIsNone(self.service.group_members(ROOM))
        with self.assertRaisesRegex(RuntimeError, "尚未适配"):
            self.service.contacts()
        self.service._send_text = Mock(return_value=True)
        self.assertTrue(self.service.send_at_text(ROOM, "hello", [FRIEND]))
        self.service._send_text.assert_called_once_with(ROOM, "hello", mentions=(FRIEND,))
        self.service._channel.read_memory.assert_not_called()

if __name__ == "__main__":
    unittest.main()
