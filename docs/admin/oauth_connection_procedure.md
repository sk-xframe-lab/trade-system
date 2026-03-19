# Google OAuth 実接続テスト手順書

作成日: 2026-03-19
対象: Phase 1 管理画面 — Google OAuth + TOTP 2FA フロー実接続確認
前提: `docs/admin/oauth_preconnect_checklist.md` の全チェックが完了していること

---

## 概要

確認するフロー全体:

```
[STEP 1] 環境起動確認
[STEP 2] GET /api/ui-admin/auth/login → authorization_url 取得
[STEP 3] Google ログイン → フロントに code コールバック
[STEP 4] POST /api/ui-admin/auth/callback → Pre-2FA セッション発行
[STEP 5] POST /api/ui-admin/auth/totp/setup → QR URI 取得
[STEP 6] POST /api/ui-admin/auth/totp/verify → セッション昇格
[STEP 7] GET /api/ui-admin/auth/me → 認証確認
[STEP 8] POST /api/ui-admin/auth/logout → セッション無効化
[STEP 9] 監査ログ確認
```

---

## STEP 1: 環境起動確認

```bash
# コンテナ起動
docker compose up -d

# ヘルスチェック
curl -s http://localhost:8000/health | python3 -m json.tool
# 期待: {"status": "ok", ...}

# admin DB migration 確認
docker compose exec trade_app alembic -c alembic_admin.ini current
# 期待: a1b2c3d4e5f6 (head)

# ui_users にテストユーザーが存在するか確認
docker compose exec postgres psql -U trade_user -d trade_db \
  -c "SELECT email, is_active, totp_enabled FROM ui_users;"
```

**成功条件:** health が ok / alembic が head / ui_users にログイン予定のメールが存在

**失敗時の確認ポイント:**
- コンテナログ: `docker compose logs trade_app`
- DB migration 未適用: `alembic -c alembic_admin.ini upgrade head` を実行
- ui_users 未登録: `oauth_preconnect_checklist.md §6` の INSERT を実行

---

## STEP 2: GET /auth/login — authorization_url 取得

**フロント実装前の API 単体確認（curl / httpie で実施）:**

```bash
# フロントが行う code_challenge 計算の模擬（テスト用固定値）
CODE_VERIFIER="test_verifier_that_is_43_chars_or_more_12345"
CODE_CHALLENGE=$(echo -n "$CODE_VERIFIER" | sha256sum | cut -d' ' -f1 | xxd -r -p | base64 | tr '+/' '-_' | tr -d '=')
STATE="test_state_$(date +%s)"

curl -s "http://localhost:8000/api/ui-admin/auth/login?code_challenge=${CODE_CHALLENGE}&state=${STATE}" | python3 -m json.tool
```

**期待レスポンス:**
```json
{
  "authorization_url": "https://accounts.google.com/o/oauth2/v2/auth?response_type=code&client_id=xxx...&redirect_uri=http://localhost:5173/auth/callback&scope=openid+email+profile&code_challenge=xxx&code_challenge_method=S256&state=xxx&access_type=online"
}
```

**成功条件:**
- `authorization_url` が `https://accounts.google.com/o/oauth2/v2/auth?` で始まる
- `code_challenge`, `state`, `redirect_uri`, `client_id` がクエリに含まれる
- `code_challenge_method=S256` が含まれる

**失敗時の確認ポイント:**
| エラー | 原因 | 対処 |
|---|---|---|
| 503 | `GOOGLE_CLIENT_ID` 未設定 | `.env` を確認 |
| 422 | `code_challenge` / `state` パラメータ欠落 | クエリパラメータを確認 |
| 500 | `get_settings()` 失敗 | コンテナログを確認 |

---

## STEP 3: Google ログイン → code コールバック

**実ブラウザでの確認（フロントエンドを使う場合）:**

1. フロントを起動（`http://localhost:5173`）
2. STEP 2 で取得した `authorization_url` にブラウザでアクセス
3. Google アカウントでログイン
4. Google が `http://localhost:5173/auth/callback?code=xxx&state=yyy` にリダイレクト
5. フロントで URL の `?state=` と sessionStorage の `oauth_state` を照合
   - **照合成功** → `{code, code_verifier, state}` を `POST /auth/callback` に送信
   - **照合失敗（不一致 / sessionStorage に値なし）** → `POST /auth/callback` を**呼ばない**。エラー表示して停止。この判断はフロント専任であり、バックエンドは照合を行わない。

**フロントなしで curl のみ確認（フロー全体を手動で模擬）:**

Google のブラウザログインは手動で行い、リダイレクト先の URL から `code` をコピーする。
（一時的に `redirect_uri` を `http://localhost/dummy` にして 404 ページの URL から `code` を取得する方法もある。）

**成功条件:** Google ログイン後にコールバック URL（`?code=xxx&state=yyy`）が受け取れる

