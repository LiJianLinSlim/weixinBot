from datetime import datetime
from unittest.mock import Mock, patch

import test_scheduled_tasks as scheduled
from wxbot_framework.panel_common import PanelError
from wxbot_framework.scheduled_tasks import ScheduledTasks, in_daily_window, next_time


def ts(clock, day=3):
    return datetime.fromisoformat(f'2026-11-{day:02d}T{clock}').timestamp()


class WindowTests(scheduled.ScheduledTests):
    def window_task(self, **updates):
        return dict(kind='interval', interval_seconds=7200, window_start='09:00', window_end='22:00', **updates)

    def test_daily_grid_has_start_but_excludes_end_and_resets_next_day(self):
        task = self.window_task()
        for current, expected, day in [('08:00', '09:00', 3), ('09:00', '11:00', 3),
                ('10:30', '11:00', 3), ('20:59', '21:00', 3), ('21:00', '09:00', 4),
                ('22:00', '09:00', 4), ('23:59', '09:00', 4)]:
            with self.subTest(current=current):
                self.assertEqual(next_time(task, ts(current)), ts(expected, day))
        self.assertTrue(in_daily_window(task, ts('09:00')))
        self.assertTrue(in_daily_window(task, ts('21:59:59')))
        self.assertFalse(in_daily_window(task, ts('22:00')))
        self.assertFalse(in_daily_window(task, ts('08:59:59')))

    def test_cross_midnight_window_follows_previous_day_anchor(self):
        task = dict(kind='interval', interval_seconds=7200, window_start='22:00', window_end='06:00')
        for current, expected, day in [('01:00', '02:00', 3), ('04:00', '22:00', 3),
                                      ('12:00', '22:00', 3), ('22:00', '00:00', 4)]:
            self.assertEqual(next_time(task, ts(current)), ts(expected, day))
        for clock, active in [('23:00', True), ('00:00', True), ('05:59', True), ('06:00', False)]:
            self.assertEqual(in_daily_window(task, ts(clock)), active)

    def test_interval_longer_than_window_still_runs_once_per_day(self):
        task = dict(kind='interval', interval_seconds=86400, window_start='09:00', window_end='10:00')
        self.assertEqual(next_time(task, ts('09:00')), ts('09:00', 4))

    def test_save_window_and_edit_text_preserves_schedule_and_at_all(self):
        with patch('wxbot_framework.scheduled_tasks.time.time', return_value=ts('08:00')):
            task = self.scheduler.save({'name':'喝水', 'params':{'chat_id':'123@chatroom',
                'content':'喝水', 'description':'只需要提醒', 'at_all':True}, **self.window_task()})['task']
        self.assertEqual(task['next_run'], ts('09:00'))
        with patch('wxbot_framework.scheduled_tasks.time.time', return_value=ts('08:30')):
            edited = self.scheduler.save({**task, 'params':{**task['params'], 'content':'记得喝水'}})['task']
        self.assertEqual(edited['next_run'], ts('09:00'))
        self.assertTrue(edited['params']['at_all'])
        with patch('wxbot_framework.scheduled_tasks.time.time', return_value=ts('10:00')):
            changed = self.scheduler.save({**edited, 'window_start':'10:30'})['task']
        self.assertEqual(changed['next_run'], ts('10:30'))

    def test_invalid_or_partial_windows_and_non_interval_are_rejected(self):
        for values in ({'window_start':'09:00'}, {'window_start':'25:00','window_end':'22:00'},
                       {'window_start':'09:00','window_end':'09:00'},
                       {'window_start':False,'window_end':'22:00'},
                       {'window_start':'09:00','window_end':'22:00','kind':'daily','daily_time':'09:00'}):
            with self.subTest(values=values), self.assertRaises(PanelError):
                self.create(**values)

    def test_explicit_first_time_must_be_inside_window(self):
        values={'name':'提醒','chat_id':'filehelper','content':'你好',**self.window_task()}
        with patch('wxbot_framework.scheduled_tasks.time.time', return_value=ts('08:00')):
            with self.assertRaises(PanelError):
                self.scheduler.save({**values, 'first_run':'2026-11-03T23:00:00'})
            task=self.scheduler.save({**values,'first_run':'2026-11-03T10:00:00'})['task']
        self.assertEqual(task['next_run'],ts('10:00'))
        # 指定首次时间不改变随后每日按时段起点排列的网格。
        self.assertEqual(next_time(task,ts('10:00')),ts('11:00'))

    def test_tick_and_restart_never_catch_up_at_night(self):
        with patch('wxbot_framework.scheduled_tasks.time.time', return_value=ts('08:00')):
            task=self.scheduler.save({'name':'提醒','chat_id':'filehelper','content':'你好',**self.window_task()})['task']
        with patch('wxbot_framework.scheduled_tasks.time.time', return_value=ts('09:00')):
            self.scheduler.tick(ts('09:00'))
        self.assertEqual(len(self.controller.sent),1)
        self.assertEqual(self.scheduler.list()['tasks'][0]['next_run'],ts('11:00'))
        with patch('wxbot_framework.scheduled_tasks.time.time', return_value=ts('23:00')):
            self.scheduler.tick(ts('23:00'))
        self.assertEqual(len(self.controller.sent),1)
        self.assertEqual(self.scheduler.list()['tasks'][0]['next_run'],ts('09:00',4))
        rows=self.scheduler.list()['tasks']; rows[0]['next_run']=ts('21:00'); self.scheduler._write(rows)
        other=ScheduledTasks(self.controller,self.scheduler.write_json,self.scheduler.read_json)
        with patch('wxbot_framework.scheduled_tasks.time.time',return_value=ts('23:00')),patch.object(other,'_loop'):
            other.start(); other.thread.join()
        self.assertEqual(other.list()['tasks'][0]['next_run'],ts('09:00',4))
        other.close()

    def test_window_ending_during_generation_cancels_before_send(self):
        with patch('wxbot_framework.scheduled_tasks.time.time',return_value=ts('20:59')):
            self.scheduler.save({'name':'提醒','params':{'chat_id':'filehelper','content':'提醒','description':'补充鼓励'},
                                 **self.window_task()})
        now=[ts('21:00')]
        def generate(*args,**kwargs):
            now[0]=ts('22:00'); return '生成的提醒'
        with patch('wxbot_framework.scheduled_tasks.time.time',side_effect=lambda:now[0]), \
             patch('wxbot_framework.scheduled_tasks.LLMClient.from_file',return_value=Mock()), \
             patch('wxbot_framework.scheduled_tasks.generate_reminder',side_effect=generate):
            self.scheduler.tick(ts('21:00'))
        self.assertEqual(self.controller.sent,[])
        self.assertEqual(self.scheduler.list()['tasks'][0]['last_status'],'已取消')

    def test_manual_execution_is_allowed_outside_window_and_does_not_reschedule(self):
        with patch('wxbot_framework.scheduled_tasks.time.time',return_value=ts('08:00')):
            task=self.scheduler.save({'name':'提醒','chat_id':'filehelper','content':'你好',**self.window_task()})['task']
        rows=self.scheduler.list()['tasks'];rows[0]['claim']='manual-test';self.scheduler._write(rows)
        with patch('wxbot_framework.scheduled_tasks.time.time',return_value=ts('23:00')):
            result=self.scheduler._deliver({**task,'claim':'manual-test','manual':True})
        self.assertEqual(result,'已发送')
        self.assertEqual(len(self.controller.sent),1)
        self.assertEqual(self.scheduler.list()['tasks'][0]['next_run'],task['next_run'])
