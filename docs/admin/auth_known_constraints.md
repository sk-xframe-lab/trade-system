# auth 周辺の既知制約一覧

作成日: 2026-03-19
対象: Phase 1 管理画面 — Google OAuth + TOTP 2FA + セッション管理
ステータス: Phase 1 設計確定済み。Phase 2 以降での対応を記録。

---

## 1. Phase 1 未実装事項

### 1-1. TOTP バックアップコード

| 項目 | 現状 | Phase 1 の対処方法 |
|---|---|---|
| バックアップコード生成 | 未実装。`backup_codes=[]` を返す | DB 操作でリセット（下記参照）|
| バックアップコードでのログイン | 未実装 | — |

**TOTP 紛失時の対処（Phase 1）:**
```sql
-- totp_secret_encrypted / totp_enabled をリセット
-- ユーザーは次回ログイン時に TOTP 再設定が必要
UPDATE ui_users
SET totp_secret_encrypted = NULL,
    totp_enabled = FALSE,
    updated_at = NOW()
WHERE email = 'user@example.com';
```

**Phase 2 以降の対応案:** TOTP 設定画面から管理者がリセット可能にする / バックアップコード（8文字×8本）を生成してハッシュ化保存する

---

### 1-2. TOTP リプレイ攻撃防止

| 項目 | 現状 | リスク |
|---|---|---|
| 使用済み TOTP コードの記録 | 未実装 | 30 秒以内に同一コードを再利用できる |
| リプレイウィンドウ | `valid_window=1` で ±30 秒 | 計 90 秒間、同一コードが有効 |

**Phase 1 の運用上の考慮事項:**
- 管理画面は 1 ユーザー 1 サーバーの専用構成（マルチテナントなし）
- ネットワーク外からのアクセスを VPN / IP 制限で制御すれば許容範囲
- セッション固定攻撃と組み合わせると問題になりうるが、Cookie の HttpOnly / SameSite=Lax で緩和

**Phase 2 以降の対応案:** 使用済みコードを Redis に TTL=90s で記録し、重複送信を拒否する

---

### 1-3. セッション管理 / cleanup

| 項目 | 現状 | 影響 |
|---|---|---|
| 期限切れセッションの自動削除 | 未実装 | `ui_sessions` にレコードが蓄積する |
| 同一ユーザーの複数セッション制限 | 未実装 | 複数デバイスから同時ログイン可能 |
| セッション一覧表示 / 強制失効 | 未実装 | 管理 UI から操作不可 |

**Phase 1 の対処方法（手動）:**
```sql
-- 期限切れセッションの手動削除
DELETE FROM ui_sessions
WHERE expires_at < NOW()
   OR invalidated_at IS NOT NULL;
```

**Phase 2 以降の対応案:** バックグラウンドタスクで定期 cleanup / 管理 UI から強制失効機能を提供

---

### 1-4. TOTP 設定変更（再セットアップ）の安全ポリシー

| 項目 | 現状 |
|---|---|
| 再 setup の呼び出し制限 | なし。`RequirePreAuth`（Pre-2FA セッション）で呼び出し可能 |
| `totp_enabled=True` 状態での再 setup | 可能。旧シークレットを即座に上書きし、旧 Authenticator が無効になる |

**リスク:** 認証済みセッションを持つユーザーが誤って再 setup を呼んだ場合、次の verify が完了するまで 2FA ログインが不可能になる。

**Phase 2 以降の対応案:** 設定変更画面では `RequireAdmin`（2FA 完了済み）のみ再 setup を許可する。`RequirePreAuth` は初回 setup 専用にする。

---

### 1-5. 複数 Google アカウント / メールアドレス変更

| 項目 | 現状 |
|---|---|
| 複数 Google アカウントの紐付け | 未対応。メールアドレスのみで `ui_users` を照合 |
| メールアドレス変更 | DB の `ui_users.email` を手動更新が必要 |
| `sub`（Google ユーザー ID）による照合 | 未実装。`email` のみ |

**Phase 2 以降の対応案:** Google `sub` を `ui_users` に保存して照合する（メールアドレス変更に対応）

