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


class OmemoCfg(BaseModel):
    enabled: bool = False
    trust: str = "btbv"
    store_dir: str = "data/omemo"


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
    storage: StorageCfg = StorageCfg()
    onebot_v11: OneBotV11Cfg = OneBotV11Cfg()
    satori: SatoriCfg = SatoriCfg()


def load_config(path: str) -> Config:
    with open(path, "rb") as f:
        return Config.model_validate(tomllib.load(f))
