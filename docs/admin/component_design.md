# 管理画面コンポーネント設計書

仕様書: 管理画面仕様書 v0.3
実装フェーズ: Phase 1（先行実装範囲）
最終更新: 2026-03-18 (I-4 確定 — SPA/PKCE/HttpOnly Cookie/事前登録必須 反映)

---

## 0. DB 分離設計（2026-03-18 確定）

### 0.1 設計決定

**admin_db / trade_db の分離を確定とする。**

| | admin_db | trade_db |
|---|---|---|
| **Base クラス** | `AdminBase` (admin/database.py) | `Base` (models/database.py) |
| **Alembic チェーン** | `alembic_admin/` (`alembic_admin.ini`) | `alembic/` (`alembic.ini`) |
| **セッション依存関数** | `get_admin_db()` | `get_trade_db()` |
| **接続設定** | `ADMIN_DATABASE_URL` | `DATABASE_URL` |

### 0.2 admin_db に置くもの（管理画面専用）

| テーブル | 用途 |
|---|---|
| `ui_users` | 管理画面ユーザー（認証・ロール管理）|
| `ui_sessions` | セッショントークン管理（SHA-256 ハッシュ保存）|
| `ui_audit_logs` | 管理画面操作ログ（APPEND ONLY）|
| `symbol_configs` | 銘柄設定（取引パラメータ）|
| `notification_configs` | 通知設定（email / telegram）|

### 0.3 trade_db に残すもの（トレードエンジン）

| テーブル | 用途 |
|---|---|
| `trade_signals`, `orders`, `positions`, `trade_results` | トレードコアデータ |
| `trading_halts` | 取引停止状態（HaltManager が管理）|
| `strategy_definitions`, `strategy_conditions`, `strategy_evaluations` | 戦略エンジン |
| `current_state_snapshots`, `state_evaluations` | 市場状態エンジン |
| `signal_plans`, `signal_strategy_decisions` | シグナル計画・ゲート |
| `audit_logs` | トレードエンジン操作ログ（trade_db 側）|

### 0.4 連携ポイント（DB をまたぐデータアクセス）

| admin 側 | 連携方式 | trade_db 側 |
|---|---|---|
| `halt.py` の halt 操作 | `get_trade_db()` セッション経由 | `trading_halts` |
| `dashboard.py` の集計 | `get_trade_db()` セッション経由 | `orders`, `positions`, `trading_halts`, `trade_results`, `strategy_definitions` |
| `dashboard_service._get_recent_activity` | `get_trade_db()` セッション経由 | `audit_logs`（trade_db 側の監査ログ）|
| `symbol_configs.watched_symbol_count` | TODO(Phase 2): symbol_configs から取得 | — |

### 0.5 直接 JOIN 禁止ルール

**admin_db テーブルと trade_db テーブルを同一 SQL クエリで JOIN してはならない。**

許可される連携方式:
- `get_admin_db()` セッションで admin_db テーブルを操作
- `get_trade_db()` セッションで trade_db テーブルを操作
- 両者のデータが必要な場合は別セッションで個別クエリ → Python で結合

禁止例:
```python
# NG: admin_db セッションで trade_db テーブルを参照
async with AdminAsyncSessionLocal() as db:
    result = await db.execute(
        select(UiAuditLog, TradingHalt)  # 異なる DB のテーブルを JOIN
        .join(TradingHalt, ...)
    )
```

### 0.6 Phase 1 単一コンテナ前提

```
Phase 1 (現在):
  ┌─────────────────────────────────────────────────────────┐
  │  単一 Docker コンテナ + 単一 PostgreSQL インスタンス      │
  │                                                          │
  │  ADMIN_DATABASE_URL == DATABASE_URL                      │
  │    → admin_db テーブルと trade_db テーブルが              │
  │      同一 PostgreSQL DB ファイルに物理共存               │
  │    → 論理分離（AdminBase / 別 migration チェーン）のみ   │
  │                                                          │
  │  system_settings も同様: 単一コンテナのみで有効な        │
  │  in-memory 書き換え（複数コンテナ同期は非対応）          │
  └─────────────────────────────────────────────────────────┘

将来の物理分離（物理配置は未確定）:
  ┌─────────────────────────────────────────────────────────┐
  │  ADMIN_DATABASE_URL を別 PostgreSQL DB/ホストに変更     │
  │  コード変更なし・設定変更のみで物理分離が完了          │
  └─────────────────────────────────────────────────────────┘
```

**Phase 1 の制約（コードに明記）:**
- `system_settings`: 同一コンテナ内のみ有効（`persistence_mode: "runtime_only"`）
- `admin_db` 接続: デフォルトは trade_db と同一 PostgreSQL（論理分離のみ）
- 複数コンテナ環境では各コンテナが独立した in-memory 値を持つ（非対応）

### 0.7 admin 用 migration / alembic 構成

