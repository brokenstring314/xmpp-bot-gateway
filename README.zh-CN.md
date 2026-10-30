# xmpp-bot-gateway

[English](README.md)

一个独立运行的网关：登录一个 XMPP 账号，把它的单聊和群聊（MUC）通过 OneBot v11 协议暴露出来，让 OneBot v11 机器人框架可以跑在 XMPP 上。

```
XMPP 客户端 ⇄ XMPP 服务器 ⇄ xmpp-bot-gateway ⇄ OneBot v11（反向 WebSocket）⇄ 机器人框架
```

## 功能

- 明文单聊和 MUC 群聊（加入配置的房间，不存在则创建为即时房间；进群时不重放历史，离线消息由 MAM 补拉负责）
- OneBot v11 反向 WebSocket（Universal 角色），可选 access token，断线退避重连
- 元事件：`lifecycle`（连接）和 `heartbeat`
- 消息事件：`message.private`、`message.group`；数组或 CQ 码字符串格式
- 提及：以 `<机器人昵称>:` 开头或带有指向机器人的 XEP-0372 提及引用的群消息，会变成 `at` 消息段；发出的 `at` 消息段变成 `<昵称>: ` 并附带 XEP-0372 提及引用
- 发送媒体：`image` / `record` / `video` / `file` 消息段接受 http(s) URL、`base64://` 数据或 `file://` / 本地路径。非 URL 数据通过 XEP-0363 HTTP Upload 上传。每个媒体单独发一条消息，正文为链接并附 XEP-0066 OOB，客户端会内联显示。在 OMEMO 会话中，文件先加密再上传（XEP-0454），以加密的 `aesgcm://` 链接发送，不附明文 OOB
- 接收媒体：正文只有一个与 OOB URL 一致的 http(s) 链接，或 OMEMO 会话里的 `aesgcm://` 链接，会按扩展名变成 `image` / `record` / `video` 消息段。加密文件会被下载、解密并保存到 `media.dir`，以 `file://` URI 传给机器人框架，所以框架必须能读到该路径
- 回复（XEP-0461）：发出的 `reply` 消息段引用原消息（群聊中用房间的 stanza-id），并为不支持回复的客户端加一行引用回退（XEP-0428）；收到的回复变成带原 `message_id` 的 `reply` 消息段，并去掉引用回退文字。原消息未知时保留引用文字
- 送达回执（XEP-0184）：自动应答收到消息上的回执请求
- 撤回：`delete_msg` 撤回网关发出的消息（XEP-0424），包括拆成文字和媒体的每一部分。联系人或群成员撤回自己的消息时，推送 `notice.friend_recall` / `notice.group_recall`，带原 `message_id`；撤回别人的消息或未知消息会被忽略
- 离线补拉（XEP-0313 MAM）：连接或重连后，从服务器存档拉取离线期间错过的单聊和群聊消息，保留原始时间、不重复。首次运行从当前时间开始；上限为每个存档 `xmpp.mam_max_messages` 条、`xmpp.mam_max_age` 秒；用 `xmpp.mam_catchup = false` 关闭
- OneBot 连接恢复后，补发未送达的事件
- 用户、群、消息使用稳定的数字 ID，持久化在 SQLite
- 单聊 OMEMO（可选，`[omemo]` 附加依赖）：解密收到的加密消息；对上一条消息是加密的联系人，回复也加密，使用旧版 OMEMO（`eu.siacs.conversations.axolotl`）；加密失败时发送失败，不回退为明文。信任模式：验证前盲信（`btbv`）或 `manual`。暂不支持群聊 OMEMO，加密的群消息会被忽略
- 房间邀请（XEP-0045 中转邀请和 XEP-0249 直接邀请）：默认只接受花名册联系人和 `xmpp.admins` 的邀请；把 `xmpp.accept_invites` 设为 `"all"` 或 `"none"` 可改变

### 加固

- 只接受机器人已加入房间的群消息；存档结果只接受来自机器人自己或该房间存档的。
- 收到的加密文件只从公网地址下载（连接时再次检查），不跟随重定向，有大小上限；媒体目录有总量上限。
- 收到的消息按发送者限流，并限制最大长度。
- 发出的本地文件必须是普通文件，可用 `media.local_file_dirs` 限定目录。
- XMPP 自动重连；重连后重新加入房间
- 收到的 XMPP 错误节会连同错误条件和文字记录到日志

### 支持的动作

`send_private_msg`、`send_group_msg`、`send_msg`、`delete_msg`、`get_msg`、`get_login_info`、`get_status`、`get_version_info`、`can_send_image`、`can_send_record`、`get_friend_list`、`get_stranger_info`、`get_group_list`、`get_group_info`、`get_group_member_list`、`get_group_member_info`、`set_group_leave`。

不支持的动作返回 `retcode 1404`。失败的动作返回 `status: "failed"`，并在 `wording` 里说明原因。

## 兼容性

任何能接受反向 WebSocket 连接的 OneBot v11 框架都应当可用。以下框架已用 `tests/e2e_framework.py` 在本地 Prosody 上验证：XMPP 用户发出的命令能收到框架自己的回复，单聊和群聊都通过。

