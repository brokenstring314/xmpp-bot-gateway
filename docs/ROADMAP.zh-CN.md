# 路线图

[English](ROADMAP.md)

当前已支持的功能清单见 [FEATURES.zh-CN.md](FEATURES.zh-CN.md)。

- **P0 打通**（已完成，见 tests/e2e_p0.py 与 tests/e2e_framework.py）：明文单聊 + MUC 群聊，OneBot v11 反向 WS、心跳/生命周期、常用动作、XEP-0424 撤回自己的消息、OOB 链接；Conversations 实机明文单聊已验证，Gajim 待测
- **P1 加密**（进行中）：✅ OMEMO 单聊收发（slixmpp-omemo；加密回复使用 `eu.siacs.conversations.axolotl`）、✅ BTBV / 手动信任、✅ 本地与公网 e2e。待做：`urn:xmpp:omemo:2` 加密发送（依赖 SCE）、密钥文件加密存储；✅ 移动端和桌面客户端实机加密互通已验证
- **P2 富消息**（已完成）：✅ 媒体收发（base64/file/http 来源，XEP-0363 上传 + XEP-0066 OOB；OMEMO 会话用 XEP-0454 aesgcm 加密上传/解密下载；e2e_media 本地与公网 10/10）；✅ 回复（XEP-0461 + XEP-0428 回退引用，双向，单聊/群聊）；✅ 自动送达回执（XEP-0184）；✅ @ 提及（昵称前缀 + XEP-0372 引用，双向）；✅ 撤回（XEP-0424：撤回自己的消息；对方撤回推送 friend_recall / group_recall）；✅ MAM 离线补拉（XEP-0313，单聊 + 群聊，去重、保留原始时间）；✅ 反向 WS 重连时补发未送达事件。P2 完成
- **第一阶段（P0–P2）已完成**，P1 里仍列为待做的两项除外；P3/P4 暂缓，先做质量阶段
- **质量阶段**（进行中）：
  - 已完成：静态扫描（ruff/bandit/pip-audit 无高危）；人工审计修复——入站媒体 SSRF（只许公网地址、连接时复查防 DNS rebinding、不跟随重定向）、分块下载截断 bug、媒体目录总量上限、群邀请默认只信任花名册/admins、伪造「群」消息（未加入房间）、MAM 结果来源校验、每发送者限流、超长正文截断、出站本地文件（拒绝 /dev/zero 等非普通文件、可限目录、防 ../ 和符号链接）、base64 先限长再解码、扩展名净化、后台任务异常不再静默；红队测试 e2e_redteam 14/14；属性/模糊测试 test_props；故障注入/压测 e2e_chaos 5/5（应用断线排队按序补发、服务器重启后重连重进群、入站 300 条按序 62 条/s、出站 200 条）；SQLite 改 WAL（入站吞吐约 3 倍）、消息记录按 storage.keep_days 清理；同一数据库单实例锁（防两个实例共用账号/OMEMO 存储）；GitHub Actions CI（失败时自动开 issue 附日志）；公网实测矩阵：yax.im、jabber.vg、chat.between-us.online、lain.rocks 两两互发，单聊 / 邀请入群 / 群聊 / 媒体 / OMEMO 全部通过（xmpp.party 已 DNS 失效，剔除）
  - 第二轮（2026-10-19）：mypy 全量通过并加入 CI（修了消息段类型漏掉 record/video 等 19 处）；OMEMO 会话不同步时不再永久丢消息——检测到无法解密会重建会话并加密提示对方重发（e2e_omemo_resync 8/8：首次接触、重复投递不重复解密、会话丢失后自动恢复）；重复到达的加密消息在解密前去重；心跳任务异常不再报 "never retrieved"；单实例锁支持 Windows；24h 挂机脚本 tests/soak.py 与真机回归清单 docs/TESTING.md
  - 第三轮（2026-10-26）：e2e_omemo_resync 扩到 14/14（新增会话错乱——棘轮状态与对方不一致、对方重装客户端换新设备与新身份密钥）；按项目依赖单独建环境跑 pip-audit 无已知漏洞；e2e 全套覆盖率 89%；修复停止时的竞态：stop() 现在先停收消息与补拉、等连接真正关闭再关数据库，关闭途中到达的消息留给下次 MAM 补拉（CI 上 e2e_mam 偶发丢第一条即此原因）（tests/cov_run.py，client 89%、OneBot 服务端 88%、OMEMO 96%）
  - 24h 挂机（2026-10-26～27，Debian 13 / Python 3.13.5，两个公网服务器账号，OMEMO 与明文交替每分钟一条）：1440 条回了 1435 条，丢的 5 条都落在测试端自己重连的两个时间窗内；网关全程 1 个 XMPP 会话、0 错误；RSS 84.9→86.8 MB、打开文件 16→16；数据库+WAL 首 4 小时到约 4 MB（WAL 填满）后 20 小时只涨 0.75 MB；往返中位 0.59s、p95 0.83s
  - 真机回归（2026-10-28）：因测试服务器磁盘不足，未部署 AstrBot，完整 10 项清单未执行，暂不再做。已在真实客户端验证过的：Conversations 明文与 OMEMO 单聊 !help、桌面客户端 OMEMO 互通
  - 待真机验证（自动化测试已通过，真实客户端未点过）：语音消息、对方撤回通知、群里 @ 机器人回复、停机后 MAM 补收；Gajim 全部项目
  - 框架兼容（2026-10-29）：AstrBot 4.29.3、LangBot 4.11.2、NoneBot2 2.5.0 单聊和群聊往返均通过（tests/e2e_framework.py）。优先级：第一梯队 AstrBot、LangBot、NoneBot2；第二梯队 OpenClaw、Hermes Agent（通过社区 OneBot v11 插件）和 Koishi（需要 Satori）；第二梯队暂缓。
  - 第四轮：第一梯队框架整链红队测试（2026-10-29）：发现并修复两个问题。（1）在隐藏真实 JID 的房间里，成员的 user_id 来自「房间/昵称」，原成员离开后任何人改用同一昵称就拿到同一个 user_id（以及框架绑定在它上面的权限或用户状态）。现在优先用可见的真实 JID，否则用服务器给的 XEP-0421 occupant-id（仅当房间声明支持时才信任；客户端自带的 id 会被服务器剥掉）；按昵称识别只作最后兜底并记录警告。发给这类成员的私信会发到其当前昵称。（2）入站媒体链接即使指向内网或本机地址，也会作为 image/record/video 段交给框架，框架或插件去下载时就成了 SSRF 跳板；现在这类链接只作为纯文本传给框架。e2e_redteam 19/19（旧代码上有 3 项失败）。新增 tests/e2e_redteam_chain.py，连真实框架测试（攻击者在单聊、冒用管理员昵称的群、昵称被抢占后的匿名房间里发管理员指令；管理员指令仍可用；内网媒体链接金丝雀；CQ 码注入；刷屏）：AstrBot 4.29.3 11/11，NoneBot2 2.5.0 11/11，LangBot 4.11.2 6/6（没有内置的管理员指令，跳过权限项）。
  - 配置（2026-10-29）：剩余写死的参数改为可配置：`[log]` 日志级别与轮转日志文件，XMPP `reconnect_max`、`join_timeout`、在线状态 `status`，OMEMO `resync_cooldown`，媒体 `http_timeout`；`reverse_ws` 每项可单独设 `access_token`；任何字符串支持 `${NAME}` 环境变量；未知配置项直接报错；`--check` 只校验配置文件。新增 `tests/test_config.py`（已进 CI）。网页配置界面暂不考虑。
  - 配置分支测试（2026-10-29）：`tests/e2e_config.py`（45 项）与 `tests/e2e_config_omemo.py`（14 项）在本地 Prosody 上逐项验证每个配置，含用测试 CA 的 STARTTLS / 直连 TLS（已进 CI）。由此修复：加入超时的房间仍被当作已加入，其群消息会被接受；MAM 补拉丢弃早于请求起点的归档（Prosody 13 不认带小数秒的 `start`，会重放整个归档并绕过 `mam_max_age`）；`tls`、OMEMO `trust`、`message_format` 取值做校验；`[satori] enabled = true` 会提示 Satori 尚未实现。
  - 待做：定期复查依赖漏洞（下次 2026-11 底）
