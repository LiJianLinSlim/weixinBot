"""通过配置接入 OpenAI 兼容的大模型接口，不记录密钥或聊天正文。"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import math
import subprocess
import threading
import time
from ipaddress import ip_address
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen


class LLMError(RuntimeError):
    pass


class ModelUnavailable(LLMError):
    """只表示临时服务故障，不包含认证、配额、内容拒绝等错误。"""


class LLMClient:
    supports_plain_text = True
    supports_images = True

    def __init__(self, options):
        if not isinstance(options, dict):
            raise ValueError("大模型配置必须是 JSON 对象")
        base_url = options.get("base_url", "https://api.deepseek.com")
        if not isinstance(base_url, str):
            raise ValueError("大模型 base_url 必须是接口地址字符串")
        self.base_url = base_url.rstrip("/")
        try:
            parsed = urlsplit(self.base_url)
            local = parsed.hostname == "localhost"
            if not local and parsed.hostname:
                try:
                    local = ip_address(parsed.hostname).is_loopback
                except ValueError:
                    pass
            valid = (parsed.scheme == "https" or (parsed.scheme == "http" and local))
            valid = valid and parsed.hostname and parsed.port != 0
            valid = valid and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
        except ValueError:
            valid = False
        if not valid or any(ch.isspace() or ord(ch) < 32 for ch in base_url):
            raise ValueError("大模型 base_url 应为不含认证信息的 HTTPS 地址；本机回环地址可使用 HTTP")
        self.endpoint = (self.base_url if self.base_url.endswith("/chat/completions")
                         else self.base_url + "/chat/completions")
        self._deepseek_official = (parsed.scheme == "https" and parsed.hostname == "api.deepseek.com"
                                  and parsed.port in (None, 443))
        self.local_bridge_task = options.get("local_bridge_task")
        if self.local_bridge_task is not None and (
                self.local_bridge_task != "CodexMagpieDucxBridge" or
                parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or
                parsed.port != 3427 or parsed.path.rstrip("/") != "/v1"):
            raise ValueError("local_bridge_task 仅适用于本机 3427 的指定 OneAPI 桥接")
        self.model = options.get("model", "deepseek-flash")
        self._api_key = options.get("api_key", "")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("大模型 model 不能为空")
        fallback = options.get('fallback_models', [])
        if not isinstance(fallback, list) or len(fallback) > 2:
            raise ValueError('fallback_models 应为最多两个备用模型的列表')
        self.fallback_models = []
        for row in fallback:
            if (not isinstance(row, dict) or set(row) - {'model', 'vision', 'timeout_seconds'} or
                    not isinstance(row.get('model'), str) or not row['model'].strip() or
                    len(row['model']) > 256 or any(ord(c) < 32 for c in row['model']) or
                    type(row.get('vision', False)) is not bool or
                    type(row.get('timeout_seconds', 20)) is not int or
                    not 1 <= row.get('timeout_seconds', 20) <= 60):
                raise ValueError('备用模型需提供 model 和布尔型 vision')
            if row['model'] != self.model and row['model'] not in {x['model'] for x in self.fallback_models}:
                entry = {'model': row['model'], 'vision': row.get('vision', False)}
                if 'timeout_seconds' in row:
                    entry['timeout_seconds'] = row['timeout_seconds']
                self.fallback_models.append(entry)
        self._response_state = threading.local()
        if not isinstance(self._api_key, str) or not self._api_key.strip():
            raise ValueError("请在大模型配置文件中填写 api_key")
        self.timeout = options.get("timeout_seconds", 20)
        self.max_tokens = options.get("max_tokens", 512)
        if type(self.timeout) not in (int, float) or not math.isfinite(self.timeout) or not 1 <= self.timeout <= 60:
            raise ValueError("timeout_seconds 应为 1–60 秒")
        if type(self.max_tokens) is not int or not 128 <= self.max_tokens <= 4096:
            raise ValueError("max_tokens 应为 128–4096 的整数")
        self.json_mode = options.get("json_mode", True)
        if type(self.json_mode) is not bool:
            raise ValueError("json_mode 应为布尔值")
        self.token_limit_parameter = options.get("token_limit_parameter", "max_tokens")
        if self.token_limit_parameter not in ("max_tokens", "max_completion_tokens"):
            raise ValueError("token_limit_parameter 应为 max_tokens 或 max_completion_tokens")
        extra = options.get("extra_body", {})
        if not isinstance(extra, dict) or not all(isinstance(key, str) for key in extra):
            raise ValueError("extra_body 应为 JSON 对象")
        reserved = {"model", "messages", "stream", "response_format", "max_tokens", "max_completion_tokens",
                    "api_key", "authorization", "Authorization"}
        if reserved.intersection(extra):
            raise ValueError("extra_body 不能覆盖模型、消息、输出格式、令牌上限或认证字段")
        try:
            encoded = json.dumps(extra, ensure_ascii=False, allow_nan=False)
            if len(encoded.encode("utf-8")) > 16384:
                raise ValueError
            # 隔离调用方后续修改，扩展参数只在显式配置时发送。
            self.extra_body = json.loads(encoded)
        except (TypeError, ValueError, RecursionError):
            raise ValueError("extra_body 必须是有效 JSON，且不能超过 16 KiB") from None

    @classmethod
    def from_file(cls, path):
        return cls(json.loads(Path(path).read_text(encoding="utf-8-sig")))

    def list_models(self):
        """读取已保存接口的模型目录，不进行推理，也不向页面返回密钥。"""
        self._ensure_local_bridge()
        base = self.base_url.removesuffix("/chat/completions")
        request = Request(base + "/models", headers={"Authorization": "Bearer " + self._api_key})

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *_args, **_kwargs):
                return None

        try:
            with build_opener(NoRedirect()).open(request, timeout=10) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise LLMError("模型列表过大，请手动填写模型名称")
            data = json.loads(raw)
            rows = data.get("data") if isinstance(data, dict) else None
            if not isinstance(rows, list) or len(rows) > 10000:
                raise LLMError("接口返回的模型列表格式不正确")
            models = list(dict.fromkeys(
                row["id"].strip() for row in rows if isinstance(row, dict) and
                isinstance(row.get("id"), str) and 0 < len(row["id"].strip()) <= 256 and
                not any(ord(char) < 32 for char in row["id"])))
            if not models:
                raise LLMError("接口没有返回可用模型，可手动填写模型名称")
            return models
        except HTTPError as error:
            error.close()
            if error.code == 404:
                raise LLMError("此接口未提供模型列表，可手动填写模型名称") from None
            if 300 <= error.code < 400:
                raise LLMError("模型列表接口发生重定向，请核对已保存的接口地址") from None
            raise LLMError(f"读取模型列表失败（HTTP {error.code}），请核对已保存的密钥和接口地址") from None
        except (URLError, TimeoutError, OSError):
            raise LLMError("读取模型列表连接失败或超时，可稍后刷新或手动填写") from None
        except (ValueError, TypeError):
            raise LLMError("接口返回的模型列表格式不正确") from None

    @property
    def response_model(self):
        """仅当前线程最近一次成功回答的模型，不改变管理台选中的主模型。"""
        return getattr(self._response_state, 'model', None)

    def complete_text(self, system, user, *, images=(), timeout_seconds=None):
        budget = self.timeout
        if timeout_seconds is not None:
            if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                    or not 1 <= timeout_seconds <= 60):
                raise ValueError('调用超时应为 1–60 秒')
            budget = min(budget, timeout_seconds)
        self._response_state.model = None
        if not self.fallback_models:
            result = self._complete_text(system, user, images=images, timeout=budget)
            self._response_state.model = self.model
            return result
        candidates = [(self.model, 20)] + [(row['model'], row.get('timeout_seconds', 20))
                                          for row in self.fallback_models if not images or row['vision']]
        self._ensure_local_bridge()
        deadline = time.monotonic() + budget
        for index, (model, candidate_timeout) in enumerate(candidates):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # 有备用模型时，每个候选只请求一次；最多三个模型，共享原来的总超时。
            if index == 0:
                candidate_timeout = min(candidate_timeout, budget / len(candidates))
            limit = min(candidate_timeout, remaining)
            prompt = system.replace('\n运行配置提供的模型标识为：' + json.dumps(self.model, ensure_ascii=False),
                                    '\n运行配置提供的模型标识为：' + json.dumps(model, ensure_ascii=False))
            try:
                result = self._complete_text(prompt, user, images=images, model=model,
                                             timeout=limit, attempts=1, bridge_ready=True)
                self._response_state.model = model
                if index:
                    logging.getLogger('LLM').info('模型自动回退成功：主模型=%s 实际模型=%s', self.model, model)
                return result
            except ModelUnavailable:
                if index < len(candidates)-1:
                    logging.getLogger('LLM').warning('模型暂不可用，自动尝试备用模型：%s → %s',
                                                    model, candidates[index+1][0])
                    continue
                raise ModelUnavailable('主模型及可用备用模型暂时不可用，请稍后再试') from None
        raise ModelUnavailable('模型调用总等待时间已用完，请稍后再试')

    def _complete_text(self, system, user, *, images=(), model=None, timeout=None, attempts=2, bridge_ready=False):
        """普通问答只读取最终回答，不要求模型把聊天正文再包装成 JSON。"""
        if not isinstance(images, (tuple, list)) or len(images) > 1:
            raise LLMError('图片输入每次只支持一张')
        content = user
        if images:
            url = images[0]
            prefix = 'data:image/jpeg;base64,'
            if (not isinstance(url, str) or not url.startswith(prefix) or
                    len(url) > len(prefix) + 4 * ((2 * 1024 * 1024 + 2) // 3)):
                raise LLMError('图片输入必须是本地处理的 JPEG 数据，且不超过 2 MiB')
            try:
                body = base64.b64decode(url[len(prefix):], validate=True)
                if not 0 < len(body) <= 2 * 1024 * 1024 or not body.startswith(b'\xff\xd8\xff'):
                    raise ValueError
            except (ValueError, binascii.Error):
                raise LLMError('图片输入数据无效') from None
            content = [{'type': 'text', 'text': user},
                       {'type': 'image_url', 'image_url': {'url': url}}]
        if not bridge_ready:
            self._ensure_local_bridge()
        payload = {"model": model or self.model, "messages": [{"role": "system", "content": system},
                   {"role": "user", "content": content}], self.token_limit_parameter: self.max_tokens,
                   "stream": False, **self.extra_body}
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        try:
            for attempt in range(attempts):
                request = Request(self.endpoint, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                  headers={"Authorization": "Bearer " + self._api_key, "Content-Type": "application/json"},
                                  method="POST")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ModelUnavailable("大模型连接失败或超时，请稍后再试")
                try:
                    with urlopen(request, timeout=remaining) as response:
                        raw = response.read(1024 * 1024 + 1)
                except HTTPError as error:
                    # 仅重试没有工具执行的纯推理请求；最多两次，共享原超时预算。
                    if error.code in (502, 503, 504) and attempt + 1 < attempts and deadline - time.monotonic() > 1:
                        status = error.code
                        error.close()
                        logging.getLogger("LLM").warning("模型服务临时错误 HTTP %d，重试一次", status)
                        time.sleep(0.4)
                        continue
                    raise
                if len(raw) > 1024 * 1024:
                    raise LLMError("大模型响应过大")
                result = json.loads(raw)
                choice = result["choices"][0]
                message = choice["message"]
                finish = choice.get("finish_reason")
                if finish == "length":
                    raise LLMError("大模型响应被截断，请增加输出上限或缩短回答")
                if finish == "content_filter" or message.get("refusal"):
                    raise LLMError("模型服务拒绝提供回答，请查看内容限制")
                if finish in ("tool_calls", "function_call") or message.get("tool_calls") or message.get("function_call"):
                    raise LLMError("当前聊天没有执行工具调用的能力")
                content = message.get("content")
                if isinstance(content, list):
                    if not all(isinstance(part, dict) and part.get("type") == "text" and
                               isinstance(part.get("text"), str) for part in content):
                        raise LLMError("大模型最终回答不是文本")
                    content = "".join(part["text"] for part in content)
                if content is not None and not isinstance(content, str):
                    raise LLMError("大模型最终回答不是文本")
                content = (content or "").strip()
                # Never expose reasoning_content or a truncated thinking block as the answer.
                if content.startswith("<think>"):
                    _, separator, content = content.partition("</think>")
                    content = content.strip() if separator else ""
                if content:
                    return content
                if attempt + 1 < attempts:
                    logging.getLogger("LLM").warning("模型未提供最终文字回答，重试一次")
                    continue
                raise ModelUnavailable("模型没有提供最终文字回答，建议切换模型")
        except HTTPError as error:
            error.close()
            if images and error.code in (400, 415, 422):
                raise LLMError('当前模型接口不支持图片或拒绝图片输入，请切换支持看图的模型') from None
            if error.code in (500, 502, 503, 504):
                raise ModelUnavailable(f"模型上游服务暂时不可用（HTTP {error.code}），请稍后再试") from None
            raise LLMError(f"大模型接口 HTTP {error.code}；请核对密钥、余额和模型配置") from None
        except (URLError, TimeoutError, OSError):
            raise ModelUnavailable("大模型连接失败或超时，请稍后再试") from None
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise LLMError("模型接口响应包格式不正确，请检查接口兼容性") from None

    def complete_json(self, system, user):
        self._ensure_local_bridge()
        json_instruction = "\n\n请只返回一个有效的 JSON 对象，不要使用 Markdown 代码围栏或附加说明。"
        if not self.json_mode:
            system += json_instruction
        payload = {"model": self.model, "messages": [{"role": "system", "content": system},
                                                       {"role": "user", "content": user}],
                   self.token_limit_parameter: self.max_tokens, "stream": False, **self.extra_body}
        if self.json_mode:
            payload["response_format"] = {"type": "json_object"}
        # 保留官方 DeepSeek 旧版的非思考行为；兼容网关不携带专属参数。
        # 用户显式指定的思考设置始终优先，旧的 reasoner 模型也不强制关闭。
        if (self._deepseek_official and self.model in ("deepseek-flash", "deepseek-v4-pro", "deepseek-chat")
                and not {"thinking", "reasoning_effort"}.intersection(self.extra_body)):
            payload["thinking"] = {"type": "disabled"}
        allow_fenced = not self.json_mode
        try:
            for attempt in range(2):
                request = Request(self.endpoint,
                                  data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                  headers={"Authorization": "Bearer " + self._api_key,
                                           "Content-Type": "application/json"}, method="POST")
                with urlopen(request, timeout=self.timeout) as response:
                    raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise LLMError("大模型响应过大")
                try:
                    result = json.loads(raw)
                except (ValueError, TypeError):
                    raise LLMError("模型接口的响应包不是有效 JSON，请检查接口兼容性") from None
                choice = result["choices"][0]
                finish = choice.get("finish_reason")
                message = choice["message"]
                if finish == "length":
                    raise LLMError("大模型响应被截断，请增加 max_tokens")
                if finish == "content_filter" or message.get("refusal"):
                    raise LLMError("大模型接口未提供可用正文，请检查服务商的内容限制")
                if finish in ("tool_calls", "function_call") or message.get("tool_calls") or message.get("function_call"):
                    raise LLMError("大模型返回了工具调用，当前功能需要直接返回 JSON 正文")
                content = message["content"]
                if content is None or (isinstance(content, str) and not content.strip()):
                    if attempt == 0 and finish in (None, "stop"):
                        if self._deepseek_official and self.json_mode:
                            # 官方 JSON 模式存在偶发空正文：仅此情形退回提示词 JSON，仍严格解析。
                            payload.pop("response_format", None)
                            payload["messages"][0]["content"] += json_instruction
                            allow_fenced = True
                        logging.getLogger("LLM").warning("大模型返回空正文，重试一次")
                        continue
                    suffix = "（只收到思考内容，未取得最终答案）" if message.get("reasoning_content") else ""
                    raise LLMError("大模型返回了空结果" + suffix)
                if not isinstance(content, str):
                    raise LLMError("大模型正文格式不正确")
                content = content.strip()
                if allow_fenced and content.startswith("```") and content.endswith("```"):
                    opening, separator, body = content.partition("\n")
                    if separator and opening.strip().lower() in ("```", "```json"):
                        content = body[:-3].strip()
                try:
                    data = json.loads(content)
                except ValueError:
                    if attempt == 0:
                        logging.getLogger("LLM").warning("模型正文不是有效 JSON，纠正格式后重试一次")
                        payload["messages"][0]["content"] += (
                            "\n上次输出未通过 JSON 解析。重新回答原问题，只返回一个合法的 JSON 对象，"
                            "严格使用功能指定的字段；不要输出思考过程、说明、代码围栏或其他文字。")
                        continue
                    raise LLMError("模型连续返回无效 JSON，建议切换模型后重试") from None
                if not isinstance(data, dict):
                    raise LLMError("大模型结果应为 JSON 对象")
                return data
        except HTTPError as error:
            # 不输出响应正文或请求头，避免服务器错误回显密钥。
            raise LLMError(f"大模型接口 HTTP {error.code}；请核对密钥、余额和模型配置") from None
        except (URLError, TimeoutError, OSError):
            raise LLMError("大模型连接失败或超时，请稍后再试") from None
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise LLMError("大模型响应格式不正确，请重试") from None

    def _ensure_local_bridge(self):
        """仅在本机桥接离线时唤醒已有任务；不重放任何模型请求。"""
        if self.local_bridge_task is None:
            return

        def ready():
            try:
                with urlopen("http://127.0.0.1:3427/health", timeout=1) as response:
                    status = json.load(response)
            except (OSError, ValueError):
                return False
            if status.get("source") != "ducx" or not status.get("chatCompletions"):
                raise LLMError("本机桥接尚未启用 helper 的 Chat Completions 入口")
            return bool(status.get("ready"))

        if ready():
            return
        try:
            subprocess.run([
                r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
                "-NoProfile", "-NonInteractive", "-Command",
                "Start-ScheduledTask -TaskName 'CodexMagpieDucxBridge' -ErrorAction Stop",
            ], check=True, capture_output=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            raise LLMError("无法启动本机 OneAPI 桥接，请检查 CodexMagpieDucxBridge 任务") from None
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if ready():
                return
            time.sleep(0.25)
        raise LLMError("本机 OneAPI 桥接启动超时，请稍后重试")
