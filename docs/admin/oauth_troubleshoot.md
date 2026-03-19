# Google OAuth 実接続 トラブル切り分け表 / 観察ポイント一覧

作成日: 2026-03-19
対象: Phase 1 管理画面 — Google OAuth + TOTP 2FA 実接続確認時

---

## 1. 実接続トラブル切り分け表

### 凡例

- **症状**: ブラウザ / curl / ログで観察できる現象
- **主な原因**: 最も可能性が高い原因
- **まず確認**: 最初に見る場所（30 秒で判断できるもの）
- **次に確認**: まず確認で解決しない場合に掘り下げる場所

---

### T-01: Google ログイン画面に飛ばない / authorization_url が取れない

| 項目 | 内容 |
|---|---|
| **症状** | `GET /auth/login?code_challenge=...&state=...` が 503 を返す、または URL が空/不正 |
| **主な原因** | `GOOGLE_CLIENT_ID` が `.env` に未設定 |
| **まず確認** | コンテナ起動ログ: `管理画面設定: Google OAuth=未設定` が出ていないか |
| **次に確認** | `.env` の `GOOGLE_CLIENT_ID` の値 / `docker compose exec trade_app printenv GOOGLE_CLIENT_ID` で空でないか確認 |

**補足:**
```bash
# 設定確認コマンド（値は表示されないので安全）
docker compose exec trade_app python3 -c "
from trade_app.config import get_settings
s = get_settings()
print('CLIENT_ID:', '設定済み' if s.GOOGLE_CLIENT_ID else '未設定')
print('REDIRECT_URI:', s.OAUTH_REDIRECT_URI)
"
```

---

### T-02: Google 認証画面で `redirect_uri_mismatch` エラー

| 項目 | 内容 |
|---|---|
| **症状** | Google の認証画面で「アクセスをブロック: このアプリのリクエストは無効です」「Error 400: redirect_uri_mismatch」が表示される |
| **主な原因** | Google Cloud Console に登録した redirect_uri と `.env` の `OAUTH_REDIRECT_URI` が不一致 |
| **まず確認** | Google Cloud Console > OAuth 2.0 クライアント > 承認済みのリダイレクト URI を確認。`.env` の `OAUTH_REDIRECT_URI` と**一字一句**一致しているか |
| **次に確認** | `authorization_url` のクエリパラメータの `redirect_uri=` の値を URL デコードして確認。末尾スラッシュ / http vs https / ポート番号の差異に注意 |

**よくある不一致パターン:**
```
NG: http://localhost:5173/auth/callback/   ← 末尾スラッシュあり
OK: http://localhost:5173/auth/callback

NG: http://localhost:5173/auth/Callback    ← 大文字
OK: http://localhost:5173/auth/callback

NG: http://localhost/auth/callback         ← ポート省略
OK: http://localhost:5173/auth/callback
```

---

### T-03: Google コールバック後 `POST /auth/callback` が 400

| 項目 | 内容 |
|---|---|
| **症状** | フロントが `POST /auth/callback` を呼んだら 400 が返る |
| **主な原因** | A) `code` が期限切れ（10 分以内に code exchange しなかった）/ B) `code` が再使用済み / C) `code_verifier` が一致しない / D) `GOOGLE_CLIENT_SECRET` が間違い |
| **まず確認** | バックエンドログ: `Google token exchange 失敗: status=400 error=<Google error コード>` の内容を確認 |
| **次に確認** | A の場合 → STEP 3〜4 をやり直す / C の場合 → フロントの code_verifier 生成・保存ロジックを確認 |

**Google error コード別の原因:**
```
error=invalid_grant       → code が期限切れ / 使用済み / code_verifier 不一致
error=invalid_client      → GOOGLE_CLIENT_ID / SECRET が間違い
error=redirect_uri_mismatch → redirect_uri の不一致（T-02 参照）
error=invalid_request     → 必須パラメータの欠落（code_verifier なし等）
```

---

### T-04: `POST /auth/callback` が 403「登録されていません」

