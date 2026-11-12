"""定时任务的持久化、账号隔离和不补发行为。"""
import tempfile
import json
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, Mock
from wxbot_framework.llm import LLMError

from wxbot_framework.panel_common import PanelError
from wxbot_framework.scheduled_tasks import ScheduledTasks, next_time
from wxbot_framework.scheduled_actions import BUILTIN, actions_from_manifest
from wxbot_framework.web_panel import atomic_json, read_json


class Store:
    account = 'wxid_test'
    def load(self): return {'wechat_account': self.account}


class Controller:
    def __init__(self, path):
        self.workspace, self.store, self.sent = Path(path), Store(), []
        self.connected = True
    def send_connection(self):
        if not self.connected: raise PanelError('未连接', 409)
        return {'session_id': 'session'}
    def send_text(self, data, *, valid=None):
        if valid is not None and not valid(): raise PanelError('已取消', 409)
        self.sent.append(data)
        return {'status': 'sent'}


class ScheduledTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.controller = Controller(self.temp.name)
        self.scheduler = ScheduledTasks(self.controller, atomic_json, read_json)
        self.addCleanup(self.scheduler.close)
        self.start_patch = patch.object(self.scheduler, 'start')
        self.start_patch.start()
        self.addCleanup(self.start_patch.stop)

    def create(self, **values):
        data = dict(name='提醒', chat_id='filehelper', content='你好', kind='interval', interval_seconds=300)
        data.update(values)
        with patch('wxbot_framework.scheduled_tasks.time.time', return_value=1000):
            return self.scheduler.save(data)['task']

    def test_interval_persist_and_send_once(self):
        task = self.create()
        self.assertEqual(task['next_run'], 1300)
        self.scheduler.tick(1299)
        self.scheduler.tick(1300)
        self.scheduler.tick(1300)
        self.assertEqual(len(self.controller.sent), 1)
        self.assertEqual(self.controller.sent[0]['content'], '你好')
        row = self.scheduler.list()['tasks'][0]
        self.assertEqual((row['last_status'], row['next_run']), ('已发送', 1600))
        self.assertEqual(read_json(self.scheduler.path)['tasks'][0]['next_run'], 1600)

    def test_late_or_disconnected_does_not_catch_up(self):
        self.create()
        self.scheduler.tick(1400)
        self.assertEqual(self.scheduler.list()['tasks'][0]['last_status'], '已跳过（错过执行时间）')
        self.controller.connected = False
        self.scheduler.tick(1700)
        self.assertIn('未发送', self.scheduler.list()['tasks'][0]['last_status'])
        self.controller.connected = True
        self.scheduler.tick(1701)
        self.assertEqual(self.controller.sent, [])

    def test_account_isolation_disable_and_delete(self):
        task = self.create()
        self.controller.store.account = 'wxid_other'
        self.scheduler.tick(1300)
        self.assertEqual(self.controller.sent, [])
        with self.assertRaises(PanelError): self.scheduler.delete({'id': task['id']})
        self.controller.store.account = task['account']
        self.scheduler.save({**task, 'enabled': False})
        self.scheduler.tick(5000)
        self.assertEqual(self.controller.sent, [])
        self.scheduler.delete({'id': task['id']})
        self.assertEqual(self.scheduler.list()['tasks'], [])

    def test_daily_time_and_validation(self):
        at = datetime(2026, 10, 30, 8, 30).timestamp()
        self.assertEqual(datetime.fromtimestamp(next_time({'kind':'daily', 'daily_time':'09:00'}, at)).hour, 9)
        self.assertEqual(datetime.fromtimestamp(next_time({'kind':'daily', 'daily_time':'08:30'}, at)).day, 31)
        with self.assertRaises(PanelError): self.create(interval_seconds=1)
        with self.assertRaises(PanelError): self.create(kind='daily', daily_time='25:00')
        with self.assertRaises(PanelError): self.create(chat_id='bad target')

    def test_stale_claim_never_sends(self):
        task = self.create()
        task['claim'] = 'stale'
        self.assertEqual(self.scheduler._deliver(task), '已取消')
        self.assertFalse(self.controller.sent)

    def test_restart_skips_missed_task_even_if_recent(self):
        self.create()
        scheduler = ScheduledTasks(self.controller, atomic_json, read_json)
        with patch('wxbot_framework.scheduled_tasks.time.time', return_value=1301), patch.object(scheduler, '_loop'):
            scheduler.start()
            scheduler.thread.join()
        row = scheduler.list()['tasks'][0]
        self.assertEqual(row['last_status'], '已跳过（停机期间错过）')
        self.assertEqual(row['next_run'], 1601)
        scheduler.close()

    def test_plugin_actions_validate_and_dispatch_without_breaking_old_tasks(self):
        action = actions_from_manifest('example', {'scheduled_actions':[{'id':'work', 'label':'做事',
            'method':'run_task', 'fields':[{'name':'count', 'label':'次数', 'type':'integer', 'required':True}]}]})[0]
        self.controller.scheduled_action_types = lambda: {'actions':[BUILTIN, action]}
        calls = []
        self.controller.execute_scheduled_plugin = lambda aid, params, session, account, valid: calls.append((aid, params, session, account, valid()))
        with self.assertRaises(PanelError): self.create(action_id=action['id'], params={'count':'2'})
        task = self.create(action_id=action['id'], params={'count':2})
        self.scheduler.tick(1300)
        self.assertEqual(calls, [(action['id'], {'count':2}, 'session', 'wxid_test', True)])
        self.assertEqual(self.scheduler.list()['tasks'][0]['last_status'], '执行完成')
        self.controller.scheduled_action_types = lambda: {'actions':[BUILTIN]}
        self.scheduler.toggle({'id':task['id'], 'enabled':False})
        self.assertFalse(self.scheduler.list()['tasks'][0]['enabled'])

    def test_running_task_never_overlaps_itself(self):
        task = self.create()
        self.scheduler.inflight.add(task['id'])
        self.scheduler.tick(1300)
        self.assertFalse(self.controller.sent)
        self.assertEqual(self.scheduler.list()['tasks'][0]['next_run'], 1600)

    def test_restart_preserves_uncertain_inflight_result(self):
        self.create()
        rows = self.scheduler.list()['tasks']
        rows[0].update(claim='unfinished', last_time=1300, last_status='执行中', next_run=1600)
        self.scheduler._write(rows)
        scheduler = ScheduledTasks(self.controller, atomic_json, read_json)
        with patch('wxbot_framework.scheduled_tasks.time.time', return_value=1700), patch.object(scheduler, '_loop'):
            scheduler.start()
            scheduler.thread.join()
        row = scheduler.list()['tasks'][0]
        self.assertIn('待确认', row['last_status'])
        self.assertEqual(row['last_time'], 1300)
        self.assertEqual(row['next_run'], 2000)
        scheduler.close()

    def test_description_persists_and_old_tasks_remain_fixed(self):
        task = self.create(params={'chat_id': 'filehelper', 'content': '该喝水了', 'description': '只需要提醒'})
        self.assertEqual(read_json(self.scheduler.path)['tasks'][0]['params']['description'], '只需要提醒')
        with patch('wxbot_framework.scheduled_tasks.LLMClient.from_file') as model:
            self.scheduler.tick(1300)
        model.assert_not_called()
        self.assertEqual(self.controller.sent[0]['content'], '该喝水了')
        self.assertNotIn('description', self.controller.sent[0])
        with self.assertRaises(PanelError):
            self.create(params={'chat_id': 'filehelper', 'content': '提醒', 'description': '字' * 2001})

    def test_custom_description_generates_every_run_and_remembers_only_sent_outputs(self):
        task = self.create(params={'chat_id': 'filehelper', 'content': '该喝水了', 'description': '提醒同时说一句喝水好处'})
        client = Mock(response_model='backup')
        client.complete_text.side_effect = ['该喝水啦，给自己补点水分。', '喝口水歇一歇，别让忙碌打断补水。']
        with patch('wxbot_framework.scheduled_tasks.LLMClient.from_file', return_value=client):
            self.scheduler.tick(1300)
            self.scheduler.tick(1600)
        self.assertEqual(len(self.controller.sent), 2)
        self.assertNotEqual(self.controller.sent[0]['content'], self.controller.sent[1]['content'])
        import json
        second = json.loads(client.complete_text.call_args_list[1].args[1])
        self.assertEqual(second['previous'], [self.controller.sent[0]['content']])
        self.assertEqual(second['supplement'], '提醒同时说一句喝水好处')
        self.assertEqual(self.scheduler.list()['tasks'][0]['params']['description'], '提醒同时说一句喝水好处')

    def test_at_all_persists_for_fixed_and_generated_reminders(self):
        for description in ('只需要提醒', '提醒时说一句鼓励的话'):
            with self.subTest(description=description):
                self.controller.sent.clear()
                task = self.create(params={'chat_id': '123@chatroom', 'content': '活动一下',
                                           'description': description, 'at_all': True})
                self.assertTrue(read_json(self.scheduler.path)['tasks'][-1]['params']['at_all'])
                with patch('wxbot_framework.scheduled_tasks.LLMClient.from_file',
                           return_value=Mock(complete_text=Mock(return_value='站起来走走，给自己打打气。'))):
                    self.scheduler.tick(1300)
                self.assertEqual(len(self.controller.sent), 1)
                self.assertIs(self.controller.sent[0]['at_all'], True)
                self.assertEqual(self.controller.sent[0]['chat_id'], '123@chatroom')
                self.scheduler.delete({'id': task['id']})

    def test_sent_history_survives_scheduler_restart_and_prevents_repetition(self):
        task = self.create(params={'chat_id': 'filehelper', 'content': '喝水了', 'description': '说一句喝水的好处'})
        old = '到点啦，记得喝口水，适量补水能帮助身体维持正常代谢。'
        fresh = '给自己留个补水的小间隙吧，嗓子干时喝些水会更舒服。'
        client = Mock(timeout=60, response_model='model')
        client.complete_text.return_value = old
        with patch('wxbot_framework.scheduled_tasks.LLMClient.from_file', return_value=client):
            self.scheduler.tick(1300)
        self.scheduler.close()
        restarted = ScheduledTasks(self.controller, atomic_json, read_json)
        self.addCleanup(restarted.close)
        client.complete_text.side_effect = [old, fresh]
        with patch('wxbot_framework.scheduled_tasks.LLMClient.from_file', return_value=client):
            restarted.tick(1600)
        self.assertEqual([row['content'] for row in self.controller.sent], [old, fresh])
        self.assertEqual(restarted._previous(task), (old, fresh))
        self.assertEqual(json.loads(client.complete_text.call_args.args[1])['previous'], [old])

    def test_repeated_outputs_never_reach_send_and_do_not_replace_sent_history(self):
        task = self.create(params={'chat_id': 'filehelper', 'content': '喝水了', 'description': '说一句喝水的好处'})
        old = '到点啦，记得喝口水，适量补水能帮助身体维持正常代谢。'
        client = Mock(timeout=60, response_model='model')
        client.complete_text.return_value = old
        with patch('wxbot_framework.scheduled_tasks.LLMClient.from_file', return_value=client):
            self.scheduler.tick(1300)
            self.scheduler.tick(1600)
        self.assertEqual(len(self.controller.sent), 1)
        self.assertEqual(client.complete_text.call_count, 4)
        self.assertIn('重复文案', self.scheduler.list()['tasks'][0]['last_status'])
        self.assertEqual(self.scheduler._previous(task), (old,))

    def test_failed_send_does_not_enter_durable_history(self):
        task = self.create(params={'chat_id': 'filehelper', 'content': '喝水了', 'description': '说句鼓励'})
        client = Mock(timeout=60, response_model='model')
        client.complete_text.return_value = '歇一歇，喝口水，再继续手头的事情。'
        with patch('wxbot_framework.scheduled_tasks.LLMClient.from_file', return_value=client), \
                patch.object(self.controller, 'send_text', return_value={'status': 'failed'}) as send:
            self.scheduler.tick(1300)
        self.assertEqual(send.call_count, 1)
        self.assertFalse(self.scheduler.history_path.exists())
        self.assertEqual(self.scheduler._previous(task), ())

    def test_durable_history_is_bounded_and_isolates_task_account_target_and_configuration(self):
        task = self.create(params={'chat_id': 'filehelper', 'content': '喝水了', 'description': '说句鼓励'})
        for i in range(15):
            self.assertTrue(self.scheduler._remember(task, f'第{i}条合成提醒'))
        self.assertEqual(self.scheduler._previous(task), tuple(f'第{i}条合成提醒' for i in range(5, 15)))
        for changed in ({**task, 'account': 'another'}, {**task, 'id': 'another'},
                        *({**task, 'params': {**task['params'], key: value}} for key, value in
                          [('chat_id', 'another@chatroom'), ('content', '活动一下'), ('description', '别的描述')])):
            with self.subTest(changed=changed):
                self.assertEqual(self.scheduler._previous(changed), ())

    def test_history_write_failure_after_send_never_reports_unsent_or_retries(self):
        self.create(params={'chat_id': 'filehelper', 'content': '喝水了', 'description': '说句鼓励'})
        client = Mock(timeout=60, response_model='model')
        client.complete_text.return_value = '歇一歇，喝口水，再继续手头的事情。'
        original_write = self.scheduler.write_json
        def write(path, data):
            if path == self.scheduler.history_path:
                raise OSError('synthetic disk full')
            original_write(path, data)
        with patch('wxbot_framework.scheduled_tasks.LLMClient.from_file', return_value=client), \
                patch.object(self.scheduler, 'write_json', side_effect=write):
            self.scheduler.tick(1300)
        self.assertEqual(len(self.controller.sent), 1)
        self.assertEqual(self.scheduler.list()['tasks'][0]['last_status'], '已发送（近期文案保存失败）')

    def test_old_tasks_do_not_start_notifying_everyone(self):
        self.create()
        self.scheduler.tick(1300)
        self.assertIs(self.controller.sent[0]['at_all'], False)

    def test_at_all_requires_boolean_and_group_target(self):
        for params in ({'chat_id': 'filehelper', 'at_all': True},
                       {'chat_id': '123@chatroom', 'at_all': 'true'}):
            with self.subTest(params=params), self.assertRaises(PanelError):
                self.create(params={'content': '提醒', **params})

    def test_at_all_survives_generation_failure_and_send_is_not_retried(self):
        self.create(params={'chat_id': '123@chatroom', 'content': '提醒', 'description': '说句鼓励', 'at_all': True})
        with patch('wxbot_framework.scheduled_tasks.LLMClient.from_file', side_effect=LLMError('unavailable')):
            self.scheduler.tick(1300)
        self.assertEqual(len(self.controller.sent), 1)
        self.assertEqual(self.controller.sent[0]['content'], '提醒')
        self.assertTrue(self.controller.sent[0]['at_all'])

    def test_at_all_downgrades_to_plain_text_on_text_only_backend(self):
        self.controller.app = type('App', (), {'backend': type('Backend', (), {
            'capabilities': frozenset({'text.send', 'group.text.send'})})()})()
        self.create(params={'chat_id': '123@chatroom', 'content': '喝水', 'description': '', 'at_all': True})
        self.scheduler.tick(now=1301)
        self.scheduler._finish_async = self.scheduler._finish_async
        self.assertEqual(self.controller.sent[0]['at_all'], False)
        self.assertIn('降级为普通群消息', self.scheduler._read()[0]['last_status'])

    def test_at_all_permission_failure_has_specific_task_status(self):
        self.create(params={'chat_id': '123@chatroom', 'content': '提醒', 'at_all': True})
        self.controller.send_text = Mock(return_value={'status': 'failed', 'error': '@全员需要机器人是群主或管理员'})
        self.scheduler.tick(1300)
        self.assertIn('群主或管理员', self.scheduler.list()['tasks'][0]['last_status'])
        self.controller.send_text.assert_called_once()

    def test_generation_failure_sends_original_once_but_does_not_retry_send_failure(self):
        self.create(params={'chat_id': 'filehelper', 'content': '该喝水了', 'description': '说好处'})
        with patch('wxbot_framework.scheduled_tasks.LLMClient.from_file', side_effect=LLMError('服务暂不可用')):
            self.scheduler.tick(1300)
        self.assertEqual(len(self.controller.sent), 1)
        self.assertEqual(self.controller.sent[0]['content'], '该喝水了')
        self.assertIn('使用原文', self.scheduler.list()['tasks'][0]['last_status'])
        self.controller.sent.clear()
        client = Mock(response_model='model')
        client.complete_text.return_value = '喝口水，歇一下。'
        with patch('wxbot_framework.scheduled_tasks.LLMClient.from_file', return_value=client), \
                patch.object(self.controller, 'send_text', side_effect=RuntimeError('unknown result')) as send:
            self.scheduler.tick(1600)
        self.assertEqual(send.call_count, 1)
        self.assertFalse(self.controller.sent)

    def test_pause_delete_edit_and_account_switch_during_generation_cancel_delivery(self):
        for change in ('pause', 'delete', 'edit', 'account'):
            with self.subTest(change=change):
                self.controller.store.account = 'wxid_test'
                self.scheduler._write([])
                self.controller.sent.clear()
                task = self.create(params={'chat_id': 'filehelper', 'content': '提醒', 'description': '说好处'})
                def generate(*args, **kwargs):
                    if change == 'pause': self.scheduler.toggle({'id': task['id'], 'enabled': False})
                    elif change == 'delete': self.scheduler.delete({'id': task['id']})
                    elif change == 'edit': self.scheduler.save({**task, 'params': {**task['params'], 'description': '改了'}})
                    else: self.controller.store.account = 'wxid_other'
                    return '旧的生成提醒'
                client = Mock(response_model='model')
                client.complete_text.side_effect = generate
                with patch('wxbot_framework.scheduled_tasks.LLMClient.from_file', return_value=client):
                    self.scheduler.tick(1300)
                self.assertFalse(self.controller.sent)

    def test_disallowed_chat_does_not_call_model(self):
        self.create(params={'chat_id': 'filehelper', 'content': '提醒', 'description': '说好处'})
        self.controller.store.load = lambda: {'wechat_account': 'wxid_test', 'chat_allowlist': ['other']}
        with patch('wxbot_framework.scheduled_tasks.LLMClient.from_file') as model:
            self.scheduler.tick(1300)
        model.assert_not_called()
        self.assertFalse(self.controller.sent)

    def test_content_edit_preserves_next_time_but_schedule_change_recalculates(self):
        task = self.create()
        with patch('wxbot_framework.scheduled_tasks.time.time', return_value=1100):
            edited = self.scheduler.save({**task, 'name': '新名称', 'params': {
                'chat_id': 'filehelper', 'content': '新提醒', 'description': '只需要提醒'}})['task']
            self.assertEqual(edited['next_run'], 1300)
            changed = self.scheduler.save({**edited, 'interval_seconds': 600})['task']
            self.assertEqual(changed['next_run'], 1700)

    def test_interval_can_select_first_or_next_time(self):
        selected = datetime(2026, 1, 1, 9).timestamp()
        at = datetime.fromtimestamp(selected).isoformat(timespec='seconds')
        task = self.create(first_run=at)
        self.assertEqual(task['next_run'], selected)
        self.scheduler.tick(selected - 300)
        self.assertFalse(self.controller.sent)
        self.scheduler.tick(selected)
        self.assertEqual(len(self.controller.sent), 1)
        self.assertEqual(self.scheduler.list()['tasks'][0]['next_run'], selected + 300)
        for value in ('not-a-time', datetime.fromtimestamp(900).isoformat(timespec='seconds')):
            with self.subTest(value=value), self.assertRaises(PanelError):
                self.create(first_run=value)

    def test_once_sends_only_once_and_requires_future_local_datetime(self):
        selected = datetime(2026, 1, 1, 9).timestamp()
        at = datetime.fromtimestamp(selected).isoformat(timespec='seconds')
        task = self.create(kind='once', send_at=at)
        self.scheduler.tick(selected - 1)
        self.assertFalse(self.controller.sent)
        self.scheduler.tick(selected)
        self.scheduler.tick(selected + 1)
        row = self.scheduler.list()['tasks'][0]
        self.assertEqual(len(self.controller.sent), 1)
        self.assertEqual((row['enabled'], row['next_run'], row['last_status']), (False, 0, '已发送'))
        with patch('wxbot_framework.scheduled_tasks.time.time', return_value=selected + 1), self.assertRaises(PanelError):
            self.scheduler.toggle({'id': task['id'], 'enabled': True})
        for value in ('2026-11-03T12:00+08:00', '', 'broken', datetime.fromtimestamp(999).isoformat(timespec='seconds')):
            with self.subTest(value=value), self.assertRaises(PanelError):
                self.create(kind='once', send_at=value)

    def test_restart_expires_missed_once_task(self):
        selected = datetime(2026, 1, 1, 9).timestamp()
        self.create(kind='once', send_at=datetime.fromtimestamp(selected).isoformat(timespec='seconds'))
        scheduler = ScheduledTasks(self.controller, atomic_json, read_json)
        self.addCleanup(scheduler.close)
        with patch('wxbot_framework.scheduled_tasks.time.time', return_value=selected + 1), patch.object(scheduler, '_loop'):
            scheduler.start()
            scheduler.thread.join()
        row = scheduler.list()['tasks'][0]
        self.assertEqual((row['enabled'], row['next_run']), (False, 0))
        self.assertIn('停机期间错过', row['last_status'])
        self.assertFalse(self.controller.sent)

    def test_manual_run_is_idempotent_and_keeps_schedule_even_when_paused(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                self.scheduler._write([])
                self.controller.sent.clear()
                task = self.create(enabled=enabled)
                request = {'id': task['id'], 'request_id': 'manual-request-0001'}
                with patch('wxbot_framework.scheduled_tasks.threading.Thread') as thread:
                    self.scheduler.run_now(request)
                    pending = thread.call_args.kwargs['args'][0]
                    self.scheduler.run_now(request)
                    self.assertEqual(thread.call_count, 1)
                    with self.assertRaises(PanelError):
                        self.scheduler.run_now({**request, 'request_id': 'manual-request-0002'})
                self.scheduler._finish_async(pending)
                self.scheduler.run_now(request)
                self.assertEqual(len(self.controller.sent), 1)
                row = self.scheduler.list()['tasks'][0]
                self.assertEqual((row['enabled'], row['next_run']), (enabled, task['next_run']))
                self.assertEqual(row['last_status'], '已发送')

    def test_manual_run_checks_account_connection_and_cancels_after_edit(self):
        task = self.create()
        request = {'id': task['id'], 'request_id': 'manual-request-0001'}
        self.controller.store.account = 'another-account'
        with self.assertRaises(PanelError): self.scheduler.run_now(request)
        self.controller.store.account = task['account']
        self.controller.connected = False
        with self.assertRaises(PanelError): self.scheduler.run_now(request)
        self.assertNotIn('claim', self.scheduler.list()['tasks'][0])
        self.controller.connected = True
        with patch('wxbot_framework.scheduled_tasks.threading.Thread') as thread:
            self.scheduler.run_now(request)
            pending = thread.call_args.kwargs['args'][0]
        self.scheduler.save({**task, 'content': 'edited', 'params': {'chat_id': 'filehelper', 'content': 'edited'}})
        self.scheduler._finish_async(pending)
        self.assertFalse(self.controller.sent)


if __name__ == '__main__': unittest.main()
