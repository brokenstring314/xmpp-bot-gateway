# Testing

[简体中文](TESTING.zh-CN.md)

## Automated tests (run by CI on every push)

```bash
bash tests/prosody/run.sh &          # local Prosody (127.0.0.1:15222)
python -m mypy                       # type check
python -m pytest -q tests/test_cq.py tests/test_props.py tests/test_config.py
for t in p0 media reply mam omemo_resync redteam config config_omemo chaos; do python -u tests/e2e_$t.py; done
# Docker job: build the image, then IMAGE=<tag> python -u tests/docker_smoke.py
# with coverage: replace `python -u` by `python -u tests/cov_run.py`, then `coverage combine && coverage report`
```

## 24-hour soak (`tests/soak.py`)

The gateway runs as a subprocess. A mock bot app inside the script echoes every direct message, and a test account sends a ping every minute (alternating plaintext and OMEMO). The script records round-trip time, losses, gateway memory, open files, database size and reconnects.

Requirements: Linux, WSL2 or macOS, Python 3.11+, outbound internet access; the machine must not sleep during the test.

```bash
git clone https://github.com/brokenstring314/xmpp-bot-gateway.git
cd xmpp-bot-gateway
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[omemo]" tomli-w
# put soak.toml (two test accounts) in the repository root
python3 tests/soak.py -c soak.toml --hours 24
```

Results go to `soak-<time>/`: `report.md` is the summary, `pings.csv`, `samples.csv` and `gateway.log` hold the details. `report.md` is refreshed every 5 minutes, so an interrupted run still leaves results.

## Manual client checklist (Conversations / Gajim)

Prerequisites: the gateway and a bot framework are running, and your account has the bot as a contact. `!help` stands for any command your framework answers. Check the bot's reaction for each item and take a screenshot when something is wrong.

| # | Action | Expected |
|---|---|---|
| 1 | Send `!help` in plaintext | Help text arrives |
| 2 | Turn OMEMO on, send `!help` | Reply is encrypted (lock shown) |
| 3 | Turn OMEMO off, send `!help` | Reply is not encrypted |
| 4 | Send an image (once with OMEMO on, once off) | Bot receives the image; a local file when encrypted |
| 5 | Send a voice message | Bot receives a record segment |
| 6 | Reply to one of the bot's messages with `!help` | Normal reply; the quote is not treated as part of the command |
| 7 | Send a message and retract it right away | Bot receives a recall notice |
| 8 | Create a group and invite the bot | Bot joins automatically |
| 9 | In the group, `@bot !help` | Reply in the group, mentioning you |
| 10 | Stop the gateway for a minute, send `!help`, start it again | Message is caught up and answered after start |
