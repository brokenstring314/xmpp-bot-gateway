# 测试说明

[English](TESTING.md)

## 自动测试（CI 每次推送都会跑）

```bash
bash tests/prosody/run.sh &          # 本地 Prosody（127.0.0.1:15222）
python -m mypy                       # 类型检查
python -m pytest -q tests/test_cq.py tests/test_props.py tests/test_config.py
for t in p0 media reply mam omemo_resync redteam config config_omemo chaos; do python -u tests/e2e_$t.py; done
# Docker 任务：先构建镜像，再运行 IMAGE=<镜像标签> python -u tests/docker_smoke.py
# 统计覆盖率：把 `python -u` 换成 `python -u tests/cov_run.py`，然后 `coverage combine && coverage report`
```

## 24 小时挂机（`tests/soak.py`）

网关作为子进程运行，脚本里的模拟机器人框架把每条私聊原样回显，测试账号每分钟发一次 ping（明文与 OMEMO 交替），记录往返时间、丢失、网关内存、打开文件数、数据库大小和重连次数。

要求：Linux、WSL2 或 macOS，Python 3.11+，能连外网；机器在测试期间不能睡眠。

```bash
git clone https://github.com/brokenstring314/xmpp-bot-gateway.git
cd xmpp-bot-gateway
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[omemo]" tomli-w
# 把 soak.toml（两个测试账号）放到仓库根目录
python3 tests/soak.py -c soak.toml --hours 24
```

结果写入 `soak-<时间>/`：`report.md` 是摘要，`pings.csv`、`samples.csv`、`gateway.log` 是明细。`report.md` 每 5 分钟刷新一次，中途断掉也有结果。

## 真机客户端检查清单（Conversations / Gajim）

前提：网关和一个机器人框架在运行，你的账号已把机器人加为联系人。`!help` 代表你的框架会回应的任意命令。每项看机器人的反应，不对就截图。

| # | 操作 | 预期 |
|---|---|---|
| 1 | 明文发 `!help` | 收到帮助文本 |
| 2 | 打开 OMEMO 再发 `!help` | 回复是加密的（显示锁） |
| 3 | 关掉 OMEMO 再发 `!help` | 回复不加密 |
| 4 | 发一张图片（OMEMO 开、关各一次） | 机器人收到图片；加密时为本地文件 |
| 5 | 发一段语音 | 机器人收到 record 消息段 |
| 6 | 回复机器人的一条消息，内容为 `!help` | 正常回复；引用不被当成命令的一部分 |
| 7 | 发一条消息后立刻撤回 | 机器人收到撤回通知 |
| 8 | 建群并邀请机器人 | 机器人自动进群 |
| 9 | 在群里 `@机器人 !help` | 在群里回复，并 @ 你 |
| 10 | 让网关停 1 分钟，期间发 `!help`，再启动 | 启动后补收并回复 |
