"""配置模型（pydantic）。"""
import tomllib
from pydantic import BaseModel


class XmppCfg(BaseModel):
    jid: str
    password: str
    server: str = ""
    port: int = 5222
    tls: str = "starttls"  # starttls | direct | none（none 仅限本地测试）
    rooms: list[str] = []
    nick: str = "bot"
    accept_invites: bool = True  # 收到群邀请时自动加入
    mam_catchup: bool = True     # 上线/重连后用 MAM（XEP-0313）补拉离线期间的单聊与群聊消息
    mam_max_messages: int = 50   # 每个会话档案最多补拉条数
    mam_max_age: int = 86400     # s；只补拉这段时间内的消息


class OmemoCfg(BaseModel):
    enabled: bool = False
    trust: str = "btbv"
    store_dir: str = "data/omemo"


class MediaCfg(BaseModel):
    dir: str = "data/media"          # 解密后的入站媒体（aesgcm://）保存目录
    max_size_mb: int = 20            # 上传/下载单个文件上限
    aesgcm_insecure_http: bool = False  # 仅本地测试：aesgcm:// 链接用 http 下载


class StorageCfg(BaseModel):
    db: str = "data/gateway.db"


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
