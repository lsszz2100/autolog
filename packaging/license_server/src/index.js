// 오토로그 라이선스·토큰 장부 서버 (Cloudflare Worker + D1)
//
// - 이 서버에는 '발급·관리' 기능이 없음. 발급·수정은 Cloudflare 계정으로 로그인된 개발 PC에서
//   D1 데이터베이스에 직접 기록 (packaging/license_admin.py) → 인터넷에서 발급을 시도할 문 자체가 없음
// - 키·블로그 아이디는 HMAC-SHA256(PEPPER) 해시로만 저장 → DB가 새어도 키를 알 수 없음
// - 모든 응답은 {data, sig}(Ed25519 서명) + 요청마다 다른 nonce → 가짜 서버·재전송 차단
// - 한 키는 한 번에 PC 한 대만 로그인 (관리자 키 제외). 다른 PC에서 로그인하면 이전 PC는 로그아웃
// - 발행 시작 전에 100토큰 선차감(reserve), 발행 실패 시 환불(refund: 1시간 이내, 하루 5회까지)
// 비밀값(wrangler secret): PEPPER, SIGN_KEY(Ed25519 개인 키 PKCS8 base64)

const COST_PER_POST = 100;
const MAX_ACCOUNTS = 5;
const FAIL_WINDOW = 600;      // 10분
const FAIL_LIMIT = 20;        // 10분에 키 틀림 20회 넘으면 잠시 차단
const REFUND_WINDOW = 3600;   // 선차감 후 1시간 안에만 환불
const REFUND_PER_DAY = 5;
const enc = new TextEncoder();

