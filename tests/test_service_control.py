"""桌面服务控制：正常退出入口的认证和失败时不重复启动。"""
import http.client
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import service_control
from wxbot_framework.panel_http import PanelServer


class ShutdownEndpointTests(unittest.TestCase):
    def setUp(self):
        self.hub = SimpleNamespace(root=Path(__file__).resolve().parents[1],
                                   robot=Mock(), stop_event=threading.Event())
        self.server = PanelServer(self.hub, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method='POST', token=True, origin=None):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['X-Helper-Token'] = self.server.token
        if origin:
            headers['Origin'] = origin
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=3)
        try:
            connection.request(method, '/api/service-stop', body='{}' if method == 'POST' else None, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_shutdown_is_global_and_only_requests_normal_lifecycle_cleanup(self):
        status, result = self.call()
        self.assertEqual(status, 200)
        self.assertTrue(result['ok'])
        self.assertGreater(result['pid'], 0)
        self.assertTrue(self.hub.stop_event.wait(1))
        self.hub.robot.assert_not_called()

    def test_missing_token_or_foreign_origin_cannot_stop_service(self):
        self.assertEqual(self.call(token=False)[0], 403)
        self.assertEqual(self.call(origin='https://foreign.example')[0], 403)
        self.assertFalse(self.hub.stop_event.is_set())

    def test_get_request_cannot_stop_service(self):
        self.assertEqual(self.call(method='GET')[0], 404)
        self.assertFalse(self.hub.stop_event.is_set())


class ServiceControlTests(unittest.TestCase):
    def test_starting_running_service_does_not_launch_duplicate_or_restart_bot(self):
        with patch.object(service_control, 'panel_listening', return_value=True), \
             patch.object(service_control, 'request', return_value={'state': 'running', 'connected': True}) as request, \
             patch.object(service_control.subprocess, 'Popen') as spawn:
            service_control.start_service()
        spawn.assert_not_called()
        self.assertTrue(all(call.args == ('state',) for call in request.call_args_list))

    def test_failed_shutdown_does_not_restart_or_terminate_any_process(self):
        with patch.object(service_control, 'stop_service', side_effect=RuntimeError('still cleaning')), \
             patch.object(service_control, 'start_service') as start:
            with self.assertRaisesRegex(RuntimeError, 'still cleaning'):
                service_control.perform('restart')
        start.assert_not_called()

    def test_invalid_shutdown_confirmation_never_waits_for_untrusted_pid(self):
        with patch.object(service_control, 'panel_listening', return_value=True), \
             patch.object(service_control, 'request', return_value={'ok': True, 'pid': -1}), \
             patch.object(service_control, 'wait_for_process_exit') as wait:
            with self.assertRaises(RuntimeError):
                service_control.stop_service()
        wait.assert_not_called()


if __name__ == '__main__':
    unittest.main()