**失敗時の確認ポイント:**
| エラー | 原因 | 対処 |
|---|---|---|
| `redirect_uri_mismatch` (Google エラー) | Google Console の登録 URI と不一致 | Console の redirect_uri を `.env` と完全一致させる |
| `invalid_client` | GOOGLE_CLIENT_ID / SECRET が間違い | Console から再確認 |
| `access_denied` | Google 同意画面でキャンセル | 再試行 |

---

## STEP 4: POST /auth/callback — Pre-2FA セッション発行

```bash
# STEP 3 で取得した code を使用
CODE="4/0AX4XfWi..."  # Google からのコード

curl -s -c /tmp/trade_cookies.txt \
  -X POST http://localhost:8000/api/ui-admin/auth/callback \
  -H "Content-Type: application/json" \
  -d "{
    \"code\": \"${CODE}\",
    \"code_verifier\": \"${CODE_VERIFIER}\",
    \"state\": \"${STATE}\"
  }" | python3 -m json.tool
```

**期待レスポンス:**
```json
{
  "session_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "requires_2fa": true,
  "user_email": "your-email@gmail.com",
  "user_display_name": "管理者"
}
```

**Cookie の確認:**
```bash
cat /tmp/trade_cookies.txt
# trade_admin_session が HttpOnly / Path=/api/ui-admin/ で設定されていること
```

**成功条件:**
- `requires_2fa: true` が返る
- `session_id` (UUID) が返る
- `Set-Cookie: trade_admin_session=xxx; HttpOnly; SameSite=lax; Path=/api/ui-admin/` がレスポンスヘッダーにある
- `ui_users` の `last_login_at` が更新される

**失敗時の確認ポイント:**
| エラー | 原因 | 対処 |
|---|---|---|
| 400 `token exchange 失敗` | `code` が期限切れ / 使用済み | STEP 3 からやり直す（code は 1 回のみ使用可）|
| 400 `access_token が取得できない` | Google 応答に access_token なし | Google Console のスコープ設定を確認 |
| 403 `登録されていません` | `ui_users` に該当メールなし | STEP 1 で INSERT を確認 |
| 403 `無効化されています` | `ui_users.is_active = false` | DB で `is_active = true` に更新 |
| 502 | Google 接続エラー | ネットワーク / プロキシを確認 |

---

## STEP 5: POST /auth/totp/setup — QR URI 取得

```bash
SESSION_ID="STEP4で取得したsession_id"

curl -s -b /tmp/trade_cookies.txt -c /tmp/trade_cookies.txt \
  -X POST http://localhost:8000/api/ui-admin/auth/totp/setup \
  -H "Content-Type: application/json" | python3 -m json.tool
```

**期待レスポンス:**
```json
{
  "totp_uri": "otpauth://totp/TradeSystem%20Admin:your-email%40gmail.com?secret=XXXXXX&issuer=TradeSystem+Admin",
  "backup_codes": []
}
```

**QR コードの表示（CLI で確認する場合）:**
```bash
# qrencode が使える場合
qrencode -t ANSIUTF8 "otpauth://totp/..."

# または https://www.qr-code-generator.com/ に URI を貼り付けて QR を生成
```

**成功条件:**
- `totp_uri` が `otpauth://totp/TradeSystem%20Admin:` で始まる
- issuer が `TradeSystem Admin`（または `.env` の `TOTP_ISSUER` 設定値）
- `ui_users.totp_secret_encrypted` に `gv1:` で始まる値が保存される
- `ui_users.totp_enabled` は **まだ false**（verify 後に true になる）

**失敗時の確認ポイント:**
| エラー | 原因 | 対処 |
|---|---|---|
| 401 | Cookie なし / 期限切れセッション | STEP 4 からやり直す |
| 503 | `TOTP_ENCRYPTION_KEY` 未設定 | `.env` に設定して再起動 |

---

## STEP 6: POST /auth/totp/verify — セッション昇格

```bash
# Google Authenticator（またはその他 TOTP アプリ）で QR コードをスキャンして表示されたコードを入力
TOTP_CODE="123456"  # 実際に Authenticator に表示されたコード

curl -s -b /tmp/trade_cookies.txt -c /tmp/trade_cookies.txt \
  -X POST http://localhost:8000/api/ui-admin/auth/totp/verify \
  -H "Content-Type: application/json" \
  -d "{
    \"session_id\": \"${SESSION_ID}\",
    \"totp_code\": \"${TOTP_CODE}\"
  }" | python3 -m json.tool
```

**期待レスポンス:**
```json
{
  "user_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "user_email": "your-email@gmail.com",
  "role": "admin",
  "expires_at": "2026-03-19T17:55:00+00:00"
}
```

**Cookie の確認:**
```bash
cat /tmp/trade_cookies.txt
# trade_admin_session の Max-Age が 28800 に延長されていること
```