---

### 1-6. ログイン試行制限（レートリミット）

| 項目 | 現状 |
|---|---|
| ブルートフォース対策 | 未実装 |
| TOTP 失敗回数の上限 | 未実装（無制限に試行可能）|
| IP ベースのレートリミット | 未実装 |

**Phase 2 以降の対応案:** Redis で失敗回数をカウントし、N 回失敗でアカウントロック / IP ブロック

---

### 1-7. state パラメータのフロント検証前提

**バックエンドは `state` の検証を行わない（フロントに委譲している）。**

| 依存している前提 | 根拠 |
|---|---|
| フロントが `state` を sessionStorage に保存・照合する | `design_i4_auth_gaps.md §2-A-1-x` |
| 照合失敗時はフロントが `POST /auth/callback` を呼ばない | 同上 |
| SameSite=Lax Cookie により外部からの CSRF POST は Cookie が送られない | Cookie 設計 |
| PKCE の `code_verifier` により盗まれた `code` の悪用を防ぐ | OAuth 2.0 PKCE 仕様 |

**フロント実装の漏れがあった場合のリスク:** CSRF 攻撃によって攻撃者のコードでユーザーがログインさせられる（OAuth Login CSRF）。フロントでの state 検証は**必須**。

---

### 1-8. ユーザー登録 / 管理機能

| 項目 | 現状 |
|---|---|
| 管理 UI からのユーザー招待 | 未実装（手動 INSERT のみ）|
| `display_name` の自動設定 | Google `name` を取得しているが現状は保存しない |
| ロール変更 | DB 手動更新のみ |

---

## 2. セキュリティ上の前提条件

Phase 1 は以下の前提条件のもとで動作する。これらが満たされない場合は追加対策が必要。

| 前提条件 | 補足 |
|---|---|
| 1 ユーザー 1 サーバー専用構成 | マルチテナントは考慮外 |
| 管理者は少数（1〜数名）| 大規模ユーザー管理 UI は不要 |
| ネットワークレベルで外部からのアクセスを制限している | VPN / IP ホワイトリスト推奨 |
| HTTPS 必須（本番）| `COOKIE_SECURE=true` + TLS 終端 |
| `TOTP_ENCRYPTION_KEY` が安全に管理されている | Git 管理外 / 環境変数で注入 |

---

## 3. 設計上の制約（変更困難）

これらは Phase 1 の設計確定事項。変更には大規模な影響がある。

| 制約 | 内容 | 変更時の影響範囲 |
|---|---|---|
| Cookie ベースのセッション管理 | `Authorization: Bearer` トークン方式は未対応 | `auth_guard.py` の全認証ロジック |
| Cookie Path = `/api/ui-admin/` | このパス外のリクエストには Cookie が送られない | Cookie Path を変更した場合は全エンドポイントの動作確認が必要 |
| セッショントークンの SHA-256 ハッシュ保存 | `ui_sessions.session_token_hash` のみ保存（平文は非保存）| ハッシュ方式の変更は全既存セッションの無効化を伴う |
| TOTP シークレットの AES-256-GCM 暗号化 | `gv1:` フォーマット。鍵ローテーションは未対応 | 鍵変更時は全ユーザーの再 setup が必要 |
| Pre-2FA セッションの TTL = 10 分 | OAuth 後 TOTP verify まで 10 分以内に完了が必要 | 変更は `PRE_2FA_SESSION_TTL_SEC` の設定値のみ |

---

## 4. 監査ログ確認項目一覧

### 4-1. 接続確認時に見ておくべきイベント