```
trade-system/
├── alembic.ini              ← trade_db 用設定
├── alembic/
│   ├── env.py               ← Base.metadata を対象
│   └── versions/
│       ├── 001_initial_schema.py
│       └── ... (011まで)
│
├── alembic_admin.ini        ← admin_db 用設定 ← NEW
└── alembic_admin/           ← NEW
    ├── env.py               ← AdminBase.metadata を対象
    ├── script.py.mako       ← 標準テンプレート
    └── versions/
        └── 001_admin_initial.py  ← 管理画面全テーブル作成
```

**実行コマンド:**
```bash
# trade_db migration
alembic upgrade head

# admin_db migration
alembic -c alembic_admin.ini upgrade head

# Docker 環境
docker compose exec trade_app alembic upgrade head
docker compose exec trade_app alembic -c alembic_admin.ini upgrade head
```

---

## 1. 全体構造

```
trade_app/
└── admin/
    ├── __init__.py              # パッケージ説明・DB 分離設計メモ
    ├── database.py              # AdminBase / AdminAsyncSessionLocal / get_admin_db / get_trade_db
    ├── constants.py             # 全定数・Enum 定義
    ├── router.py                # サブルーター集約 → /api/ui-admin/*
    ├── models/
    │   ├── ui_user.py           # 管理ユーザー（ui_users テーブル）
    │   ├── ui_session.py        # セッション（ui_sessions テーブル）
    │   ├── ui_audit_log.py      # 監査ログ APPEND ONLY（ui_audit_logs テーブル）
    │   ├── symbol_config.py     # 銘柄設定（symbol_configs テーブル）
    │   └── notification_config.py # 通知設定（notification_configs テーブル）
    ├── schemas/
    │   ├── common.py            # PaginationQuery / PaginatedResponse[T] / MessageResponse
    │   ├── auth.py              # OAuth/TOTP スキーマ（TODO I-4 スタブ）
    │   ├── symbol_config.py     # 銘柄設定 CRUD スキーマ
    │   ├── notification_config.py # 通知設定 CRUD スキーマ
    │   ├── audit_log.py         # 監査ログ照会スキーマ
    │   ├── dashboard.py         # ダッシュボード集計スキーマ
    │   └── system_settings.py   # システム設定スキーマ
    ├── services/
    │   ├── auth_guard.py        # 認証ガード（RequireAdmin 依存関数）
    │   ├── audit_log_service.py # UiAuditLogService（APPEND ONLY）
    │   ├── symbol_config_service.py # SymbolConfigService（CRUD）
    │   ├── notification_service.py  # NotificationConfigService（CRUD）
    │   └── dashboard_service.py     # DashboardService（集計）
    └── routes/
        ├── auth.py              # /auth/* （TODO I-4 スタブ）
        ├── dashboard.py         # /dashboard
        ├── symbols.py           # /symbols/*
        ├── notifications.py     # /notifications/*
        ├── audit_logs.py        # /audit-logs/*
        ├── system_settings.py   # /system-settings
        └── halt.py              # /halt/*
```

---

## 2. 認証フロー（I-4 確定版）

### 2.1 全体フロー（Authorization Code Flow with PKCE + HttpOnly Cookie）

```
SPA フロントエンド             バックエンド API              Google OAuth
       │                            │                              │
       │ GET /auth/login            │                              │
       │ ?code_challenge=xxx        │                              │
       │ &code_challenge_method=S256│                              │
       │ &state=yyy                 │                              │
       │ ─────────────────────────>│                              │
       │                            │ authorization_url を構築     │
       │ <─ { authorization_url }   │ (client_id, redirect_uri,    │
       │                            │  code_challenge, state 含む) │
       │                            │                              │
       │ ── authorization_url へ redirect ───────────────────────>│
       │    (code_verifier は sessionStorage に保存)               │
       │                            │                              │
       │ <─ Google 認証完了 ─────────────────────────────────────│
       │    https://[front]/auth/callback?code=xxx&state=yyy       │
       │                            │                              │
       │ state を sessionStorage と照合（CSRF チェック）           │
       │                            │                              │
       │ POST /auth/callback        │                              │
       │ { code, code_verifier,     │                              │
       │   state }                  │                              │
       │ ─────────────────────────>│                              │
       │                            │ code + code_verifier で      │
       │                            │ ─────────────────────────── >│
       │                            │ token exchange               │
       │                            │ <─── { id_token }            │
       │                            │                              │
       │                            │ id_token から email 取得     │
       │                            │ ui_users に存在するか確認    │
       │                            │ セッション発行（Pre-2FA）    │
       │                            │ expires_at = now + 600s      │
       │ <─ 200 OK                  │                              │
       │    Set-Cookie:             │                              │
       │    trade_admin_session=... │                              │
       │    { requires_totp_setup:  │                              │
       │      bool }                │                              │
       │                            │                              │
       │ POST /auth/totp/verify     │                              │
       │ Cookie: trade_admin_session│                              │
       │ { totp_code }              │                              │
       │ ─────────────────────────>│                              │
       │                            │ TOTP 検証                    │
       │                            │ is_2fa_completed=True        │
       │                            │ expires_at = now + 28800s    │
       │ <─ 200 OK                  │                              │
       │    { user info }           │                              │
       │                            │                              │
       │ 通常の管理 API 呼び出し    │                              │
       │ Cookie: trade_admin_session│                              │
       │ ─────────────────────────>│                              │
       │                            │ auth_guard: Cookie 読み取り  │
       │                            │ → session_token → hash → DB │
       │                            │ → is_valid チェック          │
       │ <─ 200 OK                  │                              │
```

