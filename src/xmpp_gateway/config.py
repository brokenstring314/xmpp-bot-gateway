"""配置模型（pydantic）。"""
import tomllib
from pydantic import BaseModel, field_validator


class XmppCfg(BaseModel):
    jid: str
    password: str
    server: str = ""
    port: int = 5222
    tls: str = "starttls"  # starttls | direct | none（none 仅限本地测试）
    rooms: list[str] = []
    nick: str = "bot"
    # 收到群邀请时：roster = 只接受花名册联系人和 admins 的邀请；all = 接受任何人；none = 不自动加入
    accept_invites: str = "roster"
    admins: list[str] = []       # 受信任的 JID（总是接受其群邀请）
    rate_limit: int = 20         # 每个发送者每分钟最多推送的消息数，超出丢弃；0 不限
    max_body_chars: int = 10000  # 入站正文超过该长度时截断；0 不限
    mam_catchup: bool = True     # 上线/重连后用 MAM（XEP-0313）补拉离线期间的单聊与群聊消息
    mam_max_messages: int = 50   # 每个会话档案最多补拉条数
    mam_max_age: int = 86400     # s；只补拉这段时间内的消息

    @field_validator("accept_invites", mode="before")
    @classmethod
    def _invites(cls, v):
        if isinstance(v, bool):   # 兼容旧配置 true/false
            return "all" if v else "none"
        if v not in ("roster", "all", "none"):
            raise ValueError("accept_invites must be roster, all or none")
        return v


class OmemoCfg(BaseModel):
    enabled: bool = False
    trust: str = "btbv"
    store_dir: str = "data/omemo"
    # 收到无法解密的消息（会话不同步）时，重建会话后用加密消息提示对方重发；留空则只重建不提示
    resync_notice: str = ("I couldn't decrypt your last message because our encryption session was out of "
                          "sync. It has been repaired, please send it again.")


class MediaCfg(BaseModel):
    dir: str = "data/media"          # 解密后的入站媒体（aesgcm://）保存目录
    max_size_mb: int = 20            # 上传/下载单个文件上限
    aesgcm_insecure_http: bool = False  # 仅本地测试：aesgcm:// 链接用 http 下载
    allow_private_hosts: bool = False   # 仅本地测试：允许下载内网/本机地址上的入站媒体（默认拒绝，防 SSRF）
    max_dir_mb: int = 500               # 入站媒体目录总大小上限，超出时删除最旧的文件；0 不限
    local_file_dirs: list[str] = []     # 出站 file:// / 本地路径只允许这些目录；空 = 不限制


class StorageCfg(BaseModel):
    db: str = "data/gateway.db"
    keep_days: int = 30   # 消息记录（get_msg / 回复 / 撤回用）保留天数；0 永久保留


class OneBotV11Cfg(BaseModel):
    enabled: bool = True
    reverse_ws: list[str] = []
    access_token: str = ""
    heartbeat_interval: int = 5000   # ms；<=0 关闭心跳
    message_format: str = "array"    # array | string（CQ 码）
    event_max_age: int = 60          # s；断线期间积压超过该时长的事件不再推送，0 不限
    reconnect_max: int = 30          # s；反向 WS 重连最大退避


class SatoriCfg(BaseModel):
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 5140
    token: str = ""


class Config(BaseModel):
    xmpp: XmppCfg
    omemo: OmemoCfg = OmemoCfg()
    media: MediaCfg = MediaCfg()
    storage: StorageCfg = StorageCfg()
    onebot_v11: OneBotV11Cfg = OneBotV11Cfg()
    satori: SatoriCfg = SatoriCfg()


def load_config(path: str) -> Config:
    with open(path, "rb") as f:
        return Config.model_validate(tomllib.load(f))