| `event_type` | 発生タイミング・定義 | 確認ポイント |
|---|---|---|
| `LOGIN_SUCCESS` | **事前登録ユーザー照合に成功し、Pre-2FA セッションを発行した時点**（`POST /auth/callback` 内）。Google ログイン成功とは別。2FA はまだ未完了。`TWO_FA_SUCCESS` とは別イベント。 | `user_email`, `ip_address`, `created_at` を確認 |
| `LOGIN_FAILURE` | 未登録メール / `is_active=False` でログイン試行 | `user_email`（未登録の場合）と `after_json.reason`（`unregistered_email` / `inactive_account`）を確認 |
| `TWO_FA_SUCCESS` | TOTP verify 成功・セッション昇格完了時（`POST /auth/totp/verify` 内）。**この時点で初めてフル認証完了**。`LOGIN_SUCCESS` とは別イベント。 | `user_email`, `ip_address` を確認 |
| `TWO_FA_FAILURE` | TOTP verify 失敗時（不正コード）| `user_email`, `ip_address` を確認 |
| `LOGOUT` | logout エンドポイント呼び出し時 | `user_email`, `resource_id`（session_id）を確認 |
| `SESSION_EXPIRED_ACCESS` | 期限切れセッションでの API アクセス時 | `user_id`, `ip_address` を確認 |

**`LOGIN_SUCCESS` と `TWO_FA_SUCCESS` の違い（重要）:**

```
Google 認証成功
  └─ ui_users 照合成功
       └─ Pre-2FA セッション発行
            → ★ LOGIN_SUCCESS 記録（ここ）
              └─ TOTP コード入力
                   └─ verify 成功
                        → ★ TWO_FA_SUCCESS 記録（ここ）= フル認証完了
```

実接続確認時は **両方のログが順番に記録されること**を確認する。
`LOGIN_SUCCESS` のみ記録されて `TWO_FA_SUCCESS` がない場合、TOTP フローで問題が発生している。

### 4-2. 監査ログ確認 SQL

```sql
-- 全イベント種別の件数サマリー
SELECT event_type, COUNT(*) as count
FROM ui_audit_logs
GROUP BY event_type
ORDER BY count DESC;

-- 最近のログイン試行（成功・失敗含む）
SELECT event_type, user_email, ip_address, after_json, created_at
FROM ui_audit_logs
WHERE event_type IN ('LOGIN_SUCCESS', 'LOGIN_FAILURE')
ORDER BY created_at DESC
LIMIT 20;

-- TOTP 認証結果
SELECT event_type, user_email, ip_address, created_at
FROM ui_audit_logs
WHERE event_type IN ('TWO_FA_SUCCESS', 'TWO_FA_FAILURE')
ORDER BY created_at DESC
LIMIT 20;

-- セッション期限切れアクセス
SELECT user_id, ip_address, user_agent, created_at
FROM ui_audit_logs
WHERE event_type = 'SESSION_EXPIRED_ACCESS'
ORDER BY created_at DESC;
```

### 4-3. セキュリティアラートとして見るべきパターン

| パターン | 意味 | 対処 |
|---|---|---|
| 同一 IP から短時間に `TWO_FA_FAILURE` が連続 | ブルートフォース試行の可能性 | IP ブロック / アカウント一時停止（手動）|
| 未登録メールの `LOGIN_FAILURE` が多数 | 不正アクセス試行 / メールアドレス列挙 | 環境へのアクセス制限を確認 |
| `SESSION_EXPIRED_ACCESS` が多発 | セッション管理の問題またはクロックスキュー | TTL 設定とサーバー時刻を確認 |
| `LOGIN_SUCCESS` 後に `TWO_FA_FAILURE` が続く | TOTP 設定ミスまたは時刻ずれ | サーバー / クライアントの NTP を確認 |

---

## 5. Phase 2 以降の対応ロードマップ（参考）

| 優先度 | 項目 | 概要 |
|---|---|---|
| HIGH | TOTP リプレイ防止 | Redis で使用済みコードを TTL=90s で記録 |
| HIGH | セッション cleanup | バックグラウンドタスクで定期削除 |
| MEDIUM | バックアップコード | 8文字×8本、ハッシュ化保存 |
| MEDIUM | ログイン試行制限 | N 回失敗でロック / Redis カウンター |
| MEDIUM | 再 setup を RequireAdmin に制限 | 設定変更は認証済みのみ |
| LOW | Google `sub` での照合 | メールアドレス変更対応 |
| LOW | ユーザー招待 UI | 管理画面から招待リンク送信 |
