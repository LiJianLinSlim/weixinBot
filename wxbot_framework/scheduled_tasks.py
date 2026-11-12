"""按机器人工作区保存并执行定时文本任务。"""
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import logging
import math
import re
import secrets
import threading
import time

from .config import account_is_unbound
from .panel_common import PanelError
from .scheduled_actions import BUILTIN, validate_params
from .reminder_text import HISTORY_LIMIT, RepeatedReminderError, needs_generation, generate_reminder
from .llm import LLMClient, LLMError


def in_daily_window(task, now):
    if task['kind'] != 'interval' or not task.get('window_start'):
        return True
    current = datetime.fromtimestamp(now)
    clock = current.strftime('%H:%M:%S')
    start, end = task['window_start'] + ':00', task['window_end'] + ':00'
    return start <= clock < end if start < end else clock >= start or clock < end


def next_time(task, now):
    if task['kind'] == 'once':
        return task['send_at']
    if task['kind'] == 'interval':
        if task.get('window_start'):
            current = datetime.fromtimestamp(now)
            start_hour, start_minute = map(int, task['window_start'].split(':'))
            end_hour, end_minute = map(int, task['window_end'].split(':'))
            # 每天以时段起点重新计时；跨午夜时先检查昨日开始的时段。
            for offset in (-1, 0, 1):
                day = current + timedelta(days=offset)
                start = day.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
                end = day.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
                if end <= start:
                    end += timedelta(days=1)
                step = max(0, math.floor((now - start.timestamp()) / task['interval_seconds']) + 1)
                candidate = start.timestamp() + step * task['interval_seconds']
                if candidate < end.timestamp():
                    return candidate
            raise ValueError('无法计算每天时段的下次执行时间')
        return now + task['interval_seconds']
    hour, minute = map(int, task['daily_time'].split(':'))
    current = datetime.fromtimestamp(now)
    candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate.timestamp() <= now:
        candidate += timedelta(days=1)
    return candidate.timestamp()


