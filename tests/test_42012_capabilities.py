"""新版只开放已完成验证的桥接能力。"""
import unittest
from types import SimpleNamespace
from wxbot_framework.backend import NativeTextBackend, TEXT_42012_CAPABILITIES, TEXT_42050_CAPABILITIES

class Capability42050Tests(unittest.TestCase):
    def test_first_stage_exposes_text_only(self):
        backend = object.__new__(NativeTextBackend)
        backend.raw = SimpleNamespace(wechat_version="4.2.0.50")
        self.assertEqual(backend.capabilities, TEXT_42050_CAPABILITIES)
        self.assertEqual(TEXT_42050_CAPABILITIES, frozenset({"text.receive", "text.send", "friend.text.receive", "friend.text.send", "group.text.receive", "group.text.send", "group.text.mention"}))

class Capability42012Tests(unittest.TestCase):
    def test_unverified_features_are_not_advertised(self):
        backend = object.__new__(NativeTextBackend)
        backend.raw = SimpleNamespace(wechat_version='4.2.0.12')
        self.assertEqual(backend.capabilities, TEXT_42012_CAPABILITIES)
        for feature in ('text.send', 'text.receive', 'image.send', 'image.receive',
                        'file.send', 'file.receive', 'voice.send', 'voice.receive',
                        'message.recall', 'group.members.read', 'group.text.mention', 'group.events',
                        'friend.request.receive', 'friend.request.accept'):
            self.assertIn(feature, backend.capabilities)
        for feature in ('emoticon.send', 'contact.read'):
            self.assertNotIn(feature, backend.capabilities)
