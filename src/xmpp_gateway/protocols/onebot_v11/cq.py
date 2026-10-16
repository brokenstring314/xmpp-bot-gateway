"""OneBot v11 消息格式工具：CQ 码字符串 <-> 消息段数组。"""
import re

_CQ = re.compile(r"\[CQ:([a-zA-Z0-9_.-]+)((?:,[^,\]]*?=[^,\]]*)*)\]")


def escape(text: str, in_param: bool = False) -> str:
    text = text.replace("&", "&amp;").replace("[", "&#91;").replace("]", "&#93;")
    return text.replace(",", "&#44;") if in_param else text


def unescape(text: str) -> str:
    return text.replace("&#44;", ",").replace("&#91;", "[").replace("&#93;", "]").replace("&amp;", "&")


def parse(msg: str, auto_escape: bool = False) -> list[dict]:
    """CQ 码字符串 -> 消息段数组。auto_escape=True 时整体当纯文本。"""
    if auto_escape:
        return [{"type": "text", "data": {"text": msg}}]
    out, pos = [], 0
    for m in _CQ.finditer(msg):
        if m.start() > pos:
            out.append({"type": "text", "data": {"text": unescape(msg[pos:m.start()])}})
        data = {}
        for kv in filter(None, m.group(2).split(",")):
            k, _, v = kv.partition("=")
            data[k] = unescape(v)
        out.append({"type": m.group(1), "data": data})
        pos = m.end()
    if pos < len(msg):
        out.append({"type": "text", "data": {"text": unescape(msg[pos:])}})
    return out


def normalize(message, auto_escape: bool = False) -> list[dict]:
    """接受字符串 / 单个消息段 / 消息段数组，统一成数组。"""
    if isinstance(message, str):
        return parse(message, auto_escape)
    if isinstance(message, dict):
        return [message]
    return list(message or [])


def to_cq(segments: list[dict]) -> str:
    parts = []
    for s in segments:
        if s["type"] == "text":
            parts.append(escape(s["data"].get("text", "")))
        else:
            params = "".join(f",{k}={escape(str(v), True)}" for k, v in s.get("data", {}).items())
            parts.append(f"[CQ:{s['type']}{params}]")
    return "".join(parts)
