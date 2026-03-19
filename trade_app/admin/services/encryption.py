"""
TOTP シークレット暗号化サービス

設計書: docs/admin/design_i3_encryption.md（I-3 確定済み）

【暗号化方式】
- AES-256-GCM（AEAD: 暗号化 + 完全性検証）
- IV: 暗号化ごとに os.urandom(12) で新規生成
- 保存フォーマット: "gv1:<base64url(iv || ciphertext || tag)>"

【鍵管理】
- TOTP_ENCRYPTION_KEY 環境変数: Base64 エンコードされた 32 バイト
- 生成: python3 -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"

【セキュリティ注意事項】
- ログに平文 TOTP シークレット・暗号化鍵の値を絶対に含めないこと
- エラーメッセージにシークレット値を含めないこと
"""
import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# 保存フォーマットバージョン（将来のアルゴリズム変更に備えるプレフィックス）
_VERSION = "gv1"
_IV_LENGTH = 12    # 96 ビット（GCM 推奨値）
_TAG_LENGTH = 16   # 128 ビット（GCM デフォルト）


# ─── カスタム例外 ─────────────────────────────────────────────────────────────


class ConfigurationError(Exception):
    """TOTP_ENCRYPTION_KEY が未設定または不正な場合"""


class EncryptionError(Exception):
    """暗号化処理の失敗（通常は発生しない）"""


class DecryptionError(Exception):
    """復号失敗（認証タグ不一致 / フォーマット不正 / 鍵不一致）"""


class UnsupportedVersionError(Exception):
    """未対応のバージョンプレフィックス（gv1 以外）"""


# ─── TotpEncryptor ────────────────────────────────────────────────────────────


class TotpEncryptor:
    """
    TOTP シークレットの暗号化・復号。AES-256-GCM を使用する。

    使用例:
        encryptor = TotpEncryptor.from_settings(settings)
        stored = encryptor.encrypt("JBSWY3DPEHPK3PXP")
        plain  = encryptor.decrypt(stored)
    """

    def __init__(self, key_b64: str) -> None:
        """
        Args:
            key_b64: Base64 エンコードされた 32 バイト（256 ビット）の暗号化鍵

        Raises:
            ConfigurationError: デコード失敗または鍵長が 32 バイトでない場合
        """
        try:
            key_bytes = base64.b64decode(key_b64)
        except Exception as exc:
            raise ConfigurationError(
                "TOTP_ENCRYPTION_KEY の Base64 デコードに失敗しました"
            ) from exc

        if len(key_bytes) != 32:
            raise ConfigurationError(
                f"TOTP_ENCRYPTION_KEY は 32 バイト（256 ビット）必要です。"
                f"現在: {len(key_bytes)} バイト"
            )

        self._key = key_bytes

    @classmethod
    def from_settings(cls, settings) -> "TotpEncryptor":
        """
        設定オブジェクトから TotpEncryptor を生成するファクトリ。

        Args:
            settings: TOTP_ENCRYPTION_KEY 属性を持つ設定オブジェクト

        Raises:
            ConfigurationError: TOTP_ENCRYPTION_KEY が空の場合
        """
        if not settings.TOTP_ENCRYPTION_KEY:
            raise ConfigurationError(
                "TOTP_ENCRYPTION_KEY が設定されていません。"
                ".env に TOTP_ENCRYPTION_KEY を設定してください。"
            )
        return cls(settings.TOTP_ENCRYPTION_KEY)

    def encrypt(self, plaintext: str) -> str:
        """
        TOTP シークレット（平文）を暗号化して保存フォーマット文字列を返す。

        呼び出しごとにランダム IV を生成するため、同じ平文でも異なる暗号文になる。

        Args:
            plaintext: TOTP シークレット（例: "JBSWY3DPEHPK3PXP"）

        Returns:
            "gv1:<base64url(iv || ciphertext || tag)>"

        Raises:
            EncryptionError: 暗号化処理の失敗時
        """
        try:
            iv = os.urandom(_IV_LENGTH)
            aesgcm = AESGCM(self._key)
            # AESGCM.encrypt は ciphertext + tag (16 バイト) を連結して返す
            ciphertext_and_tag = aesgcm.encrypt(iv, plaintext.encode("utf-8"), None)
            combined = iv + ciphertext_and_tag  # 12 + len(plaintext) + 16 バイト
            encoded = base64.urlsafe_b64encode(combined).decode("ascii")
            return f"{_VERSION}:{encoded}"
        except Exception as exc:
            raise EncryptionError("TOTP シークレットの暗号化に失敗しました") from exc

    def decrypt(self, stored: str) -> str:
        """
        保存フォーマット文字列を復号して平文（TOTP シークレット）を返す。

        Args:
            stored: "gv1:<base64url(iv || ciphertext || tag)>"

        Returns:
            平文 TOTP シークレット

        Raises:
            DecryptionError: フォーマット不正・認証タグ不一致・鍵不正
            UnsupportedVersionError: gv1 以外のバージョンプレフィックス
        """
        if not isinstance(stored, str) or ":" not in stored:
            raise DecryptionError(
                "不正な保存フォーマット（バージョンプレフィックスが見つかりません）"
            )

        version, _, encoded = stored.partition(":")

        if version != _VERSION:
            raise UnsupportedVersionError(
                f"未対応のバージョン: '{version}'。対応バージョン: '{_VERSION}'"
            )

        try:
            combined = base64.urlsafe_b64decode(encoded)
        except Exception as exc:
            raise DecryptionError("Base64 デコードに失敗しました") from exc

        min_length = _IV_LENGTH + _TAG_LENGTH
        if len(combined) < min_length:
            raise DecryptionError(
                f"データが短すぎます（最小 {min_length} バイト必要）"
            )

        iv = combined[:_IV_LENGTH]
        ciphertext_and_tag = combined[_IV_LENGTH:]

        try:
            aesgcm = AESGCM(self._key)
            plaintext_bytes = aesgcm.decrypt(iv, ciphertext_and_tag, None)
            return plaintext_bytes.decode("utf-8")
        except Exception as exc:
            # 詳細（鍵・平文の値）をログに含めない
            raise DecryptionError(
                "TOTP シークレットの復号に失敗しました（認証タグ不一致または鍵不正）"
            ) from exc
