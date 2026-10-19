"""OMEMO（XEP-0384）接入：基于 slixmpp-omemo / python-omemo。

- 存储：每个账号一个 JSON 文件（store_dir/<bare_jid>.json），含身份密钥，务必妥善保管。
- 信任：trust = "btbv"（Blind Trust Before Verification，适合 bot）或 "manual"
  （不自动信任新设备，需要手动在存储里标记信任；未信任设备不会收到加密消息）。
"""
import json
import logging
import os
from pathlib import Path
from typing import Any, FrozenSet, Optional

from omemo.storage import Just, Maybe, Nothing, Storage
from omemo.types import DeviceInformation, JSONType
from slixmpp.plugins import register_plugin
from slixmpp_omemo import XEP_0384

log = logging.getLogger(__name__)


class JsonStorage(Storage):
    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path
        self._data: dict[str, JSONType] = {}
        try:
            self._data = json.loads(path.read_text(encoding="utf8"))
        except FileNotFoundError:
            pass

    def _flush(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data), encoding="utf8")
        os.chmod(tmp, 0o600)
        tmp.replace(self._path)

    async def _load(self, key: str) -> Maybe[JSONType]:
        return Just(self._data[key]) if key in self._data else Nothing()

    async def _store(self, key: str, value: JSONType) -> None:
        self._data[key] = value
        self._flush()

    async def _delete(self, key: str) -> None:
        self._data.pop(key, None)
        self._flush()


class GatewayOmemo(XEP_0384):
    """XEP-0384 实现；配置项 store_path、btbv 由 GatewayXMPP 传入。"""

    name = "xep_0384"
    default_config = {
        "fallback_message": "This message is OMEMO encrypted.",
        "store_path": None,
        "btbv": True,
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._storage: Storage

    def plugin_init(self) -> None:
        path = Path(self.store_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._storage = JsonStorage(path)
        super().plugin_init()

    @property
    def storage(self) -> Storage:
        return self._storage

    @property
    def _btbv_enabled(self) -> bool:
        return bool(self.btbv)

    async def _devices_blindly_trusted(self, blindly_trusted: FrozenSet[DeviceInformation],
                                       identifier: Optional[str]) -> None:
        for d in blindly_trusted:
            log.info("OMEMO: blindly trusted device %s of %s", d.device_id, d.bare_jid)

    async def _prompt_manual_trust(self, manually_trusted: FrozenSet[DeviceInformation],
                                   identifier: Optional[str]) -> None:
        # 无交互界面：manual 模式下保持「未决定」，这些设备不会被加密到
        for d in manually_trusted:
            log.warning("OMEMO: device %s of %s is not trusted (trust=manual); skipping it",
                        d.device_id, d.bare_jid)


register_plugin(GatewayOmemo)
