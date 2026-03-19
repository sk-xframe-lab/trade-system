# I-5 セッション設計書

対象: UiSession の expires_at / idle timeout / セッション更新タイミング
作成日: 2026-03-18
ステータス: **設計確定（2026-03-18 ユーザー承認済み）**

## 確定内容サマリー（2026-03-18）

| 項目 | 確定値 |
|---|---|
| タイムアウト方式 | Absolute Timeout（毎リクエスト更新なし）|
| Pre-2FA セッション TTL | 600 秒（10 分）|
| 完全認証後 TTL | 28800 秒（8 時間）|
| TOTP 認証後 | 同一セッション更新（新規発行なし）|
| 期限切れアクセス | `SESSION_EXPIRED_ACCESS` として監査ログ記録必須 |
| 期限切れレコード cleanup | Phase 1 では未実装で可 |

config.py に追加する設定値:
```python
SESSION_TTL_SEC: int = 28800          # 8 時間
PRE_2FA_SESSION_TTL_SEC: int = 600    # 10 分
```

---

---

## 1. セッション有効期限の種類

### 1-A: Absolute Timeout（絶対有効期限）— **推奨**

```
発行時刻
  │
  ├── is_2fa_completed=False（OAuth 後・TOTP 前）
  │       TTL: 短い（例: 10 分）
  │
  └── is_2fa_completed=True（完全認証済み）
          TTL: 長い（例: 8 時間）
          │
          アクセスがあっても expires_at は延長しない
          │
          expires_at 超過 → 401 → 再ログイン
```

**メリット:**
- 実装がシンプル（`expires_at` を 1 回設定するだけ）
- サーバー側の更新処理が不要
- 長時間放置されたセッションが自動失効する

**デメリット:**
- 作業中にタイムアウトが発生する（ユーザー体験が悪い場合あり）
- 1ユーザー1サーバーの管理システムとしてはトレードオフが受け入れやすい

### 1-B: Idle Timeout（アイドル有効期限）

```
最終アクセス時刻
  │
  ├── アクセスごとに expires_at を延長（Sliding Window）
  └── 最終アクセスから N 分経過 → タイムアウト
```

**メリット:** 使用中はタイムアウトしない
**デメリット:** 毎リクエストで DB 更新が発生する（パフォーマンス影響）

### 採用案

**1-A（Absolute Timeout）を推奨する。**

理由:
- 本システムは1ユーザー1サーバーの管理画面。同時接続セッション数は極めて少ない
- 毎リクエストの DB 更新（1-B）は不要なオーバーヘッドになる
- 管理操作は短い集中作業が多く、長時間放置はセキュリティリスク

**ユーザー確認事項:**
> Absolute Timeout（8時間後に強制ログアウト）で問題ないか確認をお願いします。
> 作業中のタイムアウトが頻発するようであれば Idle Timeout に変更可能です。

---

## 2. タイムアウト時間の設計案

### 2-A: 2段階セッション（推奨）

```
OAuth 認証後（is_2fa_completed=False）
  expires_at = now + PRE_2FA_SESSION_TTL_SEC（推奨: 600 秒 = 10 分）
  目的: TOTP 入力のための短命セッション
  このセッションでは GET /auth/me と POST /auth/totp/verify 以外を拒否する

TOTP 認証後（is_2fa_completed=True）
  expires_at = now + SESSION_TTL_SEC（推奨: 28800 秒 = 8 時間）
  目的: 通常の管理操作セッション
  既存セッションの is_2fa_completed=True にする か、新しいセッションを発行するか選択
```

### 2-B: 設定値（config.py に追加する項目）

```python
# セッション設定（I-5 確定後に追加）
SESSION_TTL_SEC: int = 28800           # 8 時間（完全認証後）
PRE_2FA_SESSION_TTL_SEC: int = 600     # 10 分（OAuth 後・TOTP 前）
```

---

## 3. セッション更新タイミング

### 3-A: 更新しないケース（Absolute Timeout 採用時）

更新操作は発生しない。発行時の `expires_at` が最終期限。

### 3-B: 更新が発生するケース

以下の操作時は既存セッションを延長または再発行する:

| 操作 | 処理 |
|---|---|
| TOTP 認証成功 | `session.expires_at = now + SESSION_TTL_SEC` に更新（または新規セッション発行）|
| 明示的なセッション延長 API（要否未確定）| `session.expires_at` を現在時刻から再計算 |

**TOTP 認証後のセッション処理の選択肢:**

