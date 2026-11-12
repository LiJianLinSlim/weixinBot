"""更新后必须明确报版本不兼容，不向未知微信加载桥接。"""
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'native'))
import text_service
from wxbot_framework.native_profiles import UnsupportedWeChatVersion
from wxbot_framework.startup import scan_logged_accounts


class UpdatedWeChatTests(unittest.TestCase):
    def test_unknown_build_reports_version_instead_of_waiting_for_login(self):
        with patch.object(text_service, 'wechat_candidates', return_value=[123]), \
             patch.object(text_service, 'inspect_account', return_value=None), \
             patch.object(text_service, 'unsupported_build_reason', return_value='检测到微信 4.2.0.50，尚未适配'), \
             patch.object(text_service, 'run_text') as bridge:
            with self.assertRaisesRegex(UnsupportedWeChatVersion, '4.2.0.50'):
                text_service.find_account_process('wxid_test')
            bridge.assert_not_called()

    def test_supported_bound_account_wins_over_other_unknown_wechat(self):
        with patch.object(text_service, 'wechat_candidates', return_value=[123, 456]), \
             patch.object(text_service, 'inspect_account', side_effect=['wxid_test', None]), \
             patch.object(text_service, 'unsupported_build_reason') as diagnostic:
            self.assertEqual(text_service.find_account_process('wxid_test'), 123)
            diagnostic.assert_not_called()

    def test_known_but_logged_out_still_waits_for_login(self):
        with patch.object(text_service, 'wechat_candidates', return_value=[123]), \
             patch.object(text_service, 'inspect_account', return_value=None), \
             patch.object(text_service, 'unsupported_build_reason', return_value=None):
            with self.assertRaisesRegex(RuntimeError, '等待绑定'):
                text_service.find_account_process('wxid_test')

    def test_discovery_does_not_fabricate_account_for_unknown_build(self):
        with patch.object(text_service, 'wechat_candidates', return_value=[123]), \
             patch.object(text_service, 'inspect_profile', return_value=None), \
             patch.object(text_service, 'unsupported_build_reason', return_value='检测到微信 4.2.0.50，尚未适配'):
            with self.assertRaisesRegex(UnsupportedWeChatVersion, '4.2.0.50'):
                scan_logged_accounts(ROOT)

    def test_other_unknown_build_does_not_hide_valid_account(self):
        account = {'wxid': 'wxid_test', 'name': '测试'}
        with patch.object(text_service, 'wechat_candidates', return_value=[123, 456]), \
             patch.object(text_service, 'inspect_profile', side_effect=[account, None]), \
             patch.object(text_service, 'unsupported_build_reason', return_value='尚未适配'):
            self.assertEqual(scan_logged_accounts(ROOT), [{'pid': 123, **account}])


if __name__ == '__main__':
    unittest.main()
