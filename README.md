# weixinBot

Windows 微信机器人 Python 插件框架，提供本地网页管理台、控制台入口、插件系统和定时任务。这个仓库保存机器人业务代码、原生桥接代码和离线测试；账号、模型密钥、聊天记录及运行时状态只保存在本机。

## 当前适配范围

本项目使用 Windows 微信桌面客户端的本机原生桥接，不调用微信开放平台 API。桥接加载前会校验微信核心文件的精确版本指纹；未知构建会拒绝加载。

当前重点适配微信 **4.2.0.50**，已验证以下链路：

- 当前登录账号识别
- 私聊和群聊普通文本接收、发送
- 群聊 `@全员`
- 定时文本任务
- 本地消息监控和 SQLite 持久化

4.2.0.50 暂未开放图片、文件、语音、单独成员艾特、撤回、好友申请、联系人缓存和群成员缓存等链路。其他微信版本的能力以 [native/compatibility.json](native/compatibility.json) 和 [原生适配说明](native/README.md) 为准，不能把一个版本的内部地址直接套到另一个版本。

这是非官方客户端适配，微信升级后需要重新核对版本指纹、函数地址和对象布局。请只在你有权使用的账号和设备上运行，并先阅读项目协议及相关法律要求。

## 功能

- 多账号管理：发现已登录微信，按账号启停，独立保存插件配置与数据。
- 消息收发：文本自动分段，群聊可以使用真实 `@全员`；媒体和其他原生能力取决于当前微信版本。
- 消息监控：显示时间、会话 ID、发送者及正文，保留本地记录和撤回标记。
- 定时任务：按间隔、每天指定时间或一次性时间执行文字发送，支持网页中手动立即执行。
- 群聊机器人：支持上下文对话、人格切换、智能回复和私聊白名单。
- 模型与语音：支持 OpenAI 兼容的 Chat Completions API；语音功能需要额外安装语音依赖并满足对应版本能力。
- 插件二开：插件以独立目录组织，支持离线测试和网页管理。

## 快速开始

运行条件：Windows 10/11 x64、64 位 Python（当前开发环境为 3.12）、Microsoft Visual C++ x64 运行库，以及已登录的 Windows 微信客户端。

```powershell
python -m pip install -r requirements.txt
```

登录电脑微信后，双击 `start_web.bat`，打开 `http://127.0.0.1:8765`，在账号行点击“启动”。首次使用无需手填 wxid。也可以使用控制台入口：

```powershell
python main.py --check
python main.py
```

`--check` 只检查配置和插件语法，不连接微信或调用模型。发送 WAV 语音或使用语音合成时，还需安装 `requirements-voice.txt`。

正常退出时，先在网页停止机器人，再在管理台窗口按 Ctrl+C；控制台入口输入 `quit`。同一个微信进程只能由一个项目副本连接。

## 配置与数据

启动时会从示例配置生成本机配置。下面这些文件包含账号、会话或密钥信息，默认都被 Git 忽略，不能提交：

| 文件 | 用途 |
| --- | --- |
| `bot_config.json` | 后端、会话范围、插件开关与选项 |
| `members_config.json` | 群组、成员和功能范围 |
| `llm_config.json` | 模型地址、密钥及模型名称 |
| `tts_config.json` | Fish Audio 密钥及语音选项 |
| `routing_config.json` | 本地路由模型配置 |
| `.runtime/` | SQLite、聊天记录、任务状态、媒体缓存和运行日志 |

公开仓库只提供 `*.example.json` 模板。修改本地配置前请确认文件没有被 `git add -f` 强制加入提交。

## 文档

| 需要做什么 | 文档 |
| --- | --- |
| 安装、选择账号、添加群 | [快速开始](docs/guide/quick-start.md) · [账号与群聊](docs/guide/account-and-groups.md) |
| 启停账号、监控消息、修改插件 | [网页管理台](docs/guide/web-panel.md) |
| 定时任务和手动立即执行 | [网页管理台](docs/guide/web-panel.md) |
| 图片、文件、表情和语音 | [消息文档](docs/messages/images.md) · [文件](docs/messages/files.md) · [表情](docs/messages/emoticons.md) · [语音](docs/messages/voice.md) |
| 开发原生桥接 | [原生适配说明](native/README.md) · [代码结构与验证](docs/development/structure.md) |
| 插件开发 | [插件开发](docs/development/plugins.md) |

## 目录

```text
main.py / web.py      控制台 / 网页入口
wxbot_framework/      连接生命周期、消息、插件、模型和管理台
plugins/              独立功能与业务函数
native/               原生桥接、版本指纹与必要 DLL
webui/                管理台静态页面
docs/                 使用与开发文档
tests/                离线回归测试
tools/                配置初始化、测试与发布检查
```

## 开发检查

```powershell
python -m unittest discover -s tests -p "test_*.py"
python tools/check_publication.py
```

发布检查会检查运行时目录、个人配置、常见密钥格式和不应公开的构建产物。通过检查后再提交代码。\n