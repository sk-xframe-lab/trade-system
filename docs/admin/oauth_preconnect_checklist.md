# Google OAuth 実接続前チェックリスト

作成日: 2026-03-19
対象: Phase 1 管理画面 — Google OAuth + TOTP 2FA フロー
前提: 実装は完了済み。このファイルは**接続確認に入る前**に確認すること。

---

## 1. Google Cloud Console 側の設定

### 1-1. OAuth 2.0 クライアント ID の作成

| 確認項目 | 手順・注意点 |
|---|---|
| プロジェクト作成済み | Google Cloud Console > プロジェクト選択 |
| OAuth 同意画面設定済み | 「外部」or「内部」。社内利用なら「内部」推奨（テスト不要）|
| OAuth 2.0 クライアント ID 作成済み | 「アプリの種類」= **ウェブアプリケーション** |
| クライアント ID / シークレット取得済み | 後述の `.env` に設定する |

### 1-2. 承認済みリダイレクト URI の登録

**重要: 完全一致チェック。末尾スラッシュの有無も含めて一字一句一致させること。**

| 環境 | 登録する redirect_uri | `.env` の `OAUTH_REDIRECT_URI` |
|---|---|---|
| ローカル開発 | `http://localhost:5173/auth/callback` | `http://localhost:5173/auth/callback` |
| Docker (ホスト直接) | `http://localhost:5173/auth/callback` | 同上 |
| 本番 | `https://admin.example.com/auth/callback` | `https://admin.example.com/auth/callback` |

**注意事項:**
- Google Console は複数の redirect_uri を登録可能。開発用と本番用を両方登録しても良い。
- `http://localhost` は Google が例外的に許可している（本番 HTTPS 以外でテスト可能）。
- フロントエンドの SPA が実際にこの URL でコールバックを受け取ることを確認すること。

### 1-3. OAuth スコープ

バックエンドが要求するスコープ: `openid email profile`

| スコープ | 用途 | 必須 |
|---|---|---|
| `openid` | OIDC セッション | ✅ |
| `email` | `ui_users` との照合に使用 | ✅ |
| `profile` | `display_name` 取得（将来利用）| ○（あった方が良い）|

---

## 2. バックエンド環境変数の設定

`.env` ファイル（`.gitignore` 除外済み）に以下を設定する。

### 2-1. 必須設定（未設定だと 503 または動作不能）

```bash
# Google OAuth
GOOGLE_CLIENT_ID=xxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-xxxxxxxxxx
OAUTH_REDIRECT_URI=http://localhost:5173/auth/callback  # フロントの callback URL と完全一致

# TOTP 暗号化鍵（32 バイト Base64）
# 生成コマンド: python3 -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"
TOTP_ENCRYPTION_KEY=<生成した値>

# Cookie（ローカルは false、本番は true）
# ⚠️ ローカル HTTP 環境でのみ false を使用すること。
# ⚠️ 実接続確認完了後は、本番デプロイ前に必ず true に戻すこと。
COOKIE_SECURE=false
```

> **注意**: `COOKIE_SECURE=false` の場合、Cookie の `Secure` 属性が付与されないため、HTTP 通信でもトークンが送受信される。ローカル開発・接続確認専用の設定であり、本番環境での使用は**絶対禁止**。本番デプロイ前に `COOKIE_SECURE=true` へ戻すことを忘れないこと（§7 チェックリストも参照）。

### 2-2. CORS 設定（フロントエンドのオリジンと一致させること）

```bash
# フロントエンドの origin（末尾スラッシュなし）
ADMIN_FRONTEND_ORIGIN=http://localhost:5173
```

`ADMIN_FRONTEND_ORIGIN` は `main.py` の CORS `allow_origins` に直接使われる。
フロントが `http://localhost:5173` からリクエストを出す場合、この値と完全一致させること。

### 2-3. セッション関連（デフォルト値で動作するが確認推奨）

| 設定 | デフォルト | 説明 |
|---|---|---|
| `SESSION_TTL_SEC` | `28800` | 8 時間（2FA 完了後のセッション有効期限）|
| `PRE_2FA_SESSION_TTL_SEC` | `600` | 10 分（OAuth 後・TOTP 前の仮セッション有効期限）|
| `TOTP_ISSUER` | `TradeSystem Admin` | Google Authenticator に表示されるサービス名。変更する場合は `.env` で設定 |

---

## 3. フロントエンド実装前提条件

バックエンドの実装はこれらを前提としている。フロント実装時に必ず対応すること。

### 3-1. PKCE 実装（必須）

| フロント責務 | 実装内容 |
|---|---|
| `code_verifier` 生成 | 43〜128 文字のランダム文字列（`crypto.randomUUID()` 等）|
| `code_challenge` 計算 | `BASE64URL(SHA-256(code_verifier))` |
| `state` 生成 | ランダム文字列（CSRF トークン）|
| sessionStorage 保存 | `oauth_state`, `oauth_code_verifier` をコールバック前に保存 |
| `state` 照合 | コールバック受信後、URL の `?state=` と sessionStorage を比較 |
| **照合失敗時の動作** | `POST /auth/callback` を**呼ばない**。エラー表示して停止 |

### 3-2. Cookie の受信条件

