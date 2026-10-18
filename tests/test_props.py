"""Property-based / fuzz tests for the pure helpers (no network)."""
import os
import sys
import tempfile

import pytest
from hypothesis import given, settings, strategies as st

sys.path.insert(0, "src")
from xmpp_gateway.protocols.onebot_v11 import cq  # noqa: E402
from xmpp_gateway.xmpp import media  # noqa: E402
from xmpp_gateway.xmpp.client import GatewayXMPP  # noqa: E402

text = st.text(min_size=1)
seg = st.one_of(
    st.builds(lambda t: {"type": "text", "data": {"text": t}}, text),
    st.builds(lambda q: {"type": "at", "data": {"qq": q}}, st.text(min_size=1)),
    st.builds(lambda f: {"type": "image", "data": {"file": f}}, st.text(min_size=1)),
)


def merge(segs):
    """相邻 text 段在 CQ 字符串里无法区分，比较前合并。"""
    out = []
    for s in segs:
        if s["type"] == "text" and out and out[-1]["type"] == "text":
            out[-1] = {"type": "text", "data": {"text": out[-1]["data"]["text"] + s["data"]["text"]}}
        else:
            out.append(s)
    return out


@given(st.lists(seg, max_size=8))
def test_cq_roundtrip(segs):
    assert cq.parse(cq.to_cq(segs)) == merge(segs)


@given(st.text())
def test_cq_parse_never_crashes_and_text_survives_escape(s):
    cq.parse(s)
    assert cq.parse(cq.escape(s)) == ([{"type": "text", "data": {"text": s}}] if s else [])


@given(st.text())
def test_ext_of_is_safe(s):
    e = media.ext_of(s)
    assert e == "" or (e.isalnum() and e.isascii() and len(e) <= 8 and "/" not in e)


@pytest.mark.parametrize("url,bad", [
    ("https://127.0.0.1/x", True), ("https://10.1.2.3/x", True), ("https://[::1]/x", True),
    ("https://169.254.169.254/x", True), ("https://[::ffff:127.0.0.1]/x", True),
    ("https://0.0.0.0/x", True), ("https://192.168.1.1/x", True), ("https://8.8.8.8/x", False),
    ("https://example.org/x", False),
])
def test_check_public_url(url, bad):
    if bad:
        with pytest.raises(ValueError):
            media.check_public_url(url)
    else:
        media.check_public_url(url)


def test_load_source_limits():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "a.png")
        open(p, "wb").write(b"\x89PNG" + b"0" * 100)
        data, name, mime = media.load_source(p, None, 1000, [d])
        assert name == "a.png" and mime == "image/png"
        with pytest.raises(ValueError):
            media.load_source(p, None, 10)
        with pytest.raises(ValueError):
            media.load_source("/etc/hostname", None, 1000, [d])
        os.symlink("/etc/hostname", os.path.join(d, "link.txt"))
        with pytest.raises(ValueError):   # 符号链接逃逸
            media.load_source(os.path.join(d, "link.txt"), None, 1000, [d])
    with pytest.raises(ValueError):
        media.load_source("/dev/zero", None, 1000)
    with pytest.raises(ValueError):
        media.load_source("base64://" + "A" * 2000, None, 100)


class _Msg:
    """_take_reply 只用到 msg.xml。"""
    def __init__(self, xml):
        self.xml = xml


@settings(max_examples=300)
@given(st.text(max_size=50), st.text(max_size=12), st.text(max_size=12), st.text(max_size=20))
def test_take_reply_fuzz(body, start, end, rid):
    import xml.etree.ElementTree as ET
    m = ET.Element("{jabber:client}message")
    ET.SubElement(m, "{urn:xmpp:reply:0}reply", {"id": rid})
    fb = ET.SubElement(m, "{urn:xmpp:fallback:0}fallback", {"for": "urn:xmpp:reply:0"})
    ET.SubElement(fb, "{urn:xmpp:fallback:0}body", {"start": start, "end": end})
    out, r = GatewayXMPP._take_reply(_Msg(m), body)
    if r is not None:
        assert out + r.data["fallback"] == body or len(out) + len(r.data["fallback"]) == len(body)


def test_idmap_prune():
    import asyncio
    import time as _t
    from xmpp_gateway.core.idmap import IdMap

    async def run():
        with tempfile.TemporaryDirectory() as d:
            m = IdMap(os.path.join(d, "g.db"), keep_days=1)
            await m.open()
            old = await m.save_message(scene="private", chat="a@x", sender="a@x", sender_name="", xmpp_id="o1,o2",
                                       outgoing=False, segments=[], ts=_t.time() - 3 * 86400)
            new = await m.save_message(scene="private", chat="a@x", sender="a@x", sender_name="", xmpp_id="n1",
                                       outgoing=False, segments=[])
            uid = await m.to_int("user", "a@x")
            await m.prune()
            assert await m.get_message(old) is None and await m.find_message("a@x", "o2") is None
            assert await m.get_message(new) is not None and await m.find_message("a@x", "n1") == new
            assert await m.to_int("user", "a@x") == uid   # 用户/群 ID 永不删除
            await m.close()
    asyncio.run(run())


def test_single_instance_lock():
    import asyncio
    from xmpp_gateway.__main__ import Gateway

    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "c.toml")
        open(cfg, "w").write(f'[xmpp]\njid = "a@b"\npassword = "x"\n[storage]\ndb = "{d}/g.db"\n')
        a, b = Gateway(cfg), Gateway(cfg)
        a._lock()
        with pytest.raises(SystemExit):
            b._lock()
        os.close(a._lock_fd)
        b._lock()
        os.close(b._lock_fd)
