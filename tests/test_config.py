"""Config file parsing: strict keys, ${ENV} expansion, per-endpoint reverse WS tokens."""
import pytest

from xmpp_gateway.config import load_config


def write(tmp_path, text):
    p = tmp_path / "c.toml"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_example_config_is_valid():
    cfg = load_config("config.example.toml")
    assert cfg.log.level == "INFO" and cfg.xmpp.reconnect_max == 60


def test_unknown_key_rejected(tmp_path):
    with pytest.raises(ValueError, match="ratelimit"):
        load_config(write(tmp_path, '[xmpp]\njid="a@b"\npassword="p"\nratelimit=3\n'))


def test_env_expansion(tmp_path, monkeypatch):
    path = write(tmp_path, '[xmpp]\njid="a@b"\npassword="${GW_TEST_PW}"\n')
    with pytest.raises(ValueError, match="GW_TEST_PW"):
        load_config(path)
    monkeypatch.setenv("GW_TEST_PW", "s3cret")
    assert load_config(path).xmpp.password == "s3cret"


def test_reverse_ws_entries(tmp_path):
    cfg = load_config(write(tmp_path, '[xmpp]\njid="a@b"\npassword="p"\n[onebot_v11]\naccess_token="d"\n'
                                      'reverse_ws=["ws://x/ws", {url="ws://y/ws", access_token="t"}]\n'))
    eps = cfg.onebot_v11.reverse_ws
    assert [e.url for e in eps] == ["ws://x/ws", "ws://y/ws"]
    assert eps[0].access_token is None and eps[1].access_token == "t"


def test_log_level_validated(tmp_path):
    with pytest.raises(ValueError):
        load_config(write(tmp_path, '[log]\nlevel="LOUD"\n[xmpp]\njid="a@b"\npassword="p"\n'))
    assert load_config(write(tmp_path, '[log]\nlevel="debug"\n[xmpp]\njid="a@b"\npassword="p"\n')).log.level == "DEBUG"
