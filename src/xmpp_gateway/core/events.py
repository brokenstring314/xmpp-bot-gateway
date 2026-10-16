"""平台无关的统一事件模型。XMPP 层产出它，各协议出口只负责翻译。"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class Scene(str, Enum):
    PRIVATE = "private"   # 单聊 (type=chat)
    GROUP = "group"       # 群聊 (MUC, type=groupchat)


@dataclass
class Segment:
    type: Literal["text", "image", "file", "reply", "at"]  # at.data: {"target": JID}
    data: dict = field(default_factory=dict)


@dataclass
class MessageEvent:
    scene: Scene
    self_jid: str
    sender_jid: str            # 单聊为 bare JID；群聊为 room@conf/nick 或真实 JID（非匿名房间）
    room_jid: str | None       # 群聊时的房间 bare JID
    message_id: str            # XMPP stanza id / origin-id
    segments: list[Segment]
    encrypted: bool = False    # 是否经 OMEMO 解密
    time: float = 0.0
    sender_name: str = ""      # 单聊：花名册名称或 localpart；群聊：房间昵称
    sender_role: str = "none"  # 群聊：MUC affiliation（owner/admin/member/none/outcast）
