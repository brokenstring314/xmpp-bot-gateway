#!/usr/bin/env bash
# 启动本地测试用 Prosody，并创建测试账号（随机密码写入 tests/prosody/accounts.json）
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
export PROSODY_TEST_DIR="$DIR/run"
mkdir -p "$PROSODY_TEST_DIR/data"
CFG="$PROSODY_TEST_DIR/prosody.cfg.lua"
CERTS="$PROSODY_TEST_DIR/certs"
if [ ! -f "$CERTS/test.local.crt" ]; then
  # 测试 CA + test.local 证书（只给 TLS 配置测试用：信任 ca.crt 的客户端能验证通过，其他客户端会拒绝）
  mkdir -p "$CERTS"
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=xmpp-gateway test CA" \
    -keyout "$CERTS/ca.key" -out "$CERTS/ca.crt" 2>/dev/null
  openssl req -newkey rsa:2048 -nodes -subj "/CN=test.local" -keyout "$CERTS/test.local.key" \
    -out "$CERTS/test.local.csr" 2>/dev/null
  printf "subjectAltName=DNS:test.local,DNS:conference.test.local,DNS:upload.test.local\n" > "$CERTS/ext.cnf"
  openssl x509 -req -in "$CERTS/test.local.csr" -CA "$CERTS/ca.crt" -CAkey "$CERTS/ca.key" -CAcreateserial \
    -days 3650 -extfile "$CERTS/ext.cnf" -out "$CERTS/test.local.crt" 2>/dev/null
  chmod 600 "$CERTS"/*.key
fi
sed "s#@RUN@#$PROSODY_TEST_DIR#g" "$DIR/prosody.cfg.lua.in" > "$CFG"
if [ ! -f "$PROSODY_TEST_DIR/accounts.json" ]; then
  BP=$(python3 -c "import secrets;print(secrets.token_urlsafe(18))")
  UP=$(python3 -c "import secrets;print(secrets.token_urlsafe(18))")
  prosodyctl --config "$CFG" register bot test.local "$BP"
  prosodyctl --config "$CFG" register user test.local "$UP"
  printf '{"bot":{"jid":"bot@test.local","password":"%s"},"user":{"jid":"user@test.local","password":"%s"}}' "$BP" "$UP" > "$PROSODY_TEST_DIR/accounts.json"
fi
exec prosody --config "$CFG"
