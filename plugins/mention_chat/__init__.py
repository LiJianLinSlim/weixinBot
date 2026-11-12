"""真正艾特机器人后，用本群人格回复当前发言者。"""
import re
from pathlib import Path

from wxbot_framework.dialogue import Dialogue
from wxbot_framework.decision_router import DecisionRouter, fallback_decision
from wxbot_framework.llm import LLMError, ModelUnavailable
from wxbot_framework.members import SnapshotExpired
from wxbot_framework.personality import personality_text
from wxbot_framework.speech import deliver_model_reply
from wxbot_framework.mentions import strip_at_labels
from wxbot_framework.web_search import SearchError, WebSearch


def smart_intent(text):
    """免 @ 仅接受明确呼叫；疑问句和第二人称都不能证明接收者是机器人。"""
    if not isinstance(text, str):
        return False
    value = strip_at_labels(text).strip()
    if not value or value.startswith('/') or value in ('成语接龙', '合并接龙', '退出接龙'):
        return False
    # 不删除可见 @ 再猜对象：没有真实艾特本机的元数据时，可能是在叫别人。
    if '@' in text:
        return False
    call = re.match(r'^(?:(?:喂|嘿|嗨)[\s，,、]*)?(?:小管家|小助手|机器人|ZLLJ)(.*)$', value, re.I)
    if call is None:
        return False
    rest = call.group(1)
    if not rest:
        return True
    if re.match(r'^[A-Za-z0-9_]', rest):
        return False
    rest = rest.lstrip(' \t\r\n，,、：:！!？?。~～')
    if not rest:
        return True
    # 开头提到机器人仍可能只是评论或转述，而非呼叫。
    if re.match(r'^(?:的|们|说|表示|回复|回答|刚才|刚刚|又|居然|竟然|总是|老是|怎么还|是不是又)', rest):
        return False
    return True


def asks_to_view_image(text):
    """只判断当前提问；历史提到图片或仅有图片缓存不能触发上传。"""
    text = strip_at_labels(text)
    if any(word in text for word in (
            '这张图', '这个图', '这幅图', '这张照片', '这张截图', '这张相片',
            '刚才的图', '刚才那张图', '刚发的图', '刚发的照片', '刚发的截图',
            '上面的图', '上面那张图', '上一张图')):
        return True
    return bool(re.match(
        r'^(?:请|帮我|麻烦|再|重新|给我)?\s*'
        r'(?:看看|看下|看一下|看一眼|看|识别|分析|解读|解释|描述|读)'
        r'(?:一下|下)?\s*(?:图片|截图|照片|相片|图)', text))


def failure_notice(error):
    """只向群内发送固定诊断，不回显上游正文、网址、密钥或堆栈。"""
    detail = str(error)
    if isinstance(error, ModelUnavailable):
        return '模型服务暂时不可用，自动重试或切换仍未成功，请稍后再试。'
    if isinstance(error, SearchError):
        if '限流' in detail:
            return '搜索服务暂时达到访问额度，请稍后再试。'
        if '缺少' in detail or '过长' in detail:
            return '请写明要搜索的问题，尽量包含节目名称、季数或人物姓名。'
        if '疑似包含密钥' in detail:
            return '搜索内容疑似包含密钥，请删除敏感信息后重试。'
        if '未配置密钥' in detail or '尚未连接' in detail:
            return '联网搜索服务尚未连接，请先在本机管理配置中接入搜索服务。'
        return '这次联网搜索没拿到可靠的相关结果，请稍后重试，或补充具体名称、季数和关键词。'
    if '引用图片' in detail:
        if '仅接受' in detail:
            return '目前引用看图只支持普通图片，请引用一张图片再艾特我提问。'
        return '没有取到你引用的原图，请把那张图片重新发到本群，再引用它艾特我提问。'
    if '图片' in detail:
        if '仍在下载' in detail:
            return '图片还在下载，请稍后再艾特我。'
        if '不支持' in detail:
            return '当前模型接口不支持看图，请在管理台选择支持图片的模型。'
        if '没有' in detail:
            return '没有找到你最近发的图片，请先发一张普通图片，再艾特我提问。'
        return '图片未能读取，请重新发送一张普通图片，再艾特我提问。'
    if any(word in detail for word in ("JSON", "格式", "空结果", "空正文")):
        return "模型没有返回可解析的回答，请再试一次，或在管理台切换模型。"
    if "截断" in detail:
        return "模型输出被截断，请减少回答长度，或在管理台调整输出上限。"
    if "HTTP 429" in detail:
        return "模型服务限流或额度不足，请稍后再试并检查管理台。"
    if "HTTP 401" in detail or "HTTP 403" in detail:
        return "模型服务认证失败，请检查本机模型接口的登录和密钥。"
    if any(f"HTTP {code}" in detail for code in (500, 502, 503, 504)):
        return "模型服务暂时出错，请稍后再试。"
    if "超时" in detail or "连接失败" in detail:
        return "模型服务连接失败或超时，请稍后再艾特我。"
    return "模型调用失败，请查看本机管理台的运行日志。"