**成功条件:**
- 200 レスポンスで `user_id`, `user_email`, `role`, `expires_at` が返る
- Cookie の `Max-Age` が `28800`（8 時間）になっている
- `ui_sessions.is_2fa_completed = true` に更新されている
- `ui_users.totp_enabled = true` に更新されている

**失敗時の確認ポイント:**
| エラー | 原因 | 対処 |
|---|---|---|
| 401 `TOTP コードが正しくありません` | コードの入力ミス / 時刻ずれ（±30s超）| 最大 ±30s の誤差は許容済み。30s 待って再試行 |
| 401 `セッションが見つかりません` | `session_id` が間違い | STEP 4 のレスポンスを確認 |
| 401 `セッション情報が一致しません` | Cookie と `session_id` のペアが不一致 | Cookie ファイルと session_id を確認 |
| 400 `TOTP が設定されていません` | STEP 5 をスキップ | STEP 5 を先に実行 |
| 503 | `TOTP_ENCRYPTION_KEY` 未設定 | `.env` に設定して再起動 |

---

## STEP 7: GET /auth/me — 認証確認

```bash
curl -s -b /tmp/trade_cookies.txt \
  http://localhost:8000/api/ui-admin/auth/me | python3 -m json.tool
```

**期待レスポンス:**
```json
{
  "user_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "email": "your-email@gmail.com",
  "display_name": "管理者",
  "role": "admin",
  "totp_enabled": true,
  "last_login_at": "2026-03-19T09:55:00+00:00"
}
```

**成功条件:**
- `totp_enabled: true`（STEP 6 で更新された値）
- `last_login_at` が STEP 4 の認証時刻になっている

**失敗時の確認ポイント:**
| エラー | 原因 | 対処 |
|---|---|---|
| 401 `2FA 未完了` | STEP 6 が完了していない | STEP 6 をやり直す |
| 401 `期限切れ` | セッション TTL が過ぎた | STEP 2 からやり直す |

---

## STEP 8: POST /auth/logout — セッション無効化

```bash
curl -s -b /tmp/trade_cookies.txt -c /tmp/trade_cookies.txt \
  -X POST http://localhost:8000/api/ui-admin/auth/logout \
  -H "Content-Type: application/json" | python3 -m json.tool
```

**期待レスポンス:**
```json
{"message": "ログアウトしました"}
```

**ログアウト後の確認:**
```bash
# /auth/me を呼ぶと 401 になること
curl -s -b /tmp/trade_cookies.txt \
  http://localhost:8000/api/ui-admin/auth/me
# 期待: 401 Unauthorized
```

**成功条件:**
- 200 で `"ログアウトしました"` が返る
- 以降の `/auth/me` が 401 を返す
- `ui_sessions.invalidated_at` に値がセットされている

---

## STEP 9: 監査ログ確認

接続テスト完了後、以下の監査ログが記録されていることを確認する。

```sql
-- 全監査ログ確認（直近 10 件）
SELECT event_type, user_email, ip_address, created_at
FROM ui_audit_logs
ORDER BY created_at DESC
LIMIT 10;
```

**期待される記録（上から新しい順）:**

| event_type | タイミング | 記録されるべき内容 |
|---|---|---|
| `LOGOUT` | STEP 8 | user_email, session_id |
| `TWO_FA_SUCCESS` | STEP 6 成功時 | user_email, ip_address |
| `LOGIN_SUCCESS` | STEP 4 成功時 | user_email, ip_address |

**失敗ケースも確認する場合（任意）:**

| event_type | 発生条件 | 確認方法 |
|---|---|---|
| `LOGIN_FAILURE` | 未登録メールでログイン試行 | 別メールで STEP 3〜4 を試みる |
| `TWO_FA_FAILURE` | 不正 TOTP コードを送信 | STEP 6 で `"000000"` を送信 |
| `SESSION_EXPIRED_ACCESS` | 期限切れ Cookie でアクセス | DB で session.expires_at を過去に更新後にリクエスト |

---

## 接続テスト完了チェックリスト

```
□ STEP 1: health OK / alembic head / ui_users 確認済み
□ STEP 2: authorization_url が正しい形式で返る
□ STEP 3: Google ログインが成功して code を受け取れた
□ STEP 4: Pre-2FA セッションが発行された（requires_2fa: true）
□ STEP 4: HttpOnly Cookie が Set-Cookie ヘッダーにある
□ STEP 5: totp_uri が返り QR コードをスキャンできた
□ STEP 6: TOTP コードで verify に成功した（200）
□ STEP 6: Cookie の Max-Age が 28800 に延長された
□ STEP 7: /auth/me で totp_enabled: true が確認できた
□ STEP 8: logout 後に /auth/me が 401 になった
□ STEP 9: LOGIN_SUCCESS / TWO_FA_SUCCESS / LOGOUT ログが記録された
```
