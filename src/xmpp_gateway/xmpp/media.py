"""媒体工具：类型判断、XEP-0454（aesgcm://）加解密、OneBot 媒体来源解析。"""
import base64
import hashlib
import io
import mimetypes
import os
from pathlib import Path
from urllib.parse import unquote, urlparse

from slixmpp.plugins.xep_0454 import XEP_0454

IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "heic", "avif"}
AUDIO_EXT = {"m4a", "ogg", "oga", "opus", "mp3", "aac", "amr", "wav", "flac"}
VIDEO_EXT = {"mp4", "webm", "mov", "mkv", "3gp"}


def ext_of(url_or_name: str) -> str:
    path = urlparse(url_or_name).path if "://" in url_or_name else url_or_name
    return Path(unquote(path)).suffix.lower().lstrip(".")


def kind_of(url_or_name: str) -> str | None:
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


def load_source(src: str, name: str | None, max_bytes: int) -> tuple[bytes, str, str]:
    """解析 base64:// 或 file:// / 本地路径，返回 (内容, 文件名, MIME)。"""
    if src.startswith("base64://"):
        data = base64.b64decode(src[len("base64://"):])
    else:
        path = unquote(urlparse(src).path) if src.startswith("file://") else src
        if os.path.getsize(path) > max_bytes:
            raise ValueError(f"file too large: {path}")
        data = Path(path).read_bytes()
        name = name or Path(path).name
    if len(data) > max_bytes:
        raise ValueError("file too large")
    if not name or "." not in name:
        name = f"{hashlib.sha256(data).hexdigest()[:16]}.{sniff_ext(data)}"
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return data, name, mime


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