| 条件 | 説明 |
|---|---|
| `fetch` / `axios` に `credentials: 'include'` | Cookie を送受信するために必須 |
| CORS `allow_credentials=true` | バックエンド設定済み |
| `allow_origins` にフロント origin が含まれること | `ADMIN_FRONTEND_ORIGIN` で設定済み |
| Cookie `path=/api/ui-admin/` | `/api/ui-admin/` 以下のリクエストのみ Cookie が送られる |

### 3-3. TOTP フロー（フロント UX）

```
GET /auth/login?code_challenge=xxx&state=yyy
  → Google 認証ページへリダイレクト
    → /auth/callback?code=xxx&state=yyy（Google からフロントへ）
      → フロントで state 照合
        → POST /auth/callback {code, code_verifier, state}
          → レスポンス: {session_id, requires_2fa: true}
            → POST /auth/totp/setup（QR コード表示）
              → Google Authenticator でスキャン
                → POST /auth/totp/verify {session_id, totp_code}
                  → Cookie max_age 延長（8 時間）
                    → GET /auth/me で認証確認
```

---

## 4. ローカル / Docker / 本番 での差分一覧

| 設定項目 | ローカル開発 | Docker（compose）| 本番 |
|---|---|---|---|
| `COOKIE_SECURE` | `false` | `false`（HTTP の場合）| `true`（HTTPS 必須）|
| `OAUTH_REDIRECT_URI` | `http://localhost:5173/auth/callback` | `http://localhost:5173/auth/callback` | `https://admin.example.com/auth/callback` |
| `ADMIN_FRONTEND_ORIGIN` | `http://localhost:5173` | `http://localhost:5173` | `https://admin.example.com` |
| `TOTP_ENCRYPTION_KEY` | テスト用値でも可 | テスト用値でも可 | 本番用に新規生成 **必須** |
| Google Console 登録 URI | `http://localhost:5173/auth/callback` | 同左 | `https://admin.example.com/auth/callback` |
| DB migration | `alembic_admin upgrade head` 実施済み | 同左 | 同左 |

**本番移行時の追加作業:**
- Google Console で `https://` の redirect_uri を追加登録
- `TOTP_ENCRYPTION_KEY` を本番用に新規生成（開発用鍵と共有しない）
- `COOKIE_SECURE=true` に変更（HTTPS 環境が前提）
- HTTPS 証明書の設定（リバースプロキシ等）

---

## 5. DB migration 確認

admin_db の migration が適用済みであること。

```bash
# alembic_admin チェーンの migration 適用
docker compose exec trade_app alembic -c alembic_admin.ini upgrade head

# 確認: バージョンテーブルが alembic_version_admin に存在すること
docker compose exec postgres psql -U trade_user -d trade_db \
  -c "SELECT version_num FROM alembic_version_admin;"
# 期待出力: a1b2c3d4e5f6

# テーブル確認
docker compose exec postgres psql -U trade_user -d trade_db \
  -c "\dt ui_users ui_sessions ui_audit_logs"
```

**確認が必要なテーブル:**
- `ui_users` — ログインユーザーの事前登録先
- `ui_sessions` — セッション管理
- `ui_audit_logs` — 監査ログ

---

## 6. ユーザー事前登録

**Google OAuth で認証できるメールアドレスは `ui_users` に事前登録が必要。**

```sql
-- ui_users に管理者ユーザーを登録
INSERT INTO ui_users (id, email, display_name, role, is_active, totp_enabled, created_at, updated_at)
VALUES (
    gen_random_uuid(),
    'your-email@gmail.com',  -- Google アカウントのメールアドレス
    '管理者',
    'admin',
    true,
    false,
    NOW(),
    NOW()
);
```

**注意:** `totp_enabled=false` で登録。初回 TOTP setup/verify を行うことで `true` になる。

---

## 7. 接続前 最終確認チェックリスト

```
□ Google Cloud Console で OAuth クライアント ID を作成済み
□ redirect_uri を Google Console に登録済み（.env の OAUTH_REDIRECT_URI と完全一致）
□ .env に GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET を設定済み
□ .env に TOTP_ENCRYPTION_KEY を設定済み（32 バイト Base64）
□ .env に COOKIE_SECURE=false を設定済み（ローカル HTTP 環境の場合）
□ .env の ADMIN_FRONTEND_ORIGIN がフロントのオリジンと一致している
□ .env の OAUTH_REDIRECT_URI がフロントの callback URL と完全一致している
□ alembic_admin upgrade head 実施済み（ui_users / ui_sessions テーブル存在確認）
□ ui_users にテスト用メールアドレスを INSERT 済み
□ Docker compose up で trade_app / postgres / redis が起動している
□ フロントエンド（SPA）が http://localhost:5173 で起動している
□ フロントエンドに credentials: 'include' が設定されている
□ フロントエンドで PKCE（code_verifier / code_challenge / state）が実装されている
□ フロントエンドで state 照合失敗時は POST /auth/callback を呼ばない実装になっている

--- 接続確認完了後（本番移行前に必ず実施）---
□ .env の COOKIE_SECURE を false → true に変更した（HTTPS 環境で再確認）
□ 本番用 TOTP_ENCRYPTION_KEY を新規生成済み（開発用と別の値）
□ Google Console の redirect_uri に本番 URL を追加登録済み
```