| 項目 | 内容 |
|---|---|
| **症状** | Google 認証は成功したが 403 が返る / レスポンス body `"このメールアドレスは登録されていません"` |
| **主な原因** | ログインしようとした Google アカウントのメールアドレスが `ui_users` テーブルに未登録 |
| **まず確認** | `ui_audit_logs` の `LOGIN_FAILURE` レコードで `user_email` を確認。`ui_users` に同じメールがあるか |
| **次に確認** | `SELECT email FROM ui_users;` でテーブル内容を確認。メールアドレスの大文字小文字、ドット位置に注意（Google では `test.user` と `testuser` は別） |

**対処:**
```sql
-- メールアドレスを挿入
INSERT INTO ui_users (id, email, display_name, role, is_active, totp_enabled, created_at, updated_at)
VALUES (gen_random_uuid(), 'your-email@gmail.com', '管理者', 'admin', true, false, NOW(), NOW());
```

---

### T-05: `POST /auth/callback` が 403「無効化されています」

| 項目 | 内容 |
|---|---|
| **症状** | 403 が返る / レスポンス body `"アカウントが無効化されています"` |
| **主な原因** | `ui_users.is_active = false` になっている |
| **まず確認** | `SELECT email, is_active FROM ui_users WHERE email = 'メールアドレス';` |
| **次に確認** | なし |

**対処:**
```sql
UPDATE ui_users SET is_active = true, updated_at = NOW()
WHERE email = 'your-email@gmail.com';
```

---

### T-06: Cookie が付かない / `credentials: 'include'` 問題

| 項目 | 内容 |
|---|---|
| **症状** | `POST /auth/callback` は 200 だが、その後の `/auth/totp/setup` や `/auth/me` が 401 になる。ブラウザの Cookie タブに `trade_admin_session` が見えない |
| **主な原因** | A) フロントの fetch/axios に `credentials: 'include'` が設定されていない / B) CORS の `allow_origins` とフロントのオリジンが不一致 / C) `COOKIE_SECURE=true` なのに HTTP アクセスしている |
| **まず確認** | ブラウザ DevTools > Network > `POST /auth/callback` のレスポンスヘッダーに `Set-Cookie: trade_admin_session=...` があるか確認 |
| **次に確認** | A) フロントのコードで `credentials: 'include'` を確認 / B) `.env` の `ADMIN_FRONTEND_ORIGIN` とブラウザのアドレスバーのオリジンを比較 / C) `COOKIE_SECURE=false` に変更して再試行 |

**Cookie が Set-Cookie ヘッダーに存在するかの確認（curl）:**
```bash
curl -sv -X POST http://localhost:8000/api/ui-admin/auth/callback \
  -H "Content-Type: application/json" \
  -d '{"code":"...","code_verifier":"...","state":"..."}' 2>&1 | grep -i "set-cookie"
# 期待: < set-cookie: trade_admin_session=...; HttpOnly; SameSite=lax; Path=/api/ui-admin/
```

**CORS 原因の確認:**
```bash
# preflight OPTIONS リクエストのレスポンスを確認
curl -sv -X OPTIONS http://localhost:8000/api/ui-admin/auth/callback \
  -H "Origin: http://localhost:5173" \
  -H "Access-Control-Request-Method: POST" 2>&1 | grep -i "access-control"
# 期待: Access-Control-Allow-Origin: http://localhost:5173
#       Access-Control-Allow-Credentials: true
```

---

### T-07: `POST /auth/totp/setup` が 401

| 項目 | 内容 |
|---|---|
| **症状** | setup を呼んだら 401 が返る |
| **主な原因** | A) Cookie が送られていない（T-06 参照）/ B) Pre-2FA セッション（TTL 10 分）が期限切れ |
| **まず確認** | `POST /auth/callback` からの経過時間が 10 分以内か。TTL = `PRE_2FA_SESSION_TTL_SEC`（デフォルト 600 秒）|
| **次に確認** | `SELECT expires_at, is_2fa_completed FROM ui_sessions WHERE user_id = '...' ORDER BY created_at DESC LIMIT 1;` で期限を確認 |

