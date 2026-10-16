# xmpp-bot-gateway

XMPP 的 bot 协议端：就像 NapCat 之于 QQ。它登录一个 bot 的 JID，把 XMPP 消息翻译成标准 bot 协议，让 **AstrBot、NoneBot、Koishi** 或自研框架直接接入。目标是支持 **OMEMO** 端到端加密，与 Conversations、Gajim 互通。

```
Conversations / Gajim ⇄ XMPP 服务器 ⇄ xmpp-bot-gateway ⇄ OneBot v11 / Satori ⇄ AstrBot / Koishi / NoneBot
```

## 状态
P0 骨架阶段：明文单聊、群聊，OneBot v11 反向 WebSocket。详见 [docs/ROADMAP.md](docs/ROADMAP.md)。

## 快速开始
```bash
uv venv && uv pip install -e .
cp config.example.toml config.toml   # 填写 JID、密码、AstrBot 反向 WS 地址
xmpp-gateway -c config.toml
```

AstrBot：WebUI → 平台 → 添加适配器 → OneBot v11，把反向 WS 地址填进本项目 `onebot_v11.reverse_ws`。

## 为什么不是 OneBot 12
OneBot 12 实际生态很小；AstrBot 只支持 v11，Koishi 推 Satori，NoneBot2 两者都支持。所以顺序是 v11 → Satori → v12。