### 2.2 フロントエンド / バックエンド責務分担

| 処理 | 担当 | 詳細 |
|---|---|---|
| `code_verifier` 生成 | **フロント** | 43〜128 文字ランダム文字列。sessionStorage に保存 |
| `code_challenge` 計算 | **フロント** | `BASE64URL(SHA-256(code_verifier))` |
| `state` 生成 | **フロント** | CSRF 防止トークン。sessionStorage に保存 |
| `state` 検証（CSRF チェック） | **フロント** | コールバック受信時に sessionStorage と照合 |
| authorization_url 構築 | **バックエンド** | `GOOGLE_CLIENT_ID`, `OAUTH_REDIRECT_URI`, scope, code_challenge, state を含める |
| code exchange | **バックエンド** | `code` + `code_verifier` を Google に POST → `id_token` 取得 |
| メールアドレス検証 | **バックエンド** | `id_token` から email 取得 → `ui_users` 照合 |
| セッション発行 | **バックエンド** | UUID v4 生成 → SHA-256 ハッシュ → DB 保存 → Cookie セット |
| TOTP シークレット暗号化 | **バックエンド** | AES-256-GCM（I-3 設計、TotpEncryptor 実装後）|
| TOTP 検証 | **バックエンド** | `pyotp.TOTP(secret).verify(code)` |
| Cookie 保持 | **フロントは保持しない** | HttpOnly Cookie のためブラウザが自動送信 |

### 2.3 Cookie 設計（HttpOnly Cookie）

```
Cookie 名: trade_admin_session
値:        <raw_session_token>（UUID v4 の平文）
           ※ DB には SHA-256(raw_token) のみ保存

属性:
  HttpOnly    true   — JS から document.cookie でアクセス不可
  Secure      true   — HTTPS 通信時のみ送信（開発環境は HTTP 許可可）
  SameSite    Lax    — 同一オリジンリクエスト + クロスサイト GET（OAuth リダイレクト）を許可
                       クロスサイト POST は拒否（CSRF 対策）
  Path        /api/ui-admin/   — 管理 API パスのみに限定

フロントエンドの設定:
  fetch() オプション: credentials: 'include'（Cookie を自動送信するために必要）
  CORS: Access-Control-Allow-Origin に管理画面ホストを明示（* は不可）
        Access-Control-Allow-Credentials: true
```

### 2.4 auth_guard の動作（I-4 実装後）

```python
# auth_guard.get_current_admin_user() — Cookie 読み取り版
# Cookie "trade_admin_session" からトークンを取得
# （現在の Authorization: Bearer から変更）

raw_token = request.cookies.get("trade_admin_session")
  ↓
token_hash = sha256(raw_token)
  ↓
SELECT ui_sessions WHERE session_token_hash = token_hash AND invalidated_at IS NULL
  ↓
session.expires_at < now → SESSION_EXPIRED_ACCESS を ui_audit_logs に記録 → 401
session.is_2fa_completed=False → 401（Pre-2FA セッションは通常 API を拒否）
  ↓
SELECT ui_users WHERE id = session.user_id AND is_active = TRUE
  ↓
AdminUser(user_id, email, display_name, role, session_id)
```

### 2.5 認証失敗ケース一覧

| ケース | HTTP | レスポンス | 監査ログ |
|---|---|---|---|
| Google 認証失敗（error パラメータあり）| 400 | `"OAuth 認証がキャンセルまたは失敗しました"` | LOGIN_FAILURE |
| state 不一致（CSRF）| 400 | `"認証リクエストが無効です"` | LOGIN_FAILURE |
| code exchange 失敗（Google API エラー）| 502 | `"認証サービスとの通信に失敗しました"` | LOGIN_FAILURE |
| id_token から email 取得失敗 | 502 | `"認証情報の取得に失敗しました"` | LOGIN_FAILURE |
| **ui_users に未登録メール** | **403** | `"このメールアドレスは登録されていません"` | LOGIN_FAILURE |
| **ui_users に存在するが is_active=False** | **403** | `"アカウントが無効化されています"` | LOGIN_FAILURE |
| TOTP コード不正 | 401 | `"認証コードが正しくありません"` | TWO_FA_FAILURE |
| TOTP セッション期限切れ（Pre-2FA 10分超過）| 401 | `"セッションが期限切れです。再ログインしてください"` | SESSION_EXPIRED_ACCESS |
| 完全認証後セッション期限切れ（8時間超過）| 401 | `"セッションが期限切れです。再ログインしてください"` | SESSION_EXPIRED_ACCESS |
| Cookie なし / トークン不正 | 401 | `"認証が必要です"` | — |

### 2.6 セッション有効期限（I-5 確定）

