# Roadmap

- **P0 打通**：明文单聊 + MUC 群聊，OneBot v11 反向 WS 接 AstrBot；Conversations / Gajim 实测
- **P1 加密**：OMEMO 单聊（兼容 `eu.siacs.conversations.axolotl` 与 `urn:xmpp:omemo:2`）、密钥加密持久化、信任策略（BTBV / 手动）
- **P2 富消息**：图片/文件（XEP-0363 HTTP Upload + XEP-0066 OOB）、回复（XEP-0461）、@（XEP-0372）、撤回（XEP-0424）、回执（XEP-0184）、MAM 补拉（XEP-0313）
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