---

### T-08: `POST /auth/totp/setup` が 503

| 項目 | 内容 |
|---|---|
| **症状** | 503 が返る / レスポンス body `"TOTP 暗号化が設定されていません"` |
| **主な原因** | `TOTP_ENCRYPTION_KEY` が `.env` に未設定 |
| **まず確認** | コンテナ起動ログ: `管理画面設定: ... TOTP暗号化=未設定` が出ていないか |
| **次に確認** | `.env` に `TOTP_ENCRYPTION_KEY=` の値があるか。空文字になっていないか |

**TOTP_ENCRYPTION_KEY の生成:**
```bash
python3 -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"
```

---

### T-09: `POST /auth/totp/verify` が 401「コードが正しくありません」

| 項目 | 内容 |
|---|---|
| **症状** | Authenticator アプリに表示されたコードを入力したが 401 |
| **主な原因** | A) サーバーとスマートフォンの時刻ずれが ±30 秒を超えている / B) setup 後に Authenticator アプリでスキャンしていない / C) QR コードを誤ったアカウントでスキャンした |
| **まず確認** | サーバー時刻: `docker compose exec trade_app date` / スマートフォン時刻を確認して差異を見る |
| **次に確認** | A) 30 秒待ってから再試行（コードが切り替わる）/ B) STEP 5 からやり直して QR コードを再スキャン |

**時刻ずれの確認:**
```bash
# サーバー時刻（UTC）
docker compose exec trade_app python3 -c "from datetime import datetime, timezone; print(datetime.now(timezone.utc))"
# スマートフォンの時刻と比較して ±30 秒以内であること
```

---

### T-10: `POST /auth/totp/verify` が 401「セッション情報が一致しません」

| 項目 | 内容 |
|---|---|
| **症状** | 401 が返る / レスポンス body `"セッション情報が一致しません"` |
| **主な原因** | body の `session_id` と Cookie のトークンが別のセッションのペアになっている（二重検証で不一致）|
| **まず確認** | `session_id` が STEP 4（`POST /auth/callback`）のレスポンスの値か確認。Cookie が正しく送られているか |
| **次に確認** | 複数タブで認証を試みた場合は最新の `session_id` と Cookie のペアを使うこと |

---

### T-11: `GET /auth/me` が 401「2FA 未完了」

| 項目 | 内容 |
|---|---|
| **症状** | verify まで完了したはずだが `/auth/me` で 401 |
| **主な原因** | `verify_totp` で `is_2fa_completed=True` への更新がコミットされていない / Cookie が古い（max_age 延長前のもの）|
| **まず確認** | `SELECT is_2fa_completed FROM ui_sessions ORDER BY created_at DESC LIMIT 1;` で値を確認 |
| **次に確認** | `verify_totp` のレスポンスが 200 だったか確認。200 でも DB 更新が反映されていない場合はコンテナログを確認 |

---

### T-12: 全体的に Cookie が送られない（SPA の CORS 問題）

| 項目 | 内容 |
|---|---|
| **症状** | ブラウザの全リクエストで Cookie が送信されない / ブラウザ Console に CORS エラー |
| **主な原因** | `ADMIN_FRONTEND_ORIGIN` の設定とフロントのオリジンが不一致 |
| **まず確認** | `.env` の `ADMIN_FRONTEND_ORIGIN` の値 / ブラウザのアドレスバーのオリジン（`http://localhost:5173` など）を比較 |
| **次に確認** | ブラウザ DevTools > Console のエラーメッセージ。`CORS policy` や `has been blocked` が出ていれば CORS 設定の問題 |

**よくある CORS 設定ミス:**
```
NG: ADMIN_FRONTEND_ORIGIN=http://localhost:5173/   ← 末尾スラッシュあり
OK: ADMIN_FRONTEND_ORIGIN=http://localhost:5173

NG: ADMIN_FRONTEND_ORIGIN=https://localhost:5173   ← https なのに HTTP でアクセス
OK: ADMIN_FRONTEND_ORIGIN=http://localhost:5173
```