| 方式 | 内容 | メリット | デメリット |
|---|---|---|---|
| (a) 既存セッション更新 | `is_2fa_completed=True` にして `expires_at` を延長 | トークン変更なし | Pre-2FA トークンが長命になる（セキュリティ的に弱い）|
| (b) 新規セッション発行 | Pre-2FA セッションを `invalidated_at` で無効化。新セッションを INSERT して新トークンを返却 | 認証段階ごとにトークンを分離できる | フロントが 2 つのトークンを管理する必要あり |

**推奨:** (a) 既存セッション更新。1ユーザー1サーバーで複雑さが不要なため。

---

## 4. UiSession.is_valid プロパティの現在の実装

```python
# models/ui_session.py（現在の実装）
@property
def is_valid(self) -> bool:
    now = datetime.now(timezone.utc)
    return (
        self.invalidated_at is None
        and self.is_2fa_completed          # ← TOTP 完了必須
        and self.expires_at > now
    )
```

**この実装は設計案に沿っている。**
Pre-2FA セッション（`is_2fa_completed=False`）は `is_valid=False` になるため、
`get_current_admin_user()` が 401 を返す。
TOTP 認証専用エンドポイント（`POST /auth/totp/verify`）では `is_valid` を使わず
セッションを直接 SELECT することで Pre-2FA セッションを扱う。

---

## 5. セッション期限切れの検出とログ記録

**現状の問題:**
`get_current_admin_user()` でセッション期限切れを検出したとき、
`SESSION_INVALIDATED` の監査ログが記録されない（`component_design.md §7.1` 未実装リスト参照）。

**設計案:**
```python
# auth_guard.py get_current_admin_user() の期限切れ検出部分に追加する（実装時）
if not session.is_valid:
    if session.expires_at < datetime.now(timezone.utc):
        # 期限切れ → SESSION_INVALIDATED を記録（任意）
        # ただし: audit_svc が admin_db セッションを使うため、
        # auth_guard が admin_db セッションを持っていることを確認すること
        ...
```

**TODO(I-5 実装時):** 期限切れ検出時のログ記録要否を確認すること。
記録する場合は `get_current_admin_user()` の引数にセッションを追加するか、
別の仕組みが必要。

---

## 6. 期限切れセッションのクリーンアップ

**現在:** `ui_sessions` テーブルに期限切れレコードが蓄積していく（クリーンアップなし）

**設計案（優先度低）:**
- 定期バッチ: `DELETE FROM ui_sessions WHERE expires_at < NOW() - INTERVAL '30 days'`
- タイミング: アプリ起動時 / 1日1回 / 管理者手動
- Phase 1 では放置でもテーブルサイズは無視できる（1ユーザー = セッション数少）

---

## 7. セッショントークンの形式

**決定事項（実装時に使用）:**
- 形式: UUID v4 (`str(uuid.uuid4())`) — 128 ビットランダム
- DB 保存: `hash_session_token(raw_token)` = SHA-256（`auth_guard.py` 実装済み）
- ヘッダー: `Authorization: Bearer <raw_token>`

**セキュリティ上の注意:**
- DB に保存するのはハッシュのみ（平文トークンは保存しない）→ 実装済み
- トークンは HTTP 通信では平文で送信される → 本番環境では HTTPS 必須（管理画面仕様書の前提）
- `session_token_hash` カラムに UNIQUE 制約あり → 衝突確率は実用上無視できる

---

## 8. 確定済み設計決定事項（2026-03-18）

| 番号 | 質問 | 確定値 |
|---|---|---|
| Q1 | Absolute Timeout を採用するか？ | ✅ 採用（8時間後強制ログアウト）|
| Q2 | Pre-2FA セッションの TTL | ✅ 600 秒（10 分）|
| Q3 | 完全認証後のセッション TTL | ✅ 28800 秒（8 時間）|
| Q4 | TOTP 認証後のセッション処理 | ✅ 同一セッション更新（新規発行なし）|
| Q5 | 期限切れアクセスの監査ログ記録 | ✅ 記録必須（`SESSION_EXPIRED_ACCESS`）|
| Q6 | 期限切れレコードの cleanup | ✅ Phase 1 は未実装で可 |

---

## 9. 実装ブロッカー解消後の手順

1. 上記 Q1〜Q6 について確認・決定
2. `trade_app/config.py` に `SESSION_TTL_SEC` / `PRE_2FA_SESSION_TTL_SEC` を追加
3. `POST /auth/callback` でセッション発行ロジックを実装（I-4 ブロッカー解消後）
4. `POST /auth/totp/verify` でセッション更新ロジックを実装（I-3 + Q4 確定後）
5. 期限切れ監査ログ記録を `auth_guard.py` に追加（Q5 確定後）