class ScheduledTasks:
    def __init__(self, controller, write_json, read_json):
        self.controller = controller
        self.path = controller.workspace / '.runtime' / 'scheduled_tasks.json'
        self.history_path = controller.workspace / '.runtime' / 'reminder_history.json'
        self.write_json, self.read_json = write_json, read_json
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.slots = threading.BoundedSemaphore(8)
        self.inflight = set()

    def _read(self):
        if not self.path.exists():
            return []
        rows = self.read_json(self.path).get('tasks')
        if not isinstance(rows, list):
            raise PanelError('定时任务数据损坏，请检查本地文件', 500)
        return rows

    def _write(self, rows):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.write_json(self.path, {'tasks': rows})

    @staticmethod
    def _history_key(task):
        params = task.get('params', {})
        # 任务、账号、目标及文案配置隔离；改变收件人后不会附带原会话的提醒历史。
        scope = [task['id'], task['account'], params.get('chat_id'),
                 params.get('content'), params.get('description', '')]
        return hashlib.sha256(json.dumps(scope, ensure_ascii=False).encode('utf-8')).hexdigest()

    def _read_history(self):
        if not self.history_path.exists():
            return {}
        try:
            if self.history_path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError('历史文件过大')
            entries = self.read_json(self.history_path).get('entries')
            if not isinstance(entries, dict) or len(entries) > 200:
                raise ValueError('历史数据格式错误')
            for key, texts in entries.items():
                if (not re.fullmatch(r'[a-f0-9]{64}', key) or not isinstance(texts, list) or
                        len(texts) > HISTORY_LIMIT or any(not isinstance(t, str) or not t.strip() or len(t) > 200 for t in texts)):
                    raise ValueError('历史记录格式错误')
            return entries
        except (ValueError, OSError):
            logging.getLogger('ScheduledTasks').warning('近期提醒历史无法读取，本次使用空历史')
            return {}

    def _previous(self, task):
        with self.lock:
            return tuple(self._read_history().get(self._history_key(task), ()))

    def _remember(self, task, body):
        with self.lock:
            try:
                entries = self._read_history()
                key = self._history_key(task)
                texts = entries.pop(key, [])
                entries[key] = [*texts, body][-HISTORY_LIMIT:]
                while len(entries) > 200:
                    entries.pop(next(iter(entries)))
                self.history_path.parent.mkdir(parents=True, exist_ok=True)
                self.write_json(self.history_path, {'entries': entries})
                return True
            except (ValueError, OSError):
                # 消息已经成功发送，历史写入失败不能把状态改成未发送或重试发送。
                logging.getLogger('ScheduledTasks').warning('提醒已发送，但近期文案历史保存失败')
                return False

    def start(self):
        if self.thread is not None or self.stop_event.is_set():
            return
        with self.lock:
            if self.thread is None and not self.stop_event.is_set():
                rows = self._read()
                now = time.time()
                changed = False
                for row in rows:
                    interrupted = bool(row.pop('claim', None))
                    if interrupted:
                        row['last_status'] = '结果待确认，请核对微信'
                        changed = True
                    if row.get('enabled') and row.get('next_run', 0) <= now:
                        if row['kind'] == 'once':
                            row.update(enabled=False, next_run=0)
                        else:
                            row['next_run'] = next_time(row, now)
                        if not interrupted:
                            row.update(last_time=now, last_status='已跳过（停机期间错过）')
                        changed = True
                if changed:
                    self._write(rows)
                self.thread = threading.Thread(target=self._loop, name='ScheduledTasks', daemon=True)
                self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(3)

    def list(self):
        with self.lock:
            return {'tasks': deepcopy(self._read())}

    def save(self, data):
        name = data.get('name')
        if not isinstance(name, str) or not name.strip() or len(name) > 100 or '\n' in name:
            raise PanelError('请输入 1–100 字的任务名称')
        action_id = data.get('action_id', 'builtin.send_text')
        available = self.controller.scheduled_action_types() if hasattr(self.controller, 'scheduled_action_types') else {'actions': [BUILTIN]}
        action = next((item for item in available['actions'] if item['id'] == action_id), None)
        if action is None:
            raise PanelError('定时动作不存在，请刷新任务类型')
        params = data.get('params')
        if params is None and action_id == 'builtin.send_text':
            params = {'chat_id': data.get('chat_id'), 'content': data.get('content')}
        try:
            params = validate_params(action, params)
        except (ValueError, UnicodeEncodeError) as error:
            raise PanelError(str(error)) from None
        kind = data.get('kind')
        interval = data.get('interval_seconds')
        daily = data.get('daily_time')
        if kind == 'interval':
            if type(interval) is not int or not 60 <= interval <= 31536000:
                raise PanelError('间隔应为 1 分钟至 365 天')
            daily = ''
        elif kind == 'daily':
            if not isinstance(daily, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', daily):
                raise PanelError('请选择每天的执行时间')
            interval = 0
        elif kind == 'once':
            interval, daily = 0, ''
        else:
            raise PanelError('请选择执行方式')
        window_start, window_end = data.get('window_start', ''), data.get('window_end', '')
        if not isinstance(window_start, str) or not isinstance(window_end, str):
            raise PanelError('每天生效时段格式无效')
        if window_start or window_end:
            if kind != 'interval':
                raise PanelError('每天生效时段仅适用于按间隔执行')
            if any(not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', t) for t in (window_start, window_end)):
                raise PanelError('请完整填写每天时段的开始和结束时间')
            if window_start == window_end:
                raise PanelError('时段开始和结束不能相同；全天执行请取消时段限制')
        account = self.controller.store.load().get('wechat_account')
        if account_is_unbound(account):
            raise PanelError('请先选择已登录的微信账号', 409)
        enabled = data.get('enabled', True)
        if type(enabled) is not bool:
            raise PanelError('任务状态无效')
        with self.lock:
            rows = self._read()
            identifier = data.get('id')
            existing = next ( (row for row in rows if row['id'] == identifier), None) if identifier else None
            if identifier and (existing is None or existing['account'] != account):
                raise PanelError('任务不存在或不属于当前账号', 404)
            if not existing and len(rows) >= 200:
                raise PanelError('最多创建 200 个定时任务')
            now = time.time()
            def parse_time(value):
                try:
                    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?', value):
                        raise ValueError()
                    return datetime.fromisoformat(value).timestamp()
                except (ValueError, OverflowError, OSError):
                    raise PanelError('请选择有效的发送日期和时间') from None
            send_at = parse_time(data.get('send_at')) if kind == 'once' else None
            same_schedule = bool(existing and existing['kind'] == kind and
                existing['interval_seconds'] == interval and existing['daily_time'] == daily and
                existing.get('window_start', '') == window_start and existing.get('window_end', '') == window_end and
                (kind != 'once' or existing.get('send_at') == send_at))
            first_run = parse_time(data['first_run']) if kind == 'interval' and data.get('first_run') else None
            if first_run is not None and first_run <= now:
                raise PanelError('首次发送时间应晚于当前时间')
            if send_at is not None and send_at <= now and not (same_schedule and (not enabled or existing['enabled'])):
                raise PanelError('发送时间应晚于当前时间')
            task = {'id': identifier or secrets.token_hex(12), 'account': account, 'name': name.strip(),
                    'action_id': action_id, 'params': params, 'kind': kind,
                    'interval_seconds': interval, 'daily_time': daily, 'enabled': enabled,
                    'last_status': existing.get('last_status', '') if existing else '',
                    'last_time': existing.get('last_time', 0) if existing else 0}
            if window_start:
                task.update(window_start=window_start, window_end=window_end)
                if first_run is not None and not in_daily_window(task, first_run):
                    raise PanelError('首次发送时间必须在每天生效时段内')
            if send_at is not None:
                task['send_at'] = send_at
            if existing and existing.get('last_manual_request'):
                task['last_manual_request'] = existing['last_manual_request']
            task['next_run'] = (first_run if first_run is not None else
                existing['next_run'] if same_schedule and existing['enabled'] and enabled else
                next_time(task, now)) if enabled else 0
            if existing:
                rows[rows.index(existing)] = task
            else:
                rows.append(task)
            self._write(rows)
        self.start()
        return {'ok': True, 'task': task}

    def toggle(self, data):
        enabled = data.get('enabled')
        if type(enabled) is not bool:
            raise PanelError('任务状态无效')
        account = self.controller.store.load().get('wechat_account')
        with self.lock:
            rows = self._read()
            row = next((r for r in rows if r.get('id') == data.get('id') and r.get('account') == account), None)
            if row is None:
                raise PanelError('任务不存在或不属于当前账号', 404)
            row['enabled'] = enabled
            if enabled and row['kind'] == 'once' and row['send_at'] <= time.time():
                raise PanelError('请先编辑任务，选择未来的发送时间')
            row['next_run'] = next_time(row, time.time()) if enabled else 0
            row.pop('claim', None)
            self._write(rows)
        self.start()
        return {'ok': True}

    def delete(self, data):
        account = self.controller.store.load().get('wechat_account')
        with self.lock:
            rows = self._read()
            remaining = [r for r in rows if not (r['id'] == data.get('id') and r['account'] == account)]
            if len(remaining) == len(rows):
                raise PanelError('任务不存在或不属于当前账号', 404)
            self._write(remaining)
        return {'ok': True}

    def run_now(self, data):
        """手动执行复用发送链路；不改变启用状态或下一次时间。"""
        request_id = data.get('request_id')
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', request_id):
            raise PanelError('执行请求编号无效')
        account = self.controller.store.load().get('wechat_account')
        with self.lock:
            rows = self._read()
            row = next((r for r in rows if r['id'] == data.get('id') and r['account'] == account), None)
            if row is None:
                raise PanelError('任务不存在或不属于当前账号', 404)
            if row.get('last_manual_request') == request_id:
                return {'ok': True, 'status': row.get('last_status', '')}
            if self.stop_event.is_set():
                raise PanelError('管理服务正在退出', 409)
            if row.get('claim') or row['id'] in self.inflight:
                raise PanelError('该任务正在执行，请等待结果', 409)
            self.controller.send_connection()
            if not self.slots.acquire(blocking=False):
                raise PanelError('任务繁忙，请稍后再试', 409)
            task = deepcopy(row)
            task.update(claim=secrets.token_hex(12), manual=True)
            try:
                row.update(claim=task['claim'], last_manual_request=request_id,
                           last_time=time.time(), last_status='执行中（手动）')
                self._write(rows)
                self.inflight.add(task['id'])
                threading.Thread(target=self._finish_async, args=(task,), name='ScheduledAction', daemon=True).start()
            except BaseException:
                self.inflight.discard(task['id'])
                self.slots.release()
                raise
        return {'ok': True, 'status': '执行中（手动）'}

    def tick(self, now=None):
        live_clock = now is None
        now = time.time() if live_clock else now
        with self.lock:
            rows = self._read()
            due = []
            changed = False
            for row in rows:
                if row.get('enabled') and row.get('next_run', 0) <= now:
                    if not in_daily_window(row, now):
                        row.update(next_run=next_time(row, now), last_status='已跳过（不在每天生效时段）')
                        changed = True
                        continue
                    if row.get('claim') or row['id'] in self.inflight:
                        if row['kind'] == 'once':
                            continue
                        row['next_run'] = next_time(row, now)
                        changed = True
                        continue
                    task = deepcopy(row)
                    task['claim'] = secrets.token_hex(12)
                    due.append(task)
                    # 发送之前持久化推进时间；重启绝不重试可能已经发出的消息。
                    row.update(next_run=next_time(row, now), last_time=now, last_status='执行中', claim=task['claim'])
                    changed = True
            if changed:
                self._write(rows)
                self.inflight.update(task['id'] for task in due)
        for task in due:
            if live_clock:
                if self.slots.acquire(blocking=False):
                    threading.Thread(target=self._finish_async, args=(task,), name='ScheduledAction', daemon=True).start()
                else:
                    self._finish(task, '已跳过（任务繁忙）')
            else:
                status = '已跳过（错过执行时间）' if now - task['next_run'] > 10 else self._deliver(task)
                self._finish(task, status)

    def _finish_async(self, task):
        try:
            status = '已跳过（错过执行时间）' if not task.get('manual') and time.time() - task['next_run'] > 10 else self._deliver(task)
            self._finish(task, status)
        finally:
            self.slots.release()

    def _finish(self, task, status):
        with self.lock:
            try:
                rows = self._read()
                for row in rows:
                    if row['id'] == task['id'] and row.get('claim') == task['claim']:
                        row['last_status'] = status
                        row.pop('claim', None)
                        if row['kind'] == 'once' and not task.get('manual'):
                            row.update(enabled=False, next_run=0)
                self._write(rows)
            finally:
                self.inflight.discard(task['id'])

    def _deliver(self, task):
        try:
            if not task.get('manual') and not in_daily_window(task, time.time()):
                return '已跳过（不在每天生效时段）'
            with self.lock:
                current = next((row for row in self._read() if row['id'] == task['id']), None)
                if not current or not (current.get('enabled') or task.get('manual')) or current.get('claim') != task['claim']:
                    return '已取消'
                if self.stop_event.is_set() or self.controller.store.load().get('wechat_account') != task['account']:
                    return '已跳过（账号已变化）'
            connection = self.controller.send_connection()
            with self.lock:
                current = next((row for row in self._read() if row['id'] == task['id']), None)
                if self.stop_event.is_set() or not current or not (current.get('enabled') or task.get('manual')) or current.get('claim') != task['claim']:
                    return '已取消'
                action_id = task.get('action_id', 'builtin.send_text')
                params = task.get('params', {'chat_id': task.get('chat_id'), 'content': task.get('content')})
                if action_id == 'builtin.send_text':
                    # 生成放在锁外，期间仍可暂停、编辑或删除任务。
                    result = None
                else:
                    # 仅预约快照；插件回调在锁外运行，避免长任务阻塞配置及其他任务。
                    result = None
            if action_id != 'builtin.send_text':
                self.controller.execute_scheduled_plugin(action_id, params, connection['session_id'], task['account'],
                                                         lambda: self._claim_current(task))
                return '执行完成'
            body = params['content']
            generated, generation_failed = False, False
            if needs_generation(params.get('description', '')):
                config = self.controller.store.load()
                allowed = config.get('chat_allowlist', [])
                if allowed and params['chat_id'] not in allowed:
                    return '未发送（会话受限）'
                try:
                    path = self.controller.workspace / config.get('llm_config_file', 'llm_config.json')
                    client = LLMClient.from_file(path)
                    previous = self._previous(task)
                    body = generate_reminder(client, body, params['description'], previous=previous)
                    generated = True
                    logging.getLogger('ScheduledTasks').info('定时提醒已生成：model=%s', client.response_model)
                except RepeatedReminderError:
                    logging.getLogger('ScheduledTasks').warning('提醒连续生成重复文案，跳过本次发送')
                    return '未发送（AI连续生成重复文案）'
                except (LLMError, ValueError, OSError):
                    # 仅生成失败可用原文兜底；发送失败或结果不明绝不重发。
                    generation_failed = True
                    logging.getLogger('ScheduledTasks').warning('AI提醒生成失败，使用原消息内容')
            # 4.2.0.50 尚未开放安全的群艾特发送。定时提醒仍应按时送达，
            # 因此仅对定时链路降级为普通群消息，并在结果中明确告知；任务配置里的
            # at_all 保留，待后续版本支持后自动恢复 @全员。
            requested_at_all = params.get('at_all', False) is True
            downgraded_at_all = False
            app = getattr(self.controller, 'app', None)
            backend = getattr(app, 'backend', None) if app is not None else None
            capabilities = getattr(backend, 'capabilities', None)
            if requested_at_all and capabilities is not None and 'group.text.mention' not in capabilities:
                downgraded_at_all = True
                logging.getLogger('ScheduledTasks').warning(
                    '4.2.0.50 暂不支持群艾特，定时提醒降级为普通群消息：task=%s', task['id'])
            with self.lock:
                if not self._claim_current(task):
                    return '已取消'
                if self.controller.store.load().get('wechat_account') != task['account']:
                    return '已跳过（账号已变化）'
                request_id = secrets.token_hex(16)
                result = self.controller.send_text({'request_id': request_id, 'session_id': connection['session_id'],
                    'chat_id': params['chat_id'], 'content': body,
                    'at_all': requested_at_all and not downgraded_at_all},
                    valid=lambda: self._claim_current(task))
            deadline = time.monotonic() + 120
            while result['status'] == 'sending' and time.monotonic() < deadline and not self.stop_event.wait(0.5):
                result = self.controller.send_status(request_id)
            if result['status'] == 'sent' and generated:
                if not self._remember(task, body):
                    return '已发送（近期文案保存失败）'
            if downgraded_at_all:
                sent_status = '已发送（当前微信版本暂不支持@全员，已降级为普通群消息）'
            else:
                sent_status = '已发送（AI生成失败，使用原文）' if generation_failed else '已发送'
            if result['status'] == 'failed' and result.get('error') == '@全员需要机器人是群主或管理员':
                return '未发送（@全员需要群主或管理员权限）'
            return {'sent': sent_status, 'failed': '发送失败，请核对微信', 'sending': '结果待确认，请核对微信'}.get(result['status'], '结果待确认，请核对微信')
        except (Exception, SystemExit) as error:
            logging.getLogger('ScheduledTasks').warning('定时任务未执行：%s', type(error).__name__)
            return ('未发送（未连接、发送忙或会话受限）' if task.get('action_id', 'builtin.send_text') == 'builtin.send_text'
                    else '执行失败（请检查连接、插件、白名单和日志）')

    def _claim_current(self, task):
        with self.lock:
            row = next((item for item in self._read() if item['id'] == task['id']), None)
            return bool(not self.stop_event.is_set() and row and (row.get('enabled') or task.get('manual'))
                        and row.get('claim') == task['claim']
                        and (task.get('manual') or in_daily_window(task, time.time())))

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                logging.getLogger('ScheduledTasks').exception('定时任务处理失败')
            self.stop_event.wait(1)