class Plugin:
    def on_load(self, ctx):
        self.members = ctx.members
        if not self.members.history_enabled:
            raise ValueError("艾特上下文对话需要 history_enabled: true")
        ctx.llm  # 启动时核对配置，不请求接口。
        self.web_search = WebSearch(Path(getattr(ctx, 'project_root', Path(__file__).resolve().parents[2])) / 'search_config.json')
        self.decision_router = DecisionRouter(Path(getattr(ctx, 'project_root', Path(__file__).resolve().parents[2])) / 'routing_config.json')

    def on_message(self, message, ctx):
        private = (not message.is_group and message.chat_id in getattr(self.members, 'private_chats', ()) and
                   message.sender_id == message.chat_id and not message.is_self)
        at_members = message.data.get('at_members', ())
        quote = message.data.get('quoted_message')
        directed_elsewhere = (bool(at_members) and self.members.account not in at_members) or (
            isinstance(quote, dict) and bool(quote.get('sender_id')) and
            quote['sender_id'] != self.members.account)
        smart = (message.is_group and not message.is_self and not directed_elsewhere and
                 getattr(self.members, 'smart_reply', False) and
                 message.chat_id in self.members.groups and smart_intent(message.text))
        if (message.type != 1 or "WXBOT_AT_CHAT_PROBE" in message.text or
                message.data.get("bot_generated") or not message.data.get('dispatch_to_plugins', True)):
            return
        if not private and not smart and (not message.is_group or message.chat_id not in self.members.groups or
                not message.data.get('is_at_self') or self.members.account not in message.data.get('at_members', ())):
            return
        if private and message.text.strip().split(maxsplit=1)[:1] == ['/切换人格']:
            # 由人格插件确认，不能再把切换命令交给模型回答。
            return
        if private and message.text.strip() == '/清空':
            self.members.clear(message.chat_id)
            ctx.reply(message, '私聊上下文已清空')
            return
        personality = ctx.personality(message)
        try:
            snapshot = self.members.conversation_snapshot(message)
            guard = lambda: ctx.response_guard(snapshot, personality)
            self.reply_to_member(message, ctx, snapshot, personality, guard)
            ctx.logger.info("%s对话完成：room=%s persona=%s context=%d", '私聊' if private else '艾特', message.chat_id,
                            personality.name, len(snapshot.messages))
        except SnapshotExpired:
            ctx.logger.info("艾特对话已取消：记录或人格状态已变更")
        except LLMError as error:
            ctx.logger.warning("艾特对话失败：%s", error)
            try:
                ctx.reply(message, failure_notice(error), delivery_guard=guard)
            except SnapshotExpired:
                pass

    def reply_to_member(self, message, ctx, snapshot, personality, delivery_guard):
        """上下文已准备好，在这里生成并发送给群友的回复。"""
        recent = getattr(ctx, 'recent_images', None)
        quoted = 'quoted_message' in message.data
        requested = asks_to_view_image(message.text)
        if not message.is_group and (quoted or requested):
            ctx.reply(message, '私聊目前先支持文字问答 看图或引用问答请在群里 @我', delivery_guard=delivery_guard)
            return
        if quoted and recent is None:
            raise LLMError('引用图片未缓存')
        images = recent.for_message(message) if recent is not None and (quoted or requested) else ()
        if not images and requested:
            raise LLMError('没有找到近期图片')
        client = ctx.llm_for(message, personality=personality)
        dialogue = Dialogue(client)
        options = {}
        if not message.is_group:
            options['is_private'] = True
        if images:
            options['images'] = images
        router = getattr(self, 'decision_router', None)
        decision = (router.decide(client, snapshot, has_image=bool(images)) if router is not None
                    else fallback_decision(snapshot))
        from wxbot_framework.dialogue import response_tone
        options['use_context'] = decision.use_context
        options['tone'] = response_tone(snapshot, getattr(decision, 'tone', 'neutral'),
                                        use_context=decision.use_context)
        ctx.logger.info('问答决策：model=%s search=%s context=%s tone=%s',
                        decision.source, decision.search, decision.use_context, options['tone'])
        if decision.search:
            try:
                options['web_search'] = self.web_search.search(decision.query)
                ctx.logger.info('联网搜索完成：provider=%s results=%d',
                                options['web_search'].provider, len(options['web_search'].results))
            except SearchError:
                ctx.logger.warning('本次联网搜索未取得资料，继续回答；不声称已核实实时信息')
        result = dialogue.reply(snapshot, **options)
        actual_model = getattr(client, 'response_model', None)
        if isinstance(actual_model, str) and actual_model:
            ctx.logger.info('问答实际模型：%s', actual_model)
        reply = personality_text(result,
                                 '默认' if options['tone'] == 'serious' else personality.name)
        if message.is_group:
            display = None
            capabilities = getattr(ctx, 'capabilities', None)
            can_mention = capabilities is None or 'group.text.mention' in capabilities
            if can_mention and getattr(self.members, 'has_intervening_message', lambda *_: True)(snapshot, message.sender_id):
                display = self.members.display_name(message.sender_id, message.chat_id) or snapshot.speaker_name
            deliver_model_reply(ctx, message, reply, display_name=display, delivery_guard=delivery_guard)
        elif not ctx.reply(message, reply, delivery_guard=delivery_guard):
            raise RuntimeError('私聊回复未确认发送')
        if callable(getattr(type(self.members), 'remember_dialogue', None)):
            evidence = options.get('web_search')
            try:
                self.members.remember_dialogue(message, snapshot, reply,
                    evidence.prompt_data() if evidence is not None else None)
            except SnapshotExpired:
                pass
            except Exception:
                ctx.logger.warning('本轮回复已发送，但问答上下文暂未保存')
