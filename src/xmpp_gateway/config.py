"""配置模型（pydantic）。"""
import os
import re
import tomllib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator


class Strict(BaseModel):
    """未知配置项直接报错（防止拼错的键被静默忽略）。"""
    model_config = ConfigDict(extra="forbid")


class XmppCfg(Strict):
    jid: str
    password: str
    server: str = ""
    port: int = 5222
    tls: Literal["starttls", "direct", "none"] = "starttls"  # starttls | direct | none（none 仅限本地测试）
    rooms: list[str] = []
    nick: str = "bot"
    # 收到群邀请时：roster = 只接受花名册联系人和 admins 的邀请；all = 接受任何人；none = 不自动加入
    accept_invites: str = "roster"
    admins: list[str] = []       # 受信任的 JID：accept_invites = "roster" 时也接受其群邀请
    rate_limit: int = 20         # 每个发送者每分钟最多推送的消息数，超出丢弃；0 不限
    max_body_chars: int = 10000  # 入站正文超过该长度时截断；0 不限
    mam_catchup: bool = True     # 上线/重连后用 MAM（XEP-0313）补拉离线期间的单聊与群聊消息
    mam_max_messages: int = 50   # 每个会话档案最多补拉条数
    mam_max_age: int = 86400     # s；只补拉这段时间内的消息
    reconnect_max: int = 60      # s；XMPP 断线重连最大退避
    status: str = ""             # 在线状态文字（presence status）
    join_timeout: int = 30       # s；加入群聊等待超时

    @field_validator("accept_invites", mode="before")
    @classmethod
    def _invites(cls, v):
        if isinstance(v, bool):   # 兼容旧配置 true/false
            return "all" if v else "none"
        if v not in ("roster", "all", "none"):
            raise ValueError("accept_invites must be roster, all or none")
        return v


class OmemoCfg(Strict):
    enabled: bool = False
    trust: Literal["btbv", "manual"] = "btbv"
    store_dir: str = "data/omemo"
    # 收到无法解密的消息（会话不同步）时，重建会话后用加密消息提示对方重发；留空则只重建不提示
    resync_notice: str = ("I couldn't decrypt your last message because our encryption session was out of "
                          "sync. It has been repaired, please send it again.")
    resync_cooldown: int = 300   # s；同一设备两次会话重建的最小间隔


class MediaCfg(Strict):
    dir: str = "data/media"          # 解密后的入站媒体（aesgcm://）保存目录
    max_size_mb: int = 20            # 上传/下载单个文件上限
    aesgcm_insecure_http: bool = False  # 仅本地测试：aesgcm:// 链接用 http 下载
    allow_private_hosts: bool = False   # 仅本地测试：允许下载内网/本机地址上的入站媒体（默认拒绝，防 SSRF）
    max_dir_mb: int = 500               # 入站媒体目录总大小上限，超出时删除最旧的文件；0 不限
    local_file_dirs: list[str] = []     # 出站 file:// / 本地路径只允许这些目录；空 = 不限制
    http_timeout: int = 60              # s；单次媒体下载/上传超时


class StorageCfg(Strict):
    db: str = "data/gateway.db"
    keep_days: int = 30   # 消息记录（get_msg / 回复 / 撤回用）保留天数；0 永久保留


class ReverseWs(Strict):
    url: str
    access_token: str | None = None   # 不填则用 [onebot_v11].access_token


class OneBotV11Cfg(Strict):
    enabled: bool = True
    # 每项可以是 URL 字符串，或 {url = "...", access_token = "..."}（不同框架用不同 token）
    reverse_ws: list[ReverseWs] = []
    access_token: str = ""
    heartbeat_interval: int = 5000   # ms；<=0 关闭心跳
    message_format: Literal["array", "string"] = "array"    # array | string（CQ 码）
    event_max_age: int = 60          # s；断线期间积压超过该时长的事件不再推送，0 不限
    reconnect_max: int = 30          # s；反向 WS 重连最大退避

    @field_validator("reverse_ws", mode="before")
    @classmethod
    def _ws(cls, v):
        return [{"url": x} if isinstance(x, str) else x for x in (v or [])]


class SatoriCfg(Strict):
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 5140
    token: str = ""


class LogCfg(Strict):
    level: str = "INFO"          # DEBUG | INFO | WARNING | ERROR
    file: str = ""               # 同时写入该文件（按大小轮转）；空 = 只输出到终端
    max_mb: int = 10             # 单个日志文件上限
    backups: int = 3             # 保留的旧日志文件个数

    @field_validator("level")
    @classmethod
    def _level(cls, v: str) -> str:
        v = v.upper()
        if v not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ValueError("log.level must be DEBUG, INFO, WARNING, ERROR or CRITICAL")
        return v


class Config(Strict):
    log: LogCfg = LogCfg()
    xmpp: XmppCfg
    omemo: OmemoCfg = OmemoCfg()
    media: MediaCfg = MediaCfg()
    storage: StorageCfg = StorageCfg()
    onebot_v11: OneBotV11Cfg = OneBotV11Cfg()
    satori: SatoriCfg = SatoriCfg()


_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(v: Any) -> Any:
    """字符串里的 ${NAME} 换成环境变量（如 password = "${XMPP_PASSWORD}"，密钥不必写进文件）。"""
    if isinstance(v, str):
        def sub(m: re.Match) -> str:
            if m.group(1) not in os.environ:
                raise ValueError(f"environment variable {m.group(1)} is not set")
            return os.environ[m.group(1)]
        return _ENV.sub(sub, v)
    if isinstance(v, dict):
        return {k: _expand(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_expand(x) for x in v]
    return v


def load_config(path: str) -> Config:
    with open(path, "rb") as f:
        return Config.model_validate(_expand(tomllib.load(f)))
