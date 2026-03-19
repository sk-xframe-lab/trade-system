# I-4 認証・セッション設計書

対象: auth/callback, セッション発行, 2FA フロー
作成日: 2026-03-18
更新日: 2026-03-19（補足追記 — state 検証前提 / TOTP setup ポリシー / valid_window / TOTP_ISSUER config 化）
ステータス: **設計確定（2026-03-18 ユーザー承認済み）**

## 確定事項サマリー（2026-03-18）

| 項目 | 確定値 |
|---|---|
| フロントエンド構成 | SPA（React / Vue / Svelte 等 — フレームワーク未定）|
| OAuth フロータイプ | Authorization Code Flow **with PKCE** |
| redirect_uri | フロントエンドに直接戻す（`https://[front]/auth/callback`）|
| code exchange | バックエンドで実施（フロントが Google access_token を保持しない）|
| state 管理 | フロントが生成・sessionStorage に保存・コールバック時に照合（CSRF 検証）。バックエンドは DB 保存も照合も行わない（§2-A-1 参照）|
| 新規ユーザー作成 | **事前登録必須**（`ui_users` に存在するメールのみログイン可）|
| セッショントークン返却 | **HttpOnly Cookie** |
| Cookie 設定 | HttpOnly / Secure（本番）/ SameSite=Lax |
| TOTP バックアップコード | Phase 1 は未実装 |
| TOTP issuer 名称 | `TradeSystem Admin` |
| Google OAuth 認証情報 | 未準備でも可（先行実装後に最終接続確認）|

---

## I-3 / I-5 / I-4 確定による更新サマリー

| 項目 | 変更内容 |
|---|---|
| I-3 確定（2026-03-18）| `POST /auth/totp/setup` / `POST /auth/totp/verify` の TOTP ロジックは実装可能（§2-B）|
| I-5 確定（2026-03-18）| セッション TTL 決定済み。Pre-2FA=600s / 認証後=28800s |
| SESSION_EXPIRED_ACCESS | `constants.py` の `AdminAuditEventType` に追加済み。`auth_guard` 実装は I-4 実装時 |
| I-4 確定（2026-03-18）| 全 OAuth ブロッカー解消。`GET /auth/login` / `POST /auth/callback` 実装可能 |

---

## 1. 実装解禁ツリー（全ブロッカー解消済み）

```
I-4 確定（2026-03-18）✅
  └─ OAuth フロー: Authorization Code + PKCE 確定
       └─ redirect_uri: https://[front]/auth/callback 確定
            └─ GET /auth/login 実装可能
            └─ POST /auth/callback 実装可能
                 └─ セッション発行（HttpOnly Cookie）実装可能
                      └─ POST /auth/totp/verify セッション更新実装可能

I-3 確定（2026-03-18）✅
  └─ POST /auth/totp/setup 実装可能（TotpEncryptor 実装後）
  └─ POST /auth/totp/verify TOTP検証部分実装可能
```

---

## 2. 確定設計詳細

### 2-A: OAuth フロー設計（確定）

#### 2-A-1: Authorization Code Flow with PKCE

**確定内容:**
| 項目 | 確定値 | 備考 |
|---|---|---|
| OAuth フロータイプ | Authorization Code + PKCE | code_verifier はフロントが生成・保持 |
| redirect_uri | `https://[フロントホスト]/auth/callback` | Google Console に登録が必要 |
| state パラメータ | フロントが生成 → sessionStorage に保存 → コールバック時にフロントが照合 → **照合失敗時はバックエンドの callback を呼ばずエラー表示** | CSRF 防止 |
| code_verifier | フロントが生成（43〜128文字ランダム文字列）→ sessionStorage に保存 | Google には送らない |
| code_challenge | `BASE64URL(SHA-256(code_verifier))` をフロントが計算して authorization_url に含める | |
| code exchange | バックエンドが `code + code_verifier` で Google に POST して `id_token` を取得 | フロントは access_token を保持しない |
| OAuth ライブラリ | `authlib` または `google-auth` — I-4 実装時に選定（TODO: OAUTH-LIB）| |

