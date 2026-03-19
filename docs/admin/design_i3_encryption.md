# I-3 暗号化方式設計書

対象: `totp_secret_encrypted` カラムへの TOTP シークレット保存
作成日: 2026-03-18
ステータス: **設計確定（2026-03-18 ユーザー承認済み / 実装待ち）**

---

## 1. 採用アルゴリズム: AES-256-GCM

| 項目 | 値 |
|---|---|
| アルゴリズム | AES-256-GCM |
| 鍵長 | 256 ビット（32 バイト）|
| IV（Nonce）長 | 96 ビット（12 バイト）— GCM 推奨値 |
| 認証タグ長 | 128 ビット（16 バイト）— GCM デフォルト |
| IV 生成 | 暗号化ごとに `os.urandom(12)` で新規生成 |
| ライブラリ | `cryptography` (PyCA) — requirements.txt に追加要 |

### AES-256-GCM を選択した理由

- **AEAD（認証付き暗号）**: 暗号化と完全性検証を同時に行う。改ざん検知が組み込みで完結する
- **業界標準**: NIST 推奨。TLS 1.3 でも使用されている
- **副作用なし**: 暗号文長 = 平文長（TOTP シークレットは 20〜32 バイト程度で固定長に近い）
- **実装容易性**: PyCA `cryptography` ライブラリで数行で実装可能

---

## 2. 保存フォーマット

```
<version_prefix>:<base64url(iv || ciphertext || tag)>
```

### 具体例

```
gv1:dGVzdGl2MTIzNDU2dGVzdGNpcGhlcnRleHR0ZXN0dGFnMTI=
```

| フィールド | 長さ | 内容 |
|---|---|---|
| version_prefix | 固定: `gv1` | 将来のアルゴリズム変更に対応するバージョン識別子 |
| `:` | 1 バイト | 区切り文字 |
| iv | 12 バイト | ランダム Nonce（暗号化ごとに新規生成）|
| ciphertext | 平文と同長 | TOTP シークレット（通常 20〜32 バイト）|
| tag | 16 バイト | GCM 認証タグ |

全体を `base64url`（パディングあり可）でエンコードして結合する。

### バージョンプレフィックスの意義

- 将来 AES-256-GCM → ChaCha20-Poly1305 等に移行する場合、`gv1:` を `gv2:` に変えて
  `decrypt()` 内でフォーマットを分岐することで後方互換性を維持できる
- 現在は `gv1` のみサポート

---

## 3. 鍵の管理

### 3.1 設定方法

```
環境変数: TOTP_ENCRYPTION_KEY
値の形式: Base64 エンコードされた 32 バイトのバイト列
```

**生成コマンド（初期設定）:**
```bash
python3 -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"
```

**`.env.example` への追加（コードはまだ書かない）:**
```
# TOTP シークレット暗号化鍵（AES-256-GCM / 32 バイト）
# 生成: python3 -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"
TOTP_ENCRYPTION_KEY=
```

### 3.2 鍵ローテーション方針

- `gv1:` プレフィックスにより、古い鍵で暗号化されたデータを識別できる
- ローテーション手順:
  1. 新しい鍵で `TOTP_ENCRYPTION_KEY` を更新
  2. 既存レコードを旧鍵で復号 → 新鍵で再暗号化するバッチを実行（Phase 2 以降）
- Phase 1 では鍵ローテーションは考慮しない（ユーザー数が少ないため手動対応可能）

### 3.3 鍵の機密性

- `.env` ファイルに記載し、`.gitignore` で管理対象から除外済み
- 本番環境では環境変数として注入する（Docker Compose の `environment:` / Secrets 機能）
- ログに鍵の値・平文 TOTP シークレットを絶対に出力しない

---

## 4. インターフェース設計

