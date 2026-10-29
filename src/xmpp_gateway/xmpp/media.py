"""媒体工具：类型判断、XEP-0454（aesgcm://）加解密、OneBot 媒体来源解析。"""
import base64
import asyncio
import hashlib
import io
import ipaddress
import mimetypes
import re
import socket
import stat
from pathlib import Path
from urllib.parse import unquote, urlparse

import aiohttp

from ..core.events import MediaKind
from aiohttp.abc import AbstractResolver

from slixmpp.plugins.xep_0454 import XEP_0454

IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "heic", "avif"}
AUDIO_EXT = {"m4a", "ogg", "oga", "opus", "mp3", "aac", "amr", "wav", "flac"}
VIDEO_EXT = {"mp4", "webm", "mov", "mkv", "3gp"}


def ext_of(url_or_name: str) -> str:
    path = urlparse(url_or_name).path if "://" in url_or_name else url_or_name
    ext = Path(unquote(path)).suffix.lower().lstrip(".")
    return ext if re.fullmatch(r"[a-z0-9]{1,8}", ext) else ""


def kind_of(url_or_name: str) -> MediaKind | None:
    """按扩展名判断 OneBot 消息段类型：image / record / video；其他返回 None。"""
    e = ext_of(url_or_name)
    if e in IMAGE_EXT:
        return "image"
    if e in AUDIO_EXT:
        return "record"
    if e in VIDEO_EXT:
        return "video"
    return None


def sniff_ext(data: bytes) -> str:
    """无文件名时按文件头猜扩展名。"""
    sigs = [(b"\x89PNG", "png"), (b"\xff\xd8\xff", "jpg"), (b"GIF8", "gif"), (b"OggS", "ogg"),
            (b"ID3", "mp3"), (b"#!AMR", "amr"), (b"BM", "bmp")]
    for sig, ext in sigs:
        if data.startswith(sig):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[4:8] == b"ftyp":
        return "m4a" if data[8:11] in (b"M4A", b"M4B") else "mp4"
    return "bin"


def load_source(src: str, name: str | None, max_bytes: int,
                allowed_dirs: list[str] | None = None) -> tuple[bytes, str, str]:
    """解析 base64:// 或 file:// / 本地路径，返回 (内容, 文件名, MIME)。
    allowed_dirs 非空时，本地文件必须位于其中某个目录下（按真实路径判断，防 ../ 和符号链接逃逸）。"""
    if src.startswith("base64://"):
        b64 = src[len("base64://"):]
        if len(b64) > max_bytes * 4 // 3 + 8:
            raise ValueError("file too large")
        data = base64.b64decode(b64)
    else:
        path = Path(unquote(urlparse(src).path) if src.startswith("file://") else src).resolve()
        if allowed_dirs and not any(path.is_relative_to(Path(d).resolve()) for d in allowed_dirs):
            raise ValueError(f"local file outside allowed directories: {path}")
        st = path.stat()
        if not stat.S_ISREG(st.st_mode):   # 拒绝 /dev/zero、FIFO 等
            raise ValueError(f"not a regular file: {path}")
        if st.st_size > max_bytes:
            raise ValueError(f"file too large: {path}")
        with path.open("rb") as f:
            data = f.read(max_bytes + 1)
        name = name or path.name
    if len(data) > max_bytes:
        raise ValueError("file too large")
    if not name or "." not in name:
        name = f"{hashlib.sha256(data).hexdigest()[:16]}.{sniff_ext(data)}"
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return data, name, mime


def check_public_url(url: str) -> None:
    """拒绝主机名为内网/本机 IP 字面量的链接（域名在连接时由 PublicResolver 检查）。"""
    host = urlparse(url).hostname or ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if not host:
            raise ValueError("url has no host") from None
        return
    if not ip.is_global:
        raise ValueError(f"refusing non-public address {ip}")


async def is_public_url(url: str, timeout: float = 5.0) -> bool:
    """链接的主机是否只解析到公网地址（IP 字面量直接判断，域名做一次 DNS 解析；解析失败视为否）。"""
    try:
        check_public_url(url)
        host = urlparse(url).hostname or ""
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            pass
        infos = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, None), timeout)
    except (ValueError, OSError, asyncio.TimeoutError):
        return False
    return bool(infos) and all(ipaddress.ip_address(str(i[4][0]).split("%")[0]).is_global for i in infos)


class PublicResolver(AbstractResolver):
    """只放行公网地址的 DNS 解析器：在真正连接时检查，防 SSRF 与 DNS rebinding。"""

    def __init__(self) -> None:
        self._r = aiohttp.ThreadedResolver()

    async def resolve(self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET):
        infos = await self._r.resolve(host, port, family)
        for i in infos:
            ip = ipaddress.ip_address(i["host"])
            if not ip.is_global:
                raise OSError(f"refusing non-public address {ip} for {host}")
        return infos

    async def close(self) -> None:
        await self._r.close()


async def download(url: str, limit: int, allow_private: bool = False) -> bytes:
    """下载入站媒体：限制大小、不跟随重定向；默认只允许公网地址。"""
    if not allow_private:
        check_public_url(url)
    conn = aiohttp.TCPConnector(resolver=PublicResolver()) if not allow_private else aiohttp.TCPConnector()
    async with aiohttp.ClientSession(connector=conn, timeout=aiohttp.ClientTimeout(total=60)) as http:
        async with http.get(url, allow_redirects=False) as r:
            r.raise_for_status()
            if r.status != 200:
                raise ValueError(f"unexpected HTTP status {r.status}")
            if int(r.headers.get("Content-Length") or 0) > limit:
                raise ValueError("file too large")
            buf = bytearray()
            async for chunk in r.content.iter_chunked(65536):
                buf += chunk
                if len(buf) > limit:
                    raise ValueError("file too large")
    return bytes(buf)


def prune_dir(d: Path, max_bytes: int) -> None:
    """目录总大小超过上限时按修改时间删除最旧的文件。"""
    files = [(p.stat().st_mtime, p.stat().st_size, p) for p in d.iterdir() if p.is_file()]
    total = sum(s for _, s, _ in files)
    for _, size, p in sorted(files):
        if total <= max_bytes:
            break
        p.unlink(missing_ok=True)
        total -= size


def aesgcm_encrypt(data: bytes) -> tuple[bytes, str]:
    """返回 (密文+tag, URL 片段 iv+key 十六进制)。"""
    return XEP_0454.encrypt(input_file=io.BytesIO(data))


def aesgcm_decrypt(payload: bytes, fragment: str) -> bytes:
    return XEP_0454.decrypt(io.BytesIO(payload), fragment)


def aesgcm_url(https_url: str, fragment: str) -> str:
    return "aesgcm://" + https_url.split("://", 1)[1] + "#" + fragment


def aesgcm_split(url: str, insecure_http: bool = False) -> tuple[str, str]:
    """aesgcm://host/path#frag -> (https://host/path, frag)。"""
    rest, _, frag = url[len("aesgcm://"):].partition("#")
    return ("http://" if insecure_http else "https://") + rest, frag