```
OAuth 認証後（is_2fa_completed=False）
  expires_at = created_at + 600 秒（10 分）
  → POST /auth/totp/verify のみ許可（その他は 401）

TOTP 認証後（is_2fa_completed=True に更新）
  expires_at = now + 28800 秒（8 時間）
  → 同一セッション・同一 Cookie トークンを継続使用

期限切れアクセス検出時
  → SESSION_EXPIRED_ACCESS を ui_audit_logs に記録してから 401 を返す
```

**I-3 確定（2026-03-18）:** 暗号化方式 = AES-256-GCM。詳細は `docs/admin/design_i3_encryption.md`。
**I-4 確定（2026-03-18）:** OAuth フロー・Cookie 設計・事前登録必須。詳細は `docs/admin/design_i4_auth_gaps.md`。
**I-5 確定（2026-03-18）:** セッション有効期限 = Absolute Timeout。詳細は `docs/admin/design_i5_session.md`。

---

## 3. エンドポイント一覧

### プレフィックス: `/api/ui-admin`

| メソッド | パス | 説明 | 認証 |
|---|---|---|---|
| GET | `/auth/me` | 現在ユーザー情報 | 必須 |
| GET | `/auth/login` | authorization_url 返却（PKCE 対応）**実装解禁** | 不要 |
| POST | `/auth/callback` | code exchange + Pre-2FA セッション発行 + Cookie セット **実装解禁** | 不要 |
| POST | `/auth/totp/setup` | TOTP セットアップ（I-3 実装後に解禁） | 必須 |
| POST | `/auth/totp/verify` | TOTP 検証 + セッション更新（I-3 実装後に解禁） | 必須（Pre-2FA Cookie）|
| POST | `/auth/logout` | ログアウト（✅ 実装済み） | 必須 |
| GET | `/dashboard` | ダッシュボード全データ | 必須 |
| GET | `/symbols` | 銘柄設定一覧（フィルタ・ページ）| 必須 |
| POST | `/symbols` | 銘柄設定作成 | 必須 |
| GET | `/symbols/{id}` | 銘柄設定1件 | 必須 |
| PATCH | `/symbols/{id}` | 銘柄設定更新 | 必須 |
| DELETE | `/symbols/{id}` | 銘柄設定論理削除 | 必須 |
| PATCH | `/symbols/{id}/enable` | 有効化 | 必須 |
| PATCH | `/symbols/{id}/disable` | 無効化 | 必須 |
| GET | `/notifications` | 通知設定一覧 | 必須 |
| POST | `/notifications` | 通知設定作成 | 必須 |
| GET | `/notifications/{id}` | 通知設定1件 | 必須 |
| PATCH | `/notifications/{id}` | 通知設定更新 | 必須 |
| DELETE | `/notifications/{id}` | 通知設定削除 | 必須 |
| POST | `/notifications/{id}/test` | テスト送信（スタブ） | 必須 |
| GET | `/audit-logs` | 監査ログ一覧（フィルタ・ページ）| 必須 |
| GET | `/audit-logs/export` | CSV エクスポート（最大5000件）| 必須 |
| GET | `/audit-logs/{id}` | 監査ログ詳細 | 必須 |
| GET | `/system-settings` | システム設定一覧 | 必須 |
| PATCH | `/system-settings` | システム設定更新 | 必須 |
| GET | `/halt` | アクティブ halt 一覧 | 必須 |
| POST | `/halt` | 手動 halt 発動 | 必須 |
| DELETE | `/halt/{id}` | 指定 halt 解除 | 必須 |
| DELETE | `/halt` | 全 halt 解除 | 必須 |

**既存 `/api/admin/*` との差異:**
- `/api/admin/*` (routes/admin.py): API_TOKEN 認証（分析システム向け）
- `/api/ui-admin/*` (admin/router.py): セッショントークン認証（管理画面 UI 向け）
- halt 操作は両系統から実行可能（HaltManager を共用）

---

## 4. データモデル

### 4.1 UiUser（ui_users テーブル）

| カラム | 型 | 説明 |
|---|---|---|
| id | String(36) PK | UUID |
| email | String(255) UNIQUE NOT NULL | Google OAuth メール |
| display_name | String(128) | 表示名 |
| role | String(32) | admin / operator / viewer（Phase 1 は admin のみ） |
| is_active | Boolean | アカウント有効フラグ |
| totp_secret_encrypted | Text | TOTP シークレット（AES-256-GCM 暗号化 / 保存形式 `gv1:<base64url>`）TODO(I-3 実装待ち) |
| totp_enabled | Boolean | TOTP 設定完了フラグ |
| last_login_at | DateTime | 最終ログイン日時 |

### 4.2 UiSession（ui_sessions テーブル）

| カラム | 型 | 説明 |
|---|---|---|
| id | String(36) PK | UUID |
| user_id | String(36) FK → ui_users.id | |
| session_token_hash | String(64) UNIQUE NOT NULL | SHA-256 ハッシュ（平文非保存）|
| ip_address | String(45) | 発行時 IP（IPv6対応）|
| user_agent | Text | 発行時 UA |
| is_2fa_completed | Boolean | 2FA 完了フラグ |
| expires_at | DateTime NOT NULL | 有効期限（Absolute Timeout / Pre-2FA: +600s / 認証後: +28800s）|
| invalidated_at | DateTime NULL | 無効化日時（NULL=有効）|