---

## 2. 実接続確認時の観察ポイント一覧

### 2-A: ブラウザ DevTools > Network タブ

フロー上の各リクエストで確認するポイント:

| リクエスト | 見るもの | 期待値 |
|---|---|---|
| `GET /auth/login?...` | Status / Response body | 200 / `{"authorization_url": "https://accounts.google.com/..."}` |
| `GET /auth/login?...` | Query params | `code_challenge`, `state`, `code_challenge_method=S256` が含まれる |
| Google へのリダイレクト | Location header | `https://accounts.google.com/o/oauth2/v2/auth?...` |
| Google コールバック | URL params | `?code=xxx&state=yyy` が存在する |
| `POST /auth/callback` | Status | 200 |
| `POST /auth/callback` | Response headers > `Set-Cookie` | `trade_admin_session=...; HttpOnly; Path=/api/ui-admin/; SameSite=Lax` |
| `POST /auth/callback` | Response body | `{"session_id": "...", "requires_2fa": true, "user_email": "..."}` |
| `POST /auth/totp/setup` | Status | 200 |
| `POST /auth/totp/setup` | Response body | `{"totp_uri": "otpauth://totp/TradeSystem%20Admin:..."}` |
| `POST /auth/totp/verify` | Status | 200 |
| `POST /auth/totp/verify` | Response headers > `Set-Cookie` | `Max-Age=28800` になっている（延長確認）|
| `GET /auth/me` | Status | 200 |
| `GET /auth/me` | Response body | `{"totp_enabled": true, "last_login_at": "..."}` |

**補足:** ブラウザは `HttpOnly` Cookie の値をスクリプトから読めないが、DevTools > Application > Cookies タブで `trade_admin_session` が存在することは確認できる。

---

### 2-B: ブラウザ DevTools > Application > Cookies タブ

| 確認項目 | 期待値 |
|---|---|
| `trade_admin_session` キーが存在する | ✅ |
| `HttpOnly` が `true` | ✅ |
| `Path` が `/api/ui-admin/` | ✅ |
| `SameSite` が `Lax` | ✅ |
| `Secure` フラグ | ローカル HTTP の場合は `false`（`COOKIE_SECURE=false` 設定時）|
| `Expires` / `Max-Age` | Pre-2FA 時は `+600s`、verify 後は `+28800s` に延長される |

---

### 2-C: バックエンドログ（`docker compose logs trade_app -f`）

**起動時に確認するログ:**
```
管理画面設定: Google OAuth=設定済み TOTP暗号化=設定済み
```
→ どちらかが「未設定」なら `.env` を修正してコンテナを再起動する。

**正常フロー時に出るログ:**
```
# POST /auth/callback 成功
INFO: OAuth ログイン成功: user=admin@example.com session=xxxxxxxx (Pre-2FA)

# POST /auth/totp/setup 成功
INFO: TOTP setup 完了: user=admin@example.com

# POST /auth/totp/verify 成功
INFO: TOTP 認証成功: user=admin@example.com session=xxxxxxxx
```

**エラー時のログと意味:**
```
# T-03 の場合（token exchange 失敗）
WARNING: Google token exchange 失敗: status=400 error=invalid_grant description=Code was already redeemed.
→ code が使用済み。STEP 3 からやり直す

# T-03 の場合（redirect_uri_mismatch）
WARNING: Google token exchange 失敗: status=400 error=redirect_uri_mismatch description=...
→ T-02 を参照

# T-01 の場合（Google 接続エラー）
ERROR: Google token endpoint への接続エラー: ...
→ Docker コンテナからの外部通信を確認

# T-06 の場合（CORS エラーはバックエンドではなくブラウザに出る）
→ バックエンドログには現れない。ブラウザ Console を確認すること
```

---