| 框架 | 版本 | 反向 WebSocket 地址 | 说明 |
|---|---|---|---|
| AstrBot | 4.29.3 | `ws://127.0.0.1:6199/ws` | OneBot v11（aiocqhttp）适配器；OMEMO 和公网服务器测试也用它 |
| LangBot | 4.11.2 | `ws://127.0.0.1:2280/ws` | OneBot v11（aiocqhttp）适配器；默认只在群里被 @ 时回复 |
| NoneBot2 | 2.5.0（OneBot 适配器 2.4.6） | `ws://127.0.0.1:8080/onebot/v11/ws` | `nonebot-adapter-onebot`，FastAPI 驱动 |

验证日期 2026-10-29。尚未测试：OpenClaw、Hermes Agent（通过社区 OneBot v11 插件）和 Koishi（需要 Satori，尚未实现）。

## 快速开始

```bash
uv venv && uv pip install -e .            # 需要 OMEMO 时改为 ".[omemo]"
cp config.example.toml config.toml   # 填写 JID、密码、房间和反向 WS 地址
xmpp-gateway -c config.toml
```

在机器人框架里添加一个 OneBot v11 适配器，让它监听反向 WebSocket 连接，并把它的地址填到 `onebot_v11.reverse_ws`。

## Docker

```bash
docker build -t xmpp-bot-gateway .        # 加 --build-arg EXTRAS= 可去掉 OMEMO 依赖
mkdir -p data && sudo chown 10001 data    # 容器以 uid 10001 运行
docker run -d --name xmpp-gateway --restart unless-stopped \
  -v "$PWD/config.toml:/app/config.toml:ro" -v "$PWD/data:/app/data" xmpp-bot-gateway
```

配置文件从 `/app/config.toml` 读取；其中的相对路径（`data/...`）以 `/app` 为基准，所以数据库、OMEMO 密钥和媒体文件都落在 `data` 卷里。请备份这个目录：里面有机器人的 OMEMO 身份。网关只发起出站连接。框架跑在宿主机上时，可用 `--network host`，或加 `--add-host host.docker.internal:host-gateway` 后连 `ws://host.docker.internal:端口/...`。`docker stop` 会正常退出。Compose 用法见 `docker-compose.example.yml`。推送版本标签（`v*`）时会向 `ghcr.io/brokenstring314/xmpp-bot-gateway` 发布 amd64 和 arm64 镜像。

## 配置

所有设置都在一个 TOML 文件里；`config.example.toml` 列出了每一项及默认值。

- `xmpp-gateway -c config.toml --check` 只校验配置文件然后退出。未知配置项会直接报错，拼错的键不会被静默忽略。
- 任何字符串都可以用 `${NAME}` 读取环境变量，例如 `password = "${XMPP_PASSWORD}"`，密钥不必写进文件。
- `[log]` 设置日志级别，以及可选的按大小轮转的日志文件。
- `onebot_v11.reverse_ws` 的每一项可以是 URL，也可以是 `{ url = "...", access_token = "..." }`，多个框架可以用不同的 token 连接。
- 各种超时和退避（XMPP 重连、进群、媒体传输、OMEMO 会话重建间隔）也都可以配置。

## 测试

```bash
tests/prosody/run.sh &           # 本地 Prosody，127.0.0.1:15222，随机测试账号
python3 -m pytest -q tests/test_cq.py tests/test_props.py   # 单元测试和属性测试
python3 -u tests/e2e_redteam.py  # 恶意客户端发来的伪造、畸形和滥用节
FRAMEWORK=astrbot python3 -u tests/e2e_redteam_chain.py  # 恶意用户攻击真实 bot 框架（准备步骤见文件头）
python3 -u tests/e2e_chaos.py    # 机器人应用断线、服务器重启、双向突发（会重启 Prosody）
python3 -u tests/e2e_p0.py       # 对接模拟 OneBot v11 应用的端到端测试
python3 -u tests/e2e_media.py    # 媒体上传/下载，包括 OMEMO 加密文件
python3 -u tests/e2e_reply.py    # 回复、送达回执、提及、撤回通知
python3 -u tests/e2e_mam.py      # 通过 MAM 离线补拉
```

`tests/e2e_framework.py` 对一个正在运行的机器人框架跑单聊和群聊往返：`FRAMEWORK_WS` 设为它的反向 WebSocket 地址，`CMD` 设为它会回复的命令（默认 `/help`），可选 `GROUP_PREFIX`（如 `"bot: "` 表示 @ 机器人）和 `EXPECT`（回复必须包含的文字）。

`tests/e2e_omemo.py` 通过运行中的 AstrBot 检查 OMEMO：加密命令得到加密回复，且明文正文只有回退文字；随后的明文命令得到明文回复。`MODE=local` 用本地 Prosody，`MODE=public` 用 `.test_bots.json`。

`tests/e2e_public.py` 在公网 XMPP 服务器上运行：从 `.test_bots.json`（`{"jid", "password"}` 列表，用 `BOT_JID` / `USER_JID` 选角色）读取测试账号，检查单聊、邀请进新建房间、以及通过运行中的 AstrBot 的群聊。

## 状态

早期开发中。见 [docs/ROADMAP.zh-CN.md](docs/ROADMAP.zh-CN.md) 和 [docs/TESTING.zh-CN.md](docs/TESTING.zh-CN.md)。

## 许可证

[Apache License 2.0](LICENSE)。

可选的 `[omemo]` 附加依赖会安装 `slixmpp-omemo` 和 `oldmemo`，它们采用 AGPL-3.0 许可证，保留各自的许可：如果你把网关和它们一起分发，或带着它们作为网络服务运行，还需要满足 AGPL-3.0 的条款。
