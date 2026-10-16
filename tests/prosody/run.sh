#!/usr/bin/env bash
# 启动本地测试用 Prosody，并创建测试账号（随机密码写入 tests/prosody/accounts.json）
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
export PROSODY_TEST_DIR="$DIR/run"
mkdir -p "$PROSODY_TEST_DIR/data"
CFG="$PROSODY_TEST_DIR/prosody.cfg.lua"
sed "s#@RUN@#$PROSODY_TEST_DIR#g" "$DIR/prosody.cfg.lua.in" > "$CFG"
if [ ! -f "$PROSODY_TEST_DIR/accounts.json" ]; then
  BP=$(python3 -c "import secrets;print(secrets.token_urlsafe(18))")
  UP=$(python3 -c "import secrets;print(secrets.token_urlsafe(18))")
  prosodyctl --config "$CFG" register bot test.local "$BP"
  prosodyctl --config "$CFG" register user test.local "$UP"
  printf '{"bot":{"jid":"bot@test.local","password":"%s"},"user":{"jid":"user@test.local","password":"%s"}}' "$BP" "$UP" > "$PROSODY_TEST_DIR/accounts.json"
fi
exec prosody --config "$CFG"