**影響するコード:**
- [trade_app/admin/routes/auth.py](../../trade_app/admin/routes/auth.py) `GET /auth/login` — `authorization_url` の生成（code_challenge を受け取り URL に含める）
- [trade_app/admin/routes/auth.py](../../trade_app/admin/routes/auth.py) `POST /auth/callback` — code + code_verifier で code exchange
- [trade_app/admin/schemas/auth.py](../../trade_app/admin/schemas/auth.py) `GoogleOAuthCallbackRequest` — `{code, code_verifier, state}` を受け取る

#### 2-A-1-x: state 検証責務の分担（2026-03-19 確定）

**フロントエンドの責務（必須）:**
1. `GET /auth/login` 前: `state = crypto.randomUUID()` 等で生成し `sessionStorage.setItem("oauth_state", state)` に保存
2. Google コールバック受信時: URL の `?state=` と `sessionStorage.getItem("oauth_state")` を比較
3. **照合失敗時（不一致 / sessionStorage に値なし）: `POST /auth/callback` を呼ばずエラー表示して停止**
4. 照合成功時のみ `{code, code_verifier, state}` を `POST /auth/callback` に送信

**バックエンドの責務:**
- `state` を body から受け取るが、DB 保存・照合は行わない
- `POST /auth/callback` に届いた時点でフロントの照合は完了済みと見なす
- バックエンドが `state` を使う用途は現状なし（将来の監査ログ追記用に受け取りは維持）

**この分担が成立する前提:**
- SameSite=Lax Cookie により、外部サイトからの CSRF POST は Cookie が送られない
- PKCE の `code_verifier` により、盗まれた `code` 単体での悪用を防止
- フロントが state 照合を省略した場合、CSRF 攻撃に対して無防備になる。フロントの実装は必須。

#### 2-A-2: セッション発行ロジック（確定）

**確定内容:**
| 項目 | 確定値 |
|---|---|
| セッショントークン形式 | UUID v4（`str(uuid.uuid4())`）|
| 発行タイミング | OAuth 認証後（is_2fa_completed=False）→ TOTP 完了後（is_2fa_completed=True）の 2 段階 |
| expires_at の算出 | Pre-2FA = +600s / 認証後 = +28800s（`SESSION_TTL_SEC` / `PRE_2FA_SESSION_TTL_SEC`）|
| トークン返却方法 | **HttpOnly Cookie** (`Set-Cookie: trade_admin_session=<raw_token>`) |
| Cookie 設定詳細 | §3 を参照 |
| auth_guard の読み取り | `Authorization: Bearer` から **Cookie 読み取り**に変更（I-4 実装時）|

**影響するコード:**
- `POST /auth/callback` — OAuth 後の初回セッション発行 → Cookie セット
- `POST /auth/totp/verify` — 2FA 完了後のセッション更新（is_2fa_completed を True、expires_at を +28800s に更新）
- [trade_app/admin/services/auth_guard.py](../../trade_app/admin/services/auth_guard.py) — Cookie 読み取りに変更
- [trade_app/admin/schemas/auth.py](../../trade_app/admin/schemas/auth.py) `GoogleOAuthCallbackRequest` / レスポンス形式

#### 2-A-3: 新規ユーザー作成ポリシー（確定）

**確定: 事前登録必須**

OAuth コールバック時に `ui_users` に登録済みメールが存在しない場合 → **403 Forbidden**。

| ケース | 処理 | HTTP |
|---|---|---|
| ui_users に存在する + is_active=True | ログイン処理を継続 | — |
| ui_users に存在しない | 403 + `"このメールアドレスは登録されていません"` | 403 |
| ui_users に存在する + is_active=False | 403 + `"アカウントが無効化されています"` | 403 |