### 2-D: 監査ログ（`ui_audit_logs` テーブル）

正常フロー完了後、以下の 3 イベントが順番に記録されること:

```sql
SELECT event_type, user_email, created_at
FROM ui_audit_logs
ORDER BY created_at DESC
LIMIT 10;
```

**期待される記録（新しい順）:**
```
LOGOUT         | admin@example.com | 2026-03-19 10:05:00
TWO_FA_SUCCESS | admin@example.com | 2026-03-19 10:03:00   ← フル認証完了
LOGIN_SUCCESS  | admin@example.com | 2026-03-19 10:01:00   ← Pre-2FA 発行
```

**異常パターンの読み方:**
```
LOGIN_SUCCESS のみで TWO_FA_SUCCESS がない
  → TOTP フローで詰まっている（T-07〜T-11 を確認）

LOGIN_FAILURE が連続
  → ui_users 未登録 or is_active=false（T-04, T-05 を確認）
  → after_json で reason を確認: unregistered_email / inactive_account

TWO_FA_FAILURE が連続
  → 時刻ずれ or Authenticator スキャン未実施（T-09 を確認）
```

---

### 2-E: DB レコード確認

**正常フロー完了後の DB 状態:**

```sql
-- ui_users: totp_enabled が true になっている
SELECT email, is_active, totp_enabled, last_login_at
FROM ui_users
WHERE email = 'admin@example.com';
-- 期待: is_active=true, totp_enabled=true, last_login_at=認証時刻

-- ui_sessions: is_2fa_completed が true / 期限が 8 時間後
SELECT is_2fa_completed, expires_at, invalidated_at
FROM ui_sessions
WHERE user_id = (SELECT id FROM ui_users WHERE email = 'admin@example.com')
ORDER BY created_at DESC
LIMIT 1;
-- 期待: is_2fa_completed=true, expires_at≒NOW()+8h, invalidated_at=NULL（logout 前）

-- logout 後
-- 期待: invalidated_at=ログアウト時刻
```

---

## 3. 実接続確認の完了条件

「Google OAuth 実接続成功」の判定基準:

### 必須条件（全て満たすこと）

```
□ [C-1] GET /auth/login が 200 で authorization_url を返す
□ [C-2] authorization_url にブラウザでアクセスすると Google ログイン画面が表示される
□ [C-3] Google ログイン後にフロントの /auth/callback に code と state が返ってくる
□ [C-4] POST /auth/callback が 200 で session_id と requires_2fa=true を返す
□ [C-5] POST /auth/callback のレスポンスに Set-Cookie: trade_admin_session が付く
□ [C-6] POST /auth/totp/setup が 200 で otpauth:// URI を返す
□ [C-7] QR コードを Authenticator アプリでスキャンできる
□ [C-8] POST /auth/totp/verify が 200 で is_2fa_completed=true に昇格する
□ [C-9] verify 後の Cookie の Max-Age が 28800 になっている
□ [C-10] GET /auth/me が 200 で totp_enabled=true を返す
□ [C-11] POST /auth/logout が 200 で Cookie がクリアされる
□ [C-12] logout 後の GET /auth/me が 401 になる
□ [C-13] 監査ログに LOGIN_SUCCESS → TWO_FA_SUCCESS → LOGOUT が順番に記録されている
```

### 確認推奨（余裕があれば）

```
□ [C-14] 未登録メールでのログイン試行が 403 + LOGIN_FAILURE ログを記録する
□ [C-15] 不正 TOTP コードが 401 + TWO_FA_FAILURE ログを記録する
□ [C-16] logout 後にセッション Cookie を付けたリクエストが 401 になる
□ [C-17] 2 回目以降のログインフロー（totp_enabled=true の状態）が正常に動作する
```

### 完了後の作業

```
□ [P-1] .env の COOKIE_SECURE=false → true に変更して本番確認
□ [P-2] 本番用 TOTP_ENCRYPTION_KEY を新規生成
□ [P-3] Google Console に本番 redirect_uri を追加登録
```
