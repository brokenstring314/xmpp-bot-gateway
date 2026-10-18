# Roadmap

- **P0 打通**（已完成，见 tests/e2e_p0.py 与 tests/e2e_astrbot.py）：明文单聊 + MUC 群聊，OneBot v11 反向 WS、心跳/生命周期、常用动作、XEP-0424 撤回自己的消息、OOB 链接；Conversations 实机明文单聊已验证，Gajim 待测
- **P1 加密**（进行中）：✅ OMEMO 单聊收发（slixmpp-omemo；加密回复使用 `eu.siacs.conversations.axolotl`）、✅ BTBV / 手动信任、✅ 本地与公网 e2e。待做：`urn:xmpp:omemo:2` 加密发送（依赖 SCE）、密钥文件加密存储；✅ 桌面客户端实机加密互通已验证
- **P2 富消息**（已完成）：✅ 媒体收发（base64/file/http 来源，XEP-0363 上传 + XEP-0066 OOB；OMEMO 会话用 XEP-0454 aesgcm 加密上传/解密下载；e2e_media 本地与公网 10/10）；✅ 回复（XEP-0461 + XEP-0428 回退引用，双向，单聊/群聊）；✅ 自动送达回执（XEP-0184）；✅ @ 提及（昵称前缀 + XEP-0372 引用，双向）；✅ 撤回（XEP-0424：撤回自己的消息；对方撤回推送 friend_recall / group_recall）；✅ MAM 离线补拉（XEP-0313，单聊 + 群聊，去重、保留原始时间）；✅ 反向 WS 重连时补发未送达事件。P2 完成
- **第一阶段（P0–P2）已完成**；P3/P4 暂缓，先做质量阶段
- **质量阶段**（进行中）：
  - 已完成：静态扫描（ruff/bandit/pip-audit 无高危）；人工审计修复——入站媒体 SSRF（只许公网地址、连接时复查防 DNS rebinding、不跟随重定向）、分块下载截断 bug、媒体目录总量上限、群邀请默认只信任花名册/admins、伪造「群」消息（未加入房间）、MAM 结果来源校验、每发送者限流、超长正文截断、出站本地文件（拒绝 /dev/zero 等非普通文件、可限目录、防 ../ 和符号链接）、base64 先限长再解码、扩展名净化、后台任务异常不再静默；红队测试 e2e_redteam 14/14；属性/模糊测试 test_props；故障注入/压测 e2e_chaos 5/5（应用断线排队按序补发、服务器重启后重连重进群、入站 300 条按序 62 条/s、出站 200 条）；SQLite 改 WAL（入站吞吐约 3 倍）、消息记录按 storage.keep_days 清理；同一数据库单实例锁（防两个实例共用账号/OMEMO 存储）；GitHub Actions CI（失败时自动开 issue 附日志）；公网实测矩阵：yax.im、jabber.vg、chat.between-us.online、lain.rocks 两两互发，单聊 / 邀请入群 / 群聊 / 媒体 / OMEMO 全部通过（xmpp.party 已 DNS 失效，剔除）
  - 待做：长时间挂机（24h+）稳定性观察；真实 Conversations / Gajim 客户端回归（需要人）；OMEMO 首次接触旧设备时偶发首条解密失败的处理；类型检查（mypy）公网真实账号全面测试；安全审查（静态扫描 + 人工审计）；红队对抗测试（恶意客户端构造畸形/伪造 stanza）；单元/模糊/压力/长稳/故障注入测试；CI
- **P3 多协议**（暂缓）：Satori 接 Koishi；OneBot v11 正向 WS / HTTP API；OneBot 12 / Milky 视需求
- **P4**（暂缓）：OMEMO 群聊（非匿名 members-only 房间）、多账号、WebUI、Docker 发布

## 消息段映射

| OneBot v11 | XMPP |
|---|---|
| text | `<body>` |
| image / file | HTTP Upload + OOB |
| reply | XEP-0461 |
| at | MUC 昵称 / XEP-0372 |
| delete_msg | XEP-0424 |
| 好友 / 群列表 | Roster / MUC 书签 (XEP-0402) |

## 参考
- toughworm/Openclaw-XMPP-Plugin：功能清单、OMEMO 流程
- haydenwu/xmpp-onebot：OneBot 12 动作映射
- NapCat、lc-cn/onebots：配置与管理体验