const now = () => Math.floor(Date.now() / 1000);
const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
const normKey = (k) => String(k || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
const isHex = (v, min = 16, max = 64) => typeof v === "string" && v.length >= min && v.length <= max && /^[0-9a-f]+$/.test(v);

async function hmac(env, text) {
  const key = await crypto.subtle.importKey("raw", enc.encode(env.PEPPER), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return hex(await crypto.subtle.sign("HMAC", key, enc.encode(text)));
}

let signKey = null;
async function signed(env, obj, status = 200) {
  if (!signKey) {
    const der = Uint8Array.from(atob(env.SIGN_KEY), (c) => c.charCodeAt(0));
    signKey = await crypto.subtle.importKey("pkcs8", der, { name: "Ed25519" }, false, ["sign"]);
  }
  const data = JSON.stringify({ ...obj, server_time: now() });
  const sig = hex(await crypto.subtle.sign({ name: "Ed25519" }, signKey, enc.encode(data)));
  return new Response(JSON.stringify({ data, sig }), {
    status, headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });
}

const plain = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" } });

function view(lic, accounts) {
  const t = now();
  const expired = lic.expires_at > 0 && lic.expires_at <= t;
  return {
    plan: lic.plan,
    unlimited: !!lic.unlimited,
    tokens_left: lic.tokens_left,
    tokens_total: lic.tokens_total,
    cost_per_post: COST_PER_POST,
    issued_at: lic.issued_at,
    expires_at: lic.expires_at,
    status: lic.status,
    expired,
    usable: lic.status === "active" && !expired,
    accounts_used: accounts,
    max_accounts: MAX_ACCOUNTS,
  };
}

async function accountCount(env, kh) {
  const r = await env.DB.prepare("SELECT COUNT(*) AS n FROM accounts WHERE key_hash = ?").bind(kh).first();
  return r?.n || 0;
}

async function client(env, path, body, ip) {
  const nonce = body.nonce;
  const session = body.session;
  if (!isHex(nonce)) return plain({ error: "bad request" }, 400);
  if (!isHex(session, 32, 32)) {
    // 예전 버전 프로그램 (PC 1대 규칙을 모름) → 업데이트 안내
    return signed(env, { ok: false, nonce, code: "update_required",
      msg: "프로그램을 최신 버전으로 업데이트한 뒤 다시 로그인해 주세요. (화면 위쪽 버전 옆 업데이트 버튼)" });
  }

  const fails = await env.DB.prepare("SELECT COUNT(*) AS n FROM fails WHERE ip = ? AND at > ?").bind(ip, now() - FAIL_WINDOW).first();
  if ((fails?.n || 0) >= FAIL_LIMIT) return signed(env, { ok: false, nonce, code: "rate_limited", msg: "잠시 후 다시 시도해 주세요." }, 429);

  const kh = await hmac(env, "key:" + normKey(body.key));
  let lic = await env.DB.prepare("SELECT * FROM licenses WHERE key_hash = ?").bind(kh).first();
  if (!lic) {
    await env.DB.prepare("INSERT INTO fails (ip, at) VALUES (?, ?)").bind(ip, now()).run();
    return signed(env, { ok: false, nonce, code: "invalid", msg: "등록되지 않은 라이선스 키입니다." });
  }
  const t = now();
  let acc = await accountCount(env, kh);
  const v = () => view(lic, acc);

  // 정지된 키 (가장 먼저 확인 → 세션 갱신·차감 등 어떤 작업도 하지 않음): 프로그램이 저장된 키를 지우고 바로 로그아웃하도록 'invalid' 로 응답
  if (lic.status !== "active") {
    return signed(env, { ok: false, nonce, code: "invalid", msg: "사용이 중지된 라이선스입니다. 로그아웃되었습니다.", license: v() });
  }

  // 한 번에 PC 한 대 (관리자 키 제외)
  if (!lic.unlimited) {
    if (path === "/v1/check" && body.take_over === true) {
      await env.DB.prepare("UPDATE licenses SET session_id = ?, session_at = ? WHERE key_hash = ?").bind(session, t, kh).run();
      lic.session_id = session;
    } else if (!lic.session_id) {
      await env.DB.prepare("UPDATE licenses SET session_id = ?, session_at = ? WHERE key_hash = ? AND session_id = ''").bind(session, t, kh).run();
      lic.session_id = session;
    }
    if (lic.session_id !== session) {
      return signed(env, { ok: false, nonce, code: "session_moved", msg: "다른 PC에서 이 라이선스로 로그인해 이 PC는 로그아웃되었습니다.", license: v() });
    }
  }
  if (lic.status !== "active" || (lic.expires_at > 0 && lic.expires_at <= t)) {
    return signed(env, { ok: false, nonce, code: lic.status !== "active" ? "revoked" : "expired",
      msg: lic.status !== "active" ? "사용이 중지된 라이선스입니다." : "기간이 끝난 라이선스입니다.", license: v() });
  }

  if (path === "/v1/check") {
    await env.DB.prepare("UPDATE licenses SET last_used = ? WHERE key_hash = ?").bind(t, kh).run();
    return signed(env, { ok: true, nonce, code: "ok", license: v() });
  }

  if (path === "/v1/reserve" || path === "/v1/refund") {
    const rid = body.request_id;
    if (!isHex(rid, 32, 32)) return plain({ error: "bad request_id" }, 400);
    const row = await env.DB.prepare("SELECT * FROM usage WHERE request_id = ?").bind(rid).first();
    if (row && row.key_hash !== kh) return plain({ error: "bad request_id" }, 400);

    if (path === "/v1/reserve") {
      if (!row) {
        const amount = lic.unlimited ? 0 : COST_PER_POST;
        const acct = body.account ? await hmac(env, "acct:" + String(body.account).toLowerCase()) : "";
        if (amount) {
          const r = await env.DB.prepare("UPDATE licenses SET tokens_left = tokens_left - ?, last_used = ? WHERE key_hash = ? AND tokens_left >= ?")
            .bind(amount, t, kh, amount).run();
          if (!r.meta.changes) {
            return signed(env, { ok: false, nonce, code: "no_tokens", msg: `토큰이 부족합니다. (글 1개 발행에 ${COST_PER_POST}토큰 필요)`, license: v() });
          }
        }
        await env.DB.prepare("INSERT INTO usage (request_id, key_hash, amount, acct_hash, at, state) VALUES (?, ?, ?, ?, ?, 'reserved')")
          .bind(rid, kh, amount, acct, t).run();
      }
    } else {
      if (!row || row.state !== "reserved") return signed(env, { ok: false, nonce, code: "no_refund", msg: "환불할 수 없는 요청입니다.", license: v() });
      if (t - row.at > REFUND_WINDOW) return signed(env, { ok: false, nonce, code: "no_refund", msg: "환불 가능 시간이 지났습니다.", license: v() });
      const today = await env.DB.prepare("SELECT COUNT(*) AS n FROM usage WHERE key_hash = ? AND state = 'refunded' AND at > ?").bind(kh, t - 86400).first();
      if ((today?.n || 0) >= REFUND_PER_DAY) return signed(env, { ok: false, nonce, code: "no_refund", msg: "오늘 환불 가능 횟수를 넘었습니다.", license: v() });
      await env.DB.batch([
        env.DB.prepare("UPDATE usage SET state = 'refunded' WHERE request_id = ? AND state = 'reserved'").bind(rid),
        env.DB.prepare("UPDATE licenses SET tokens_left = tokens_left + ? WHERE key_hash = ?").bind(row.amount, kh),
      ]);
    }
    lic = await env.DB.prepare("SELECT * FROM licenses WHERE key_hash = ?").bind(kh).first();
    return signed(env, { ok: true, nonce, code: row ? "already" : "ok", license: v() });
  }

  if (path === "/v1/accounts/add" || path === "/v1/accounts/remove") {
    const name = String(body.account || "").toLowerCase().trim();
    if (!/^[a-z0-9._@+-]{2,64}$/.test(name)) return plain({ error: "bad account" }, 400);
    const ah = await hmac(env, "acct:" + name);
    if (path === "/v1/accounts/remove") {
      await env.DB.prepare("DELETE FROM accounts WHERE key_hash = ? AND acct_hash = ?").bind(kh, ah).run();
      acc = await accountCount(env, kh);
      return signed(env, { ok: true, nonce, code: "ok", license: v() });
    }
    const exists = await env.DB.prepare("SELECT 1 AS x FROM accounts WHERE key_hash = ? AND acct_hash = ?").bind(kh, ah).first();
    if (!exists) {
      if (acc >= MAX_ACCOUNTS) {
        return signed(env, { ok: false, nonce, code: "account_limit", msg: `이 라이선스에는 블로그 계정을 최대 ${MAX_ACCOUNTS}개까지 등록할 수 있습니다. (다른 PC에서 등록한 계정 포함)`, license: v() });
      }
      await env.DB.prepare("INSERT OR IGNORE INTO accounts (key_hash, acct_hash, added_at) VALUES (?, ?, ?)").bind(kh, ah, t).run();
      acc = await accountCount(env, kh);
    }
    return signed(env, { ok: true, nonce, code: exists ? "already" : "ok", license: v() });
  }
  return plain({ error: "not found" }, 404);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method !== "POST") return plain({ service: "nbs-license" }, url.pathname === "/" ? 200 : 405);
    if (!["/v1/check", "/v1/reserve", "/v1/refund", "/v1/accounts/add", "/v1/accounts/remove"].includes(url.pathname)) {
      return plain({ error: "not found" }, 404);
    }
    let body;
    try {
      body = await request.json();
    } catch {
      return plain({ error: "bad json" }, 400);
    }
    if (!body || typeof body !== "object") return plain({ error: "bad json" }, 400);
    const ip = request.headers.get("cf-connecting-ip") || "unknown";
    if (Math.random() < 0.02) await env.DB.prepare("DELETE FROM fails WHERE at < ?").bind(now() - FAIL_WINDOW).run();
    return client(env, url.pathname, body, ip);
  },
};