```python
# 配置先: trade_app/admin/services/encryption.py
# コードはまだ書かない。このインターフェース仕様を実装の際に使用すること。

class TotpEncryptor:
    """
    TOTP シークレットの暗号化・復号。
    AES-256-GCM を使用する。鍵は環境変数 TOTP_ENCRYPTION_KEY から読み込む。
    """

    def __init__(self, key_b64: str): ...
    # key_b64: Base64 エンコードされた 32 バイトの鍵

    @classmethod
    def from_settings(cls, settings) -> "TotpEncryptor": ...
    # settings.TOTP_ENCRYPTION_KEY から生成するファクトリ

    def encrypt(self, plaintext: str) -> str:
        """
        平文（TOTP シークレット）を暗号化して保存フォーマット文字列を返す。
        呼び出しごとにランダム IV を生成する（同じ平文でも異なる暗号文になる）。

        Returns: "gv1:<base64url(iv || ciphertext || tag)>"
        Raises:  EncryptionError — 暗号化失敗時（鍵不正など）
        """
        ...

    def decrypt(self, stored: str) -> str:
        """
        保存フォーマット文字列を復号して平文（TOTP シークレット）を返す。

        Args:   stored: "gv1:<base64url(iv || ciphertext || tag)>"
        Returns: 平文 TOTP シークレット
        Raises:  DecryptionError — フォーマット不正・認証タグ不一致・鍵不正
                 UnsupportedVersionError — gv1 以外のバージョンプレフィックス
        """
        ...
```

---

## 5. エラーハンドリング方針

| エラー種別 | 例外クラス | 説明 | 呼び出し元の対応 |
|---|---|---|---|
| 鍵未設定 | `ConfigurationError` | `TOTP_ENCRYPTION_KEY` が空 / 長さ不正 | アプリ起動時に検出してシャットダウン |
| 暗号化失敗 | `EncryptionError` | 通常は発生しないが念のため | 500 エラーを返す（詳細はログのみ）|
| 復号失敗 | `DecryptionError` | 認証タグ不一致（改ざん検知）/ 鍵不一致 | 500 エラーを返す（詳細はログのみ）|
| フォーマット不正 | `DecryptionError` | `gv1:` プレフィックスなし / Base64 不正 | 500 エラーを返す（詳細はログのみ）|
| 未対応バージョン | `UnsupportedVersionError` | `gv2:` 等の未知バージョン | 500 エラー + 要対応ログ |

**重要**: 全エラーで例外の `message` にはシークレット値を含めない。
ログにも平文・暗号文の実値は出力しない（"TOTP decryption failed for user={user_id}" のみ記録）。

---

## 6. 使用箇所

| ファイル | 操作 | タイミング |
|---|---|---|
| `routes/auth.py` `POST /auth/totp/setup` | `encrypt(totp_secret)` → `ui_users.totp_secret_encrypted` に保存 | TOTP セットアップ時 |
| `routes/auth.py` `POST /auth/totp/verify` | `decrypt(ui_users.totp_secret_encrypted)` → pyotp で検証 | TOTP 認証時（2FA 完了マーク）|

---

## 7. 依存ライブラリ

### 追加が必要な requirements.txt エントリ

```
# ─── 暗号化 ────────────────────────────────────────────────────────────────
cryptography>=42.0.0     # AES-256-GCM（TOTP シークレット暗号化）
pyotp>=2.9.0             # TOTP 生成・検証
```

- `cryptography` は `pyca/cryptography` パッケージ。PyPI 公式。多くの本番システムで採用実績あり。
- `pyotp` は Google Authenticator 互換の TOTP/HOTP ライブラリ。
- **追加タイミング**: I-3 実装時（コードを書く前に requirements.txt を更新して Docker ビルド）

---

## 8. テスト方針

実装時に以下のテストケースを `tests/admin/test_encryption.py` として追加すること:

| テストケース | 内容 |
|---|---|
| 暗号化 → 復号 往復テスト | `encrypt(s)` → `decrypt(...)` で元の値が返る |
| IV の一意性 | 同じ平文を 2 回暗号化した結果が異なること |
| 認証タグ改ざん検知 | 暗号文の 1 バイトを変えると `DecryptionError` が発生すること |
| 鍵不一致検知 | 異なる鍵で復号すると `DecryptionError` が発生すること |
| フォーマット不正 | `gv1:` なし / Base64 不正で `DecryptionError` |
| 鍵長不正 | 31 バイト / 33 バイトの鍵で `ConfigurationError` |

---

## 9. 実装ブロッカー解消後の手順

1. `requirements.txt` に `cryptography>=42.0.0` と `pyotp>=2.9.0` を追加
2. `.env.example` に `TOTP_ENCRYPTION_KEY=` を追加
3. `trade_app/config.py` に `TOTP_ENCRYPTION_KEY: str = ""` を追加
4. `trade_app/admin/services/encryption.py` を本設計書のインターフェースで実装
5. `routes/auth.py` の `POST /totp/setup` と `POST /totp/verify` を実装
6. `tests/admin/test_encryption.py` を追加
7. Docker ビルド + テスト全件確認