`is_valid` プロパティ: `invalidated_at IS NULL AND is_2fa_completed AND expires_at > now`

### 4.3 UiAuditLog（ui_audit_logs テーブル、APPEND ONLY）

| カラム | 型 | 説明 |
|---|---|---|
| id | String(36) PK | UUID |
| user_id | String(36) NULL | 操作者（システム自動は NULL）|
| user_email | String(255) NULL | 非正規化（JOIN なし照会用）|
| event_type | String(64) NOT NULL | AdminAuditEventType の値 |
| resource_type | String(64) NULL | 対象リソース種別 |
| resource_id | String(36) NULL | 対象リソース ID |
| resource_label | String(255) NULL | 人間可読ラベル |
| ip_address | String(45) NULL | クライアント IP（システム自動は NULL）|
| user_agent | Text NULL | クライアント UA |
| before_json | JSON NULL | 変更前状態（秘密情報除去済み）|
| after_json | JSON NULL | 変更後状態（秘密情報除去済み）|
| description | Text NULL | 補足説明 |
| created_at | DateTime NOT NULL | |

**APPEND ONLY 保証:** `write()` のみ INSERT。UPDATE/DELETE メソッドは定義しない。

### 4.4 SymbolConfig（symbol_configs テーブル）

| カラム | 型 | 説明 |
|---|---|---|
| id | String(36) PK | UUID |
| symbol_code | String(32) UNIQUE NOT NULL | 銘柄コード（作成後変更不可）|
| symbol_name | String(128) NULL | 銘柄名（表示補助）|
| trade_type | String(32) NOT NULL | daytrading / swing |
| strategy_id | String(36) NULL | 紐付け戦略（FK なし、TODO O-4: strategy_configs 確定後）|
| is_enabled | Boolean DEFAULT FALSE | 有効フラグ |
| open_behavior | String(32) NULL | オープン動作 |
| trading_start_time | Time NULL | 取引開始時刻（JST）|
| trading_end_time | Time NULL | 取引終了時刻（JST）|
| max_single_investment_jpy | Integer NULL | 1回最大投資額 |
| max_daily_investment_jpy | Integer NULL | 日次最大投資額 |
| take_profit_pct | Numeric(5,2) NOT NULL | 利確率（%）|
| stop_loss_pct | Numeric(5,2) NOT NULL | 損切率（%）|
| max_hold_minutes | Integer NOT NULL | 最大保有時間（分）|
| created_by | String(36) NULL | 作成者 ID（FK なし、TODO I-4: OAuth 完了後）|
| updated_by | String(36) NULL | 更新者 ID |
| deleted_at | DateTime NULL | 論理削除日時（NULL=有効）|

### 4.5 NotificationConfig（notification_configs テーブル）

| カラム | 型 | 説明 |
|---|---|---|
| id | String(36) PK | UUID |
| channel_type | String(32) NOT NULL | email / telegram |
| destination | String(512) NOT NULL | 送信先（メールアドレス/チャンネル ID）|
| is_enabled | Boolean DEFAULT FALSE | 有効フラグ |
| events_json | JSON NOT NULL | NotificationEventCode の配列 |
| created_by | String(36) NULL | 作成者 ID |
| updated_by | String(36) NULL | 更新者 ID |

---

## 5. サービス設計

### 5.1 UiAuditLogService

**責務:** 管理画面操作の全記録（APPEND ONLY）。

**秘密情報除去（_sanitize）:**
```python
SENSITIVE_KEYS = {"password", "totp_secret", "session_token", "api_key", ...}
# before_json / after_json から SENSITIVE_KEYS を [REDACTED] に置換
# ネスト dict に再帰適用
```

**IP アドレス記録ルール:**
- `USER_INITIATED_EVENTS`: `ip_address` が None の場合は警告ログを記録するが INSERT は継続
- `SYSTEM_INITIATED_EVENTS`: `ip_address` は NULL で保存

### 5.2 SymbolConfigService

**設計制約:**
- `symbol_code` は作成後変更不可（`update()` で上書き試行しても `symbol_code` フィールドは無視される）
- 削除は論理削除のみ（`deleted_at` 設定）
- `get()` / `list()` はデフォルトで `deleted_at IS NULL` をフィルタ
- `strategy_id` は文字列保存のみ（FK 制約なし、TODO I-1/O-4）

**返戻値パターン:**
```python
create() → (SymbolConfig, after_json)
update() → (SymbolConfig, before_json, after_json)
soft_delete() → (SymbolConfig, before_json)
```
呼び出し元（ルーター）が before/after を使って `UiAuditLogService.write()` を呼ぶ。

### 5.2.1 NotificationConfig destination バリデーション

`NotificationConfigBase` の `@model_validator(mode="after")` で channel_type 別に検証:

| channel_type | 形式 | 検証ルール |
|---|---|---|
| email | `user@example.com` | `@` 1個、ローカルパート非空、ドメインに `.` を含む |
| telegram | `@username` | `@[A-Za-z0-9_]{5,32}` |
| telegram | チャットID | `-?[0-9]{1,20}`（負値グループID対応）|

スキーマレベルで拒否するため、サービス・DB に不正値が到達しない。

### 5.3 DashboardService

**TODO(I-1/T-1/I-3):** `_get_environment_banner()` は `broker_connection_configs` 実装まで "not_configured" を固定返却。

**集計対象:**
- `trade_db` の既存テーブルのみ（orders, positions, trading_halts, trade_results, audit_logs, strategy_definitions）
- `symbol_configs` テーブルは I-1 確定後に migration → その後 `watched_symbol_count` を更新

**JST 日付計算（優先順位）:**
1. `zoneinfo.ZoneInfo("Asia/Tokyo")` — Python 3.9+ stdlib (`tzdata` パッケージ要）
2. UTC+9 固定オフセット算術 — tzdata 未整備環境のフォールバック

`pytz` は使用しない（Docker イメージに未インストール）。`requirements.txt` に `tzdata>=2024.1` を追加済み。

---

## 6. 定数設計

### AdminAuditEventType の分類

| 分類 | イベント例 | IP/UA 記録 |
|---|---|---|
| 認証系 | LOGIN_SUCCESS, TWO_FA_FAILURE, LOGOUT | 必須 |
| 銘柄管理 | SYMBOL_CREATED, SYMBOL_DISABLED | 必須 |
| 通知設定 | NOTIFICATION_CONFIG_CREATED, NOTIFICATION_TEST_SENT | 必須 |
| halt 操作 | HALT_TRIGGERED_MANUAL, HALT_RELEASED | 必須 |
| システム設定 | SYSTEM_SETTINGS_UPDATED | 必須 |
| 監査ログ操作 | AUDIT_LOG_EXPORTED | 必須（機密ダウンロード）|
| システム自動 | HALT_TRIGGERED_AUTO, SYSTEM_ERROR_DETECTED | NULL 可 |

### NotificationEventCode（7種類）

| コード | 説明 |
|---|---|
| ORDER_FILLED | 約定発生 |
| ORDER_ERROR | 注文エラー |
| HALT_TRIGGERED | 緊急停止（halt 発動）|
| HALT_RELEASED | halt 解除 |
| BROKER_DISCONNECTED | 証券接続断 |
| DAILY_PNL_REPORT | 日次損益レポート |
| SYSTEM_ERROR | システムエラー |

---

## 7. 未実装事項（TODO）

### 確定済み・実装待ち

| ID | 内容 | 依存 |
|---|---|---|
| TODO(I-3) | `TotpEncryptor` 実装（`trade_app/admin/services/encryption.py`）| I-3 設計確定済み → 実装可能 |
| TODO(I-3) | `POST /auth/totp/setup` 実装（TOTP シークレット生成・暗号化・DB保存）| I-3 実装後 |
| TODO(I-3) | `POST /auth/totp/verify` 実装（復号・TOTP 検証・セッション更新）| I-3 実装後 |
| TODO(I-3) | `requirements.txt` に `cryptography>=42.0.0` / `pyotp>=2.9.0` 追加 | I-3 実装時 |
| TODO(I-3) | `config.py` に `TOTP_ENCRYPTION_KEY: str = ""` 追加 | I-3 実装時 |
| TODO(I-3) | `.env.example` に `TOTP_ENCRYPTION_KEY=` 追加 | I-3 実装時 |
| TODO(I-4) | `GET /auth/login` 実装（PKCE 対応 authorization_url 生成）| **I-4 確定済み → 実装可能** |
| TODO(I-4) | `POST /auth/callback` 実装（code exchange + Pre-2FA セッション発行 + Cookie）| **I-4 確定済み → 実装可能** |
| TODO(I-4) | `auth_guard.py` を Cookie 読み取りに変更 + SESSION_EXPIRED_ACCESS 監査ログ追加 | I-4 実装時 |
| TODO(I-4) | `config.py` に `SESSION_TTL_SEC=28800` / `PRE_2FA_SESSION_TTL_SEC=600` / `GOOGLE_CLIENT_ID` 等追加 | I-4 実装時 |
| TODO(OAUTH-LIB) | OAuth ライブラリ選定（`authlib` 推奨）・`requirements.txt` 追加 | I-4 実装時 |
| TODO(T-1) | broker_connection_configs テーブル・モデル | T-1 フィールド確定後 |
| TODO(T-1) | 環境バナー実装（EnvironmentBanner を実環境から取得）| T-1 確定後 |
| TODO(T-3) | 接続テスト API エンドポイント実装 | T-3 立花証券 API 確認後 |
| TODO | 通知送信実装（メール/Telegram）| 通知サービス設計後 |
| TODO(Phase 2) | operator/viewer ロール分岐 | Phase 2 |
| TODO(Phase 2) | システム設定の .env 永続化 | Phase 2 |
| TODO(物理配置確定後) | admin_db 物理分離（ADMIN_DATABASE_URL を別ホスト/DB に変更）— 物理配置は未確定 | 物理配置確定後 |
| TODO(I-4 完了後) | created_by / updated_by に FK 制約追加（admin_db 内完結 / I-4 完了前は実ユーザー不在のため不可）| I-4 完了後 |
| TODO(Phase 2) | ダッシュボード watched_symbol_count を symbol_configs から取得 | symbol_configs 実装後 |
| TODO(Phase 1 後) | 期限切れ ui_sessions レコードの定期クリーンアップ | Phase 1 では未実装で可 |

### 解消済み

~~TODO `alembic_admin upgrade head`~~ → **2026-03-18 完了**: `alembic_version_admin = a1b2c3d4e5f6`、全5テーブル確認済み。

~~TODO(I-1) DB統合方針未確定~~ → **2026-03-18 確定**: admin_db / trade_db 分離。`AdminBase` + `alembic_admin/` 実装済み。

~~TODO(I-3) 暗号化方式未確定~~ → **2026-03-18 確定**: AES-256-GCM。詳細は `docs/admin/design_i3_encryption.md`。

~~TODO(I-5) セッションタイムアウト未確定~~ → **2026-03-18 確定**: Absolute Timeout / Pre-2FA 600s / 認証後 28800s。詳細は `docs/admin/design_i5_session.md`。

~~TODO(I-4) フロントエンドスタック未確定~~ → **2026-03-18 確定**: SPA / PKCE / HttpOnly Cookie / 事前登録必須 / TOTP issuer=TradeSystem Admin。詳細は `docs/admin/design_i4_auth_gaps.md`。

---

## 7.1 監査ログ書き込みポイント一覧

### 実装済み（Phase 1）

| エンドポイント | イベントタイプ | 書き込みタイミング |
|---|---|---|
| `POST /auth/logout` | LOGOUT | セッション無効化後、commit 前 |
| `POST /symbols` | SYMBOL_CREATED | 作成後、commit 前 |
| `PATCH /symbols/{id}` | SYMBOL_UPDATED | 更新後、commit 前 |
| `DELETE /symbols/{id}` | SYMBOL_DELETED | 論理削除後、commit 前 |
| `PATCH /symbols/{id}/enable` | SYMBOL_ENABLED | 有効化後、commit 前 |
| `PATCH /symbols/{id}/disable` | SYMBOL_DISABLED | 無効化後、commit 前 |
| `POST /notifications` | NOTIFICATION_CONFIG_CREATED | 作成後、commit 前 |
| `PATCH /notifications/{id}` | NOTIFICATION_CONFIG_UPDATED | 更新後、commit 前 |
| `DELETE /notifications/{id}` | NOTIFICATION_CONFIG_DELETED | 削除後、commit 前 |
| `POST /notifications/{id}/test` | NOTIFICATION_TEST_SENT/FAILED | テスト実行後、commit 前 |
| `PATCH /system-settings` | SYSTEM_SETTINGS_UPDATED | in-memory 書き換え後、commit 前 |
| `POST /halt` | HALT_TRIGGERED_MANUAL | halt 発動後、commit 前 |
| `DELETE /halt/{id}` | HALT_RELEASED | halt 解除後、commit 前 |
| `DELETE /halt` | HALT_RELEASED | 全 halt 解除後、commit 前 |
| `GET /audit-logs/export` | AUDIT_LOG_EXPORTED | CSV 生成後・StreamingResponse 返却前 |

### 未実装（ブロッカー別）

| イベントタイプ | 未実装理由 | ブロッカー |
|---|---|---|
| LOGIN_SUCCESS / LOGIN_FAILURE | OAuth ログインフロー未実装 | TODO(I-4) |
| TWO_FA_SUCCESS / TWO_FA_FAILURE | TOTP 検証未実装 | TODO(I-3 実装) |
| SESSION_EXPIRED_ACCESS | auth_guard の期限切れ検出時に記録 — **設計確定済み**（I-5）。実装は I-4 セッション発行実装と同タイミング | TODO(I-4 実装時) |
| SESSION_INVALIDATED | 手動無効化（logout 以外）時のログ記録 | TODO(I-4 実装時) |
| BROKER_CONFIG_UPDATED / BROKER_CONNECTION_TEST | broker_connection_configs 未実装 | TODO(T-1/T-3) |
| STRATEGY_CREATED / UPDATED / ENABLED / DISABLED | 戦略管理 UI 未実装 | Phase 2 以降 |
| HALT_TRIGGERED_AUTO | HaltManager 自動発動時は既存 audit_logs テーブルに記録（ui_audit_logs には未記録）| 設計要確認 |

### 読み取り操作（監査不要・設計確認済み）

GET 系エンドポイント（一覧・詳細取得）は監査ログを記録しない。例外: `GET /audit-logs/export` は機密ダウンロードのため AUDIT_LOG_EXPORTED を記録する。

---

## 8. テスト構成

```
tests/admin/
├── __init__.py
├── test_constants.py              # 定数・Enum 整合性（11件）
├── test_audit_log_service.py      # _sanitize + write/query/get（21件）
├── test_symbol_config_service.py  # CRUD 全操作（26件）
├── test_notification_service.py   # CRUD + validate + send_test + destination 検証（32件）
├── test_dashboard_routes.py       # _today_jst_range（zoneinfo）+ service + schema（17件）
├── test_auth_routes.py            # hash_session_token + logout + get_me（10件）
├── test_system_settings.py        # persistence_mode + runtime update + audit（18件）
└── test_audit_log_export.py       # CSV export audit + _common helpers（17件）
```

**合計: 145 件**（DB 分離後も変更なし。テストは conftest.py の AdminBase.metadata.create_all() 対応済み）

テスト実行:
```bash
docker compose run --rm trade_app pytest tests/admin/ -q
```

---

## 9. セキュリティ設計

### セッショントークンの安全性

- 平文トークンは DB に保存しない
- `sha256(raw_token)` をハッシュキーとして `ui_sessions` に保存
- **I-4 確定（2026-03-18）**: `Authorization: Bearer` → **HttpOnly Cookie** (`trade_admin_session`) に変更
- Cookie 値（raw_token）を受信 → ハッシュ化 → DB 検索

### HttpOnly Cookie + CORS 設計（I-4 確定）

```
Cookie 属性:
  HttpOnly    true
  Secure      true（本番環境）/ false（開発環境 HTTP）
  SameSite    Lax
  Path        /api/ui-admin/

FastAPI CORS 設定（main.py 更新が必要）:
  allow_origins     = ["https://[管理画面ホスト]"]  # * は不可（credentials 使用時）
  allow_credentials = True
  allow_methods     = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
  allow_headers     = ["Content-Type"]

フロントエンド fetch() 設定:
  credentials: 'include'  # Cookie を自動送信するために必須
```

**SameSite=Lax を選んだ理由:**
- `Strict` にすると Google OAuth の redirect（外部サイトからの GET）で Cookie が送信されなくなる
- `None` にすると CSRF リスクが高まる（Secure 必須）
- `Lax` はトップレベルナビゲーションの GET に Cookie を送るためOAuth redirect に対応しつつ、クロスサイト POST を防ぐ

### 秘密情報の監査ログ除外

```python
SENSITIVE_KEYS = {
    "password", "password_encrypted",
    "extra_secrets_encrypted",
    "totp_secret", "totp_secret_encrypted",
    "session_token", "session_token_hash",
    "api_key", "secret", "access_token", "refresh_token",
}
```

上記キーは `before_json` / `after_json` に含まれていても `[REDACTED]` に置換。

### IP アドレスの取得

`trade_app/admin/routes/_common.py` の `get_client_ip(request)` で一元管理する。

```python
# X-Forwarded-For ヘッダー優先（リバースプロキシ後ろの実 IP）
# → routes/_common.py: get_client_ip(request) / get_user_agent(request)
```

全ルートファイルは `from trade_app.admin.routes._common import get_client_ip, get_user_agent` でインポートする（ローカル定義は禁止）。

### admin API エラーレスポンス規約

すべての 4xx/5xx は FastAPI 標準の `{"detail": "<日本語メッセージ>"}` 形式を使用する。

| HTTP ステータス | 用途 |
|---|---|
| 400 | 論理的不正入力（スキーマは通るがビジネスルール違反）|
| 401 | 認証失敗・セッション無効 |
| 403 | ロール不足（Phase 2 以降）|
| 404 | リソースが存在しない |
| 409 | リソース作成時の一意制約違反（symbol_code 重複等）|
| 422 | Pydantic バリデーションエラー（FastAPI 自動）|
| 500 | 予期しないサーバーエラー |

`ValueError` を `except` する場合: 意味に応じて 400/404/409 を明示的に返す。
Pydantic バリデーション失敗（422）は FastAPI が自動で返すため、ルーター側での処理不要。

### system_settings の永続化（暫定実装）

`PATCH /system-settings` は `lru_cache` シングルトンを `object.__setattr__()` で書き換える。

- **有効範囲**: 同一プロセス内（同一コンテナ内）のみ
- **無効になるケース**: プロセス再起動、Docker コンテナ再起動、複数コンテナ環境
- **永続化されない**: `.env` ファイル・DB のいずれにも書き込まない
- **レスポンス**: `persistence_mode: "runtime_only"` と `persistence_note` を常に含める
- **複数コンテナ同期**: 非対応（Phase 1 は単一コンテナを前提とする）

**Phase 1 単一コンテナ前提の明示（コードコメント）:**
`trade_app/admin/routes/system_settings.py` の module docstring に明記済み:
```
制約:
  - Docker コンテナが複数起動している場合、各コンテナの in-memory 値は独立する
  - .env への書き込みは行わない
  - DB への永続化は行わない
```

TODO(Phase 2): `.env` ファイルへの書き込みまたは DB 永続化を実装する。

---

*作成日: 2026-03-18 / 最終更新: 2026-03-18*
*DB 分離設計確定: 2026-03-18 (AdminBase / alembic_admin/ / get_admin_db / get_trade_db 実装完了)*
*実装者: Claude Sonnet 4.6 (管理画面 Phase 1 先行実装)*
