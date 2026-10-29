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


def test_enum_values_validated(tmp_path):
    for section, line in (("xmpp", 'tls="ssl"'), ("onebot_v11", 'message_format="json"'), ("omemo", 'trust="yes"'),
                          ("xmpp", 'accept_invites="friends"')):
        extra = f"[{section}]\n{line}\n" if section != "xmpp" else ""
        body = f'[xmpp]\njid="a@b"\npassword="p"\n{line if section == "xmpp" else ""}\n{extra}'
        with pytest.raises(ValueError):
            load_config(write(tmp_path, body))


@pytest.fixture
def clean_root_logger():
    import logging
    root = logging.getLogger()
    saved = (root.level, list(root.handlers))
    yield root
    for h in root.handlers[:]:
        if h not in saved[1]:
            h.close()
            root.removeHandler(h)
    root.setLevel(saved[0])


def test_log_level_and_file(tmp_path, clean_root_logger):
    import logging
    from xmpp_gateway.__main__ import setup_logging
    from xmpp_gateway.config import LogCfg
    f = tmp_path / "logs" / "gw.log"
    setup_logging(LogCfg(level="WARNING", file=str(f)))
    logging.getLogger("xmpp_gateway.t").info("quiet-info")
    logging.getLogger("xmpp_gateway.t").warning("loud-warning")
    for h in clean_root_logger.handlers:
        h.flush()
    text = f.read_text(encoding="utf-8")
    assert "loud-warning" in text and "quiet-info" not in text


def test_log_rotation(tmp_path, clean_root_logger):
    import logging
    from xmpp_gateway.__main__ import setup_logging
    from xmpp_gateway.config import LogCfg
    f = tmp_path / "gw.log"
    setup_logging(LogCfg(level="INFO", file=str(f), max_mb=1, backups=2))
    line = "x" * 1000
    for _ in range(3500):                     # ~3.5 MB through a 1 MB file with 2 backups
        logging.getLogger("xmpp_gateway.t").info(line)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["gw.log", "gw.log.1", "gw.log.2"], names
    assert all(p.stat().st_size <= 1024 * 1024 + 2000 for p in tmp_path.iterdir())


def test_log_terminal_only(tmp_path, clean_root_logger):
    from xmpp_gateway.__main__ import setup_logging
    from xmpp_gateway.config import LogCfg
    import logging
    before = len(clean_root_logger.handlers)
    setup_logging(LogCfg())
    added = clean_root_logger.handlers[before:]
    assert len(added) == 1 and not isinstance(added[0], logging.FileHandler)