**登録運用:**
`ui_users` を手動 INSERT するか、管理者 API（`POST /api/admin/ui-users`、Phase 1 では CLI / DB 操作でも可）で事前登録する。

---

### 2-B: I-3 確定後に実装可能（フロントエンドスタック非依存）

> **2026-03-18 更新**: I-3 設計確定済み。`TotpEncryptor` 実装後に 2-B-1 / 2-B-2 の実装を開始できる。

#### 2-B-1: POST /auth/totp/setup

**ブロッカー:** ~~I-3（暗号化方式）~~ → **I-3 設計確定済み**。`TotpEncryptor` 実装待ちのみ。
**実装可能条件:** `TotpEncryptor.encrypt()` が使えれば実装できる

**実装内容（設計確定済み）:**
1. `pyotp.random_base32()` で TOTP シークレット生成
2. `TotpEncryptor.encrypt(secret)` → `totp_secret_encrypted` に保存
3. `totp_enabled = False` のまま（verify 完了後に True にする）
4. `otpauth://totp/...` 形式の URI を返却（Google Authenticator 読み込み用）
5. バックアップコード生成（仕様書で要否を確認すること）

**issuer 名称（2026-03-19 確定）:**
- `TOTP_ISSUER` 設定値（デフォルト: `"TradeSystem Admin"`）
- `otpauth://totp/TradeSystem%20Admin:{email}?secret={secret}&issuer=TradeSystem%20Admin`
- 変更する場合は `.env` の `TOTP_ISSUER=` を設定すること

**バックアップコード（Phase 1 未実装）:**
- Phase 1 は `backup_codes=[]` を返す（空リスト）
- TOTP 紛失時の対応: DB 操作で `totp_secret_encrypted=NULL` / `totp_enabled=FALSE` にリセットし、再ログインを要求

#### 2-B-1-x: TOTP setup 再実行ポリシー（2026-03-19 確定）

| ケース | 動作 | 備考 |
|---|---|---|
| 初回 setup（`totp_enabled=False`）| `totp_secret_encrypted` を新規保存。`totp_enabled` は False のまま | verify 完了後に True になる |
| 再 setup（`totp_enabled=True`）| `totp_secret_encrypted` を上書き。`totp_enabled` は True のまま | 旧 Authenticator は即座に使用不可になる |
| setup 後に verify せず再 setup | `totp_secret_encrypted` を上書き（前の未確認シークレットは消去）| 問題なし |

**Phase 1 のポリシー:**
- アカウントの状態（`totp_enabled` の値）に関わらず setup は常に呼び出し可能（`RequirePreAuth` で保護）
- 再 setup 時は旧シークレットを即座に上書きするため、ユーザーは速やかに新 QR コードを Authenticator に登録する必要がある
- `totp_enabled=True` のユーザーが再 setup を呼ぶと、次の verify が完了するまで 2FA ログインが不可能になる（旧 Authenticator が無効になるため）
- この挙動は「認証済みセッションでの再 setup（設定変更画面）」では問題になりうる。`RequireAdmin`（2FA 完了済み）でのみ再 setup を許可する制限は Phase 2 以降で検討する

#### 2-B-2: POST /auth/totp/verify

**ブロッカー:** I-3 + セッション発行ロジック（2-A-2）→ **全ブロッカー解消済み。実装完了。**

**実装内容（確定・実装済み）:**
1. Cookie から `raw_token` を取得（なければ 401）
2. body の `session_id` でセッションを SELECT
3. `session.session_token_hash == hash_session_token(raw_token)` を検証（二重検証 — セッション ID 詐称防止）
4. `TotpEncryptor.decrypt(user.totp_secret_encrypted)` でシークレット取得
5. `pyotp.TOTP(secret).verify(totp_code, valid_window=1)` で検証（**§2-B-2-x 参照**）
6. 失敗時: `TWO_FA_FAILURE` 監査ログ → 401
7. 成功時: `session.is_2fa_completed = True`、`session.expires_at = now + SESSION_TTL_SEC`
8. `user.totp_enabled = True`
9. `TWO_FA_SUCCESS` 監査ログ
10. 同一 raw_token を Cookie に再セット（`max_age=SESSION_TTL_SEC`）

