-- 키·블로그 아이디는 원문 대신 HMAC(서버 비밀값) 해시만 저장
CREATE TABLE IF NOT EXISTS licenses (
  key_hash TEXT PRIMARY KEY,
  plan TEXT NOT NULL,              -- '30' | '40' | '50' | 'admin'
  unlimited INTEGER NOT NULL DEFAULT 0,
  tokens_total INTEGER NOT NULL DEFAULT 0,
  tokens_left INTEGER NOT NULL DEFAULT 0,
  issued_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL DEFAULT 0, -- 0 = 만료 없음
  status TEXT NOT NULL DEFAULT 'active', -- active | revoked
  memo TEXT NOT NULL DEFAULT '',
  key_hint TEXT NOT NULL DEFAULT '',     -- 키 끝 4자리 (관리 목록 구분용)
  last_used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS accounts (
  key_hash TEXT NOT NULL,
  acct_hash TEXT NOT NULL,
  added_at INTEGER NOT NULL,
  PRIMARY KEY (key_hash, acct_hash)
);
CREATE TABLE IF NOT EXISTS usage (
  request_id TEXT PRIMARY KEY,
  key_hash TEXT NOT NULL,
  amount INTEGER NOT NULL,
  acct_hash TEXT NOT NULL DEFAULT '',
  at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_key ON usage(key_hash, at);
CREATE TABLE IF NOT EXISTS fails (ip TEXT NOT NULL, at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS fails_ip ON fails(ip, at);
-- migrate1: 한 번에 한 PC 로그인(session), 발행 전 선차감·실패 시 환불(state: reserved|refunded|used)
-- ALTER TABLE licenses ADD COLUMN session_id TEXT NOT NULL DEFAULT '';
-- ALTER TABLE licenses ADD COLUMN session_at INTEGER NOT NULL DEFAULT 0;
-- ALTER TABLE usage ADD COLUMN state TEXT NOT NULL DEFAULT 'used';