- **Docker**（2026-10-30）：`Dockerfile`（多阶段构建，非 root uid 10001，默认含 OMEMO 依赖，`EXTRAS=` 可去掉）、`docker-compose.example.yml`，收到 SIGTERM 正常退出。`tests/docker_smoke.py`（9 项：`--check`、带 OMEMO 启动、收发往返、状态写入数据卷、SIGTERM 停止、同一数据重启）在 CI 中对构建出的镜像运行。推送版本标签时向 GHCR 发布 amd64/arm64 镜像。
- **v0.1.0**（2026-10-30）：首个版本标签（P0–P2、质量阶段、Docker）。另外补充了包元数据（关键词、分类、项目链接）和 DOAP 项目描述（`xmpp-bot-gateway.doap`，XEP-0453）。许可证与商标复查：Docker 镜像标注为 `Apache-2.0 AND AGPL-3.0-only`，README 说明镜像的 AGPL 条款，并加入商标与无隶属声明。
- **P3 多协议**（暂缓）：Satori 接 Koishi；OneBot v11 正向 WS / HTTP API；OneBot 12 / Milky 视需求
- **P4**（暂缓）：OMEMO 群聊（非匿名 members-only 房间）、多账号、WebUI

## 消息段映射

| OneBot v11 | XMPP |
|---|---|
| text | `<body>` |
| image / file | HTTP Upload + OOB |
| reply | XEP-0461 |
| at | MUC 昵称 / XEP-0372 |
| delete_msg | XEP-0424 |
| 好友 / 群列表 | Roster / 已加入的 MUC 房间 |