**確定事項:**
- セッション再発行なし（same token + is_2fa_completed=True 更新）
- Cookie max_age を `PRE_2FA_SESSION_TTL_SEC`（600s）から `SESSION_TTL_SEC`（28800s）に延長

#### 2-B-2-x: valid_window=1 仕様（2026-03-19 確定）

**`pyotp.TOTP.verify(totp_code, valid_window=1)` の意味:**

| パラメータ | 値 | 説明 |
|---|---|---|
| `valid_window` | `1` | 現在の TOTP コード ± 1 ステップ（各 30 秒）を許容 |
| 許容範囲 | ±30 秒 | 前ステップ（−30s）・現ステップ（±0s）・次ステップ（+30s）の計 3 コードが有効 |
| 目的 | 時刻ドリフト吸収 | ユーザーのスマートフォンとサーバーの時計が最大 ±30 秒ずれても認証成功 |
| デフォルト（未指定）| `0` | 現在ステップのみ有効（厳密） |

**採用理由:**
- RFC 6238 は「ネットワーク遅延と時刻同期誤差を考慮して ±1 ステップの許容を推奨」している
- `valid_window=1` は Google Authenticator 等の一般的な 2FA 実装でも標準的に採用される値
- `valid_window=2`（±60s）以上はリプレイ攻撃のウィンドウが広がるため採用しない

**セキュリティ上の注意:**
- `valid_window=1` でも同一コードの再利用（リプレイ）は防止できない
- Phase 1 ではリプレイ攻撃の防止は未実装（使用済みコードの記録なし）
- リプレイ防止が必要な場合は Phase 2 以降で「使用済み TOTP コードキャッシュ（Redis TTL=90s）」を追加する

---

### 2-C: 実装済み（フロント / I-3 非依存）

| エンドポイント | 実装状況 | 備考 |
|---|---|---|
| `POST /auth/logout` | ✅ 実装済み | `UiSession.invalidated_at` を設定するだけ |
| `GET /auth/me` | ✅ 実装済み | セッショントークンからユーザー情報を返す |
| `auth_guard.py` `get_current_admin_user()` | ✅ 実装済み | セッション検証ロジック完成 |
| `hash_session_token()` | ✅ 実装済み | SHA-256 ハッシュ化 |

---

## 3. 実装解禁範囲（I-4 確定後）

### 3-A: 即座に実装可能（ブロッカーなし）

| 実装内容 | 依存 | ファイル |
|---|---|---|
| `GET /auth/login` — authorization_url 生成 | OAuth ライブラリ選定 | `routes/auth.py` |
| `POST /auth/callback` — code exchange + Pre-2FA セッション発行 + Cookie セット | OAuth ライブラリ + `config.py` 更新 | `routes/auth.py` |
| `auth_guard.py` Cookie 読み取りへ変更 | I-4 実装 | `services/auth_guard.py` |
| `SESSION_EXPIRED_ACCESS` 監査ログ記録 | auth_guard 更新 | `services/auth_guard.py` |
| `SESSION_TTL_SEC` / `PRE_2FA_SESSION_TTL_SEC` を `config.py` に追加 | — | `config.py` |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `OAUTH_REDIRECT_URI` を `config.py` に追加 | — | `config.py` |
| GoogleOAuthCallbackRequest スキーマ確定 | — | `schemas/auth.py` |

### 3-B: I-3 実装後に可能

| 実装内容 | 依存 |
|---|---|
| `POST /auth/totp/setup` — TOTP シークレット生成・暗号化 | TotpEncryptor 実装 |
| `POST /auth/totp/verify` — 復号・TOTP 検証・セッション更新 | TotpEncryptor 実装 + I-4 セッション発行 |

---

