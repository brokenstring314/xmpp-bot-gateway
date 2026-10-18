# Roadmap

- **P0 打通**（已完成，见 tests/e2e_p0.py 与 tests/e2e_astrbot.py）：明文单聊 + MUC 群聊，OneBot v11 反向 WS、心跳/生命周期、常用动作、XEP-0424 撤回自己的消息、OOB 链接；Conversations 实机明文单聊已验证，Gajim 待测
- **P1 加密**（进行中）：✅ OMEMO 单聊收发（slixmpp-omemo；加密回复使用 `eu.siacs.conversations.axolotl`）、✅ BTBV / 手动信任、✅ 本地与公网 e2e。待做：`urn:xmpp:omemo:2` 加密发送（依赖 SCE）、密钥文件加密存储；✅ 桌面客户端实机加密互通已验证
- **P2 富消息**（进行中）：✅ 媒体收发（base64/file/http 来源，XEP-0363 上传 + XEP-0066 OOB；OMEMO 会话用 XEP-0454 aesgcm 加密上传/解密下载；e2e_media 本地与公网 10/10）；✅ 回复（XEP-0461 + XEP-0428 回退引用，双向，单聊/群聊）；✅ 自动送达回执（XEP-0184）。待做：@（XEP-0372）、撤回（XEP-0424）、MAM 补拉（XEP-0313）
- **P3 多协议**：Satori 接 Koishi，OneBot 12 / Milky 视需求
- **P4**：OMEMO 群聊（非匿名 members-only 房间）、多账号、WebUI、Docker 发布

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
