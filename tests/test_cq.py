import sys
sys.path.insert(0, "src")
from xmpp_gateway.protocols.onebot_v11 import cq  # noqa: E402


def test_parse_roundtrip():
    s = "hi [CQ:at,qq=10000001] look &#91;x&#93; [CQ:image,file=https://a.b/c.png?x=1&#44;2]"
    segs = cq.parse(s)
    assert segs == [
        {"type": "text", "data": {"text": "hi "}},
        {"type": "at", "data": {"qq": "10000001"}},
        {"type": "text", "data": {"text": " look [x] "}},
        {"type": "image", "data": {"file": "https://a.b/c.png?x=1,2"}},
    ]
    assert cq.to_cq(segs) == s


def test_auto_escape_and_normalize():
    assert cq.normalize("[CQ:at,qq=1]", auto_escape=True) == [{"type": "text", "data": {"text": "[CQ:at,qq=1]"}}]
    assert cq.normalize({"type": "text", "data": {"text": "a"}}) == [{"type": "text", "data": {"text": "a"}}]
    assert cq.normalize("plain") == [{"type": "text", "data": {"text": "plain"}}]