## 4. 影響ファイル一覧（確定版）

| ファイル | 必要な変更 |
|---|---|
| `routes/auth.py` | `GET /login`, `POST /callback` を 501 スタブから実装へ |
| `schemas/auth.py` | `GoogleOAuthCallbackRequest` = `{code, code_verifier, state}` / レスポンスから Cookie 方式に変更 |
| `services/auth_guard.py` | `Authorization: Bearer` → Cookie 読み取り / SESSION_EXPIRED_ACCESS ログ追加 |
| `config.py` | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `OAUTH_REDIRECT_URI`, `SESSION_TTL_SEC`, `PRE_2FA_SESSION_TTL_SEC` 追加 |
| `requirements.txt` | OAuth ライブラリ（`authlib` 等）+ `pyotp>=2.9.0` + `cryptography>=42.0.0` 追加 |

---

## 5. 現在のセッション発行なしでの動作確認方法

テスト・開発時は以下の手順でセッションを手動作成できる:

```python
# 例: pytest フィクスチャや管理スクリプトでの手動セッション作成
import hashlib, uuid
from datetime import datetime, timedelta, timezone

raw_token = "test-session-token"
session = UiSession(
    id=str(uuid.uuid4()),
    user_id=user.id,
    session_token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
    is_2fa_completed=True,
    expires_at=datetime.now(timezone.utc) + timedelta(hours=8),
)
db.add(session)
await db.commit()
# Authorization: Bearer test-session-token で認証可能になる
```

この方式は `tests/admin/test_auth_routes.py` で既に使用済み。
OAuth 実装なしでも全 admin エンドポイントのテスト・動作確認が可能。

---

## 6. I-4 確定内容（2026-03-18 ユーザー承認済み）

---

### Q1: フロントエンドの構成 → **SPA を採用**

```
✅ (A) SPA（React / Vue / Svelte など）— バックエンドとは別ホストで提供
```

---

### Q2: OAuth フロータイプ → **Authorization Code Flow with PKCE**

```
✅ Authorization Code Flow + PKCE
   code_verifier: フロントエンドが生成・保持
   code exchange: バックエンドが実施（フロントは Google access_token を保持しない）
```

---

### Q3: redirect_uri の形式 → **フロントエンドに直接リダイレクト + バックエンドで code exchange**

```
✅ Google → https://[フロントホスト]/auth/callback?code=xxx&state=yyy
   → フロントが code + code_verifier を POST /auth/callback に送信
   → バックエンドが code exchange 実施
```

---

### Q4: 新規ユーザー作成ポリシー → **事前登録必須**

```
✅ (B) 事前登録必須
   Google 認証成功でも ui_users に登録されていないメールは 403
```

---

### Q5: セッショントークンの返却方式 → **HttpOnly Cookie**

```
✅ (B) HttpOnly Cookie
   - Cookie 名: trade_admin_session
   - HttpOnly: true（JS から読み取り不可）
   - Secure: true（本番環境。開発は HTTP 許可可）
   - SameSite: Lax（OAuth リダイレクト（GET）を許容しつつ CSRF POST を防ぐ）
   - Path: /api/ui-admin/
```

---

### Q6: TOTP バックアップコード → **Phase 1 は未実装**

```
✅ (B) バックアップコードなし（Phase 1）
   TOTP 紛失時は手動対応（DB の totp_secret_encrypted リセット + totp_enabled=False）
```

---

### Q7: TOTP issuer 名称 → **"TradeSystem Admin"**

```
✅ issuer = "TradeSystem Admin"
   otpauth://totp/TradeSystem%20Admin:[user_email]?secret=[secret]&issuer=TradeSystem%20Admin
```

---

### Q8: Google OAuth 認証情報 → **未準備でも実装先行可**

```
✅ 先行実装してから最終接続確認を行う
   - GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / OAUTH_REDIRECT_URI はプレースホルダーで実装
   - 実際の Google Cloud Console 設定は接続確認時に対応
```
