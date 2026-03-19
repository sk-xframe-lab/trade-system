"""
TotpEncryptor テスト

設計書: docs/admin/design_i3_encryption.md §8 テスト方針

【検証内容】
- 暗号化 → 復号 往復テスト（元の値が返ること）
- IV の一意性（同じ平文を 2 回暗号化した結果が異なること）
- 認証タグ改ざん検知（1 バイト変更で DecryptionError）
- 鍵不一致検知（異なる鍵で復号すると DecryptionError）
- フォーマット不正（gv1: なし / Base64 不正 → DecryptionError）
- 鍵長不正（31 / 33 バイトで ConfigurationError）
- 未対応バージョン（gv2: → UnsupportedVersionError）
- from_settings ファクトリ（TOTP_ENCRYPTION_KEY 未設定で ConfigurationError）
"""
import base64
import os

import pytest

from trade_app.admin.services.encryption import (
    ConfigurationError,
    DecryptionError,
    TotpEncryptor,
    UnsupportedVersionError,
)

# テスト用固定鍵（32 バイト = 256 ビット）
_TEST_KEY_BYTES = b"test_key_32bytes_exactly_padding"  # 32 bytes
assert len(_TEST_KEY_BYTES) == 32
_TEST_KEY_B64 = base64.b64encode(_TEST_KEY_BYTES).decode()


def _make_encryptor() -> TotpEncryptor:
    return TotpEncryptor(_TEST_KEY_B64)


# ─── TestEncryptDecryptRoundtrip ──────────────────────────────────────────────


class TestEncryptDecryptRoundtrip:
    def test_basic_roundtrip(self):
        """暗号化→復号で元の値が返ること"""
        enc = _make_encryptor()
        secret = "JBSWY3DPEHPK3PXP"
        stored = enc.encrypt(secret)
        assert enc.decrypt(stored) == secret

    def test_roundtrip_with_various_secrets(self):
        """様々な形式の TOTP シークレットで往復テスト"""
        enc = _make_encryptor()
        secrets = [
            "JBSWY3DPEHPK3PXP",           # 16 chars（標準）
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAA",  # 28 chars（長め）
            "A" * 32,                        # 32 chars
        ]
        for secret in secrets:
            assert enc.decrypt(enc.encrypt(secret)) == secret

    def test_stored_format_starts_with_gv1(self):
        """保存フォーマットが 'gv1:' で始まること"""
        enc = _make_encryptor()
        stored = enc.encrypt("TESTSECRET")
        assert stored.startswith("gv1:")


# ─── TestIvUniqueness ─────────────────────────────────────────────────────────


class TestIvUniqueness:
    def test_same_plaintext_produces_different_ciphertext(self):
        """同じ平文を 2 回暗号化した結果が異なること（IV の一意性）"""
        enc = _make_encryptor()
        secret = "JBSWY3DPEHPK3PXP"
        stored1 = enc.encrypt(secret)
        stored2 = enc.encrypt(secret)
        # 全体の文字列が異なること（IV が異なるため）
        assert stored1 != stored2

    def test_both_results_decrypt_to_same_plaintext(self):
        """異なる暗号文でも同じ平文に復号できること"""
        enc = _make_encryptor()
        secret = "JBSWY3DPEHPK3PXP"
        stored1 = enc.encrypt(secret)
        stored2 = enc.encrypt(secret)
        assert enc.decrypt(stored1) == secret
        assert enc.decrypt(stored2) == secret


# ─── TestTamperDetection ─────────────────────────────────────────────────────


class TestTamperDetection:
    def test_tamper_one_byte_raises_decryption_error(self):
        """暗号文の 1 バイトを変えると DecryptionError が発生すること"""
        enc = _make_encryptor()
        stored = enc.encrypt("JBSWY3DPEHPK3PXP")

        # "gv1:" の後の Base64 文字列を取り出して 1 バイト改ざん
        prefix, _, encoded = stored.partition(":")
        data = bytearray(base64.urlsafe_b64decode(encoded))
        # 最後のバイト（認証タグの末尾）を反転
        data[-1] ^= 0xFF
        tampered = f"{prefix}:{base64.urlsafe_b64encode(bytes(data)).decode()}"

        with pytest.raises(DecryptionError):
            enc.decrypt(tampered)

    def test_tamper_ciphertext_raises_decryption_error(self):
        """暗号文本体（タグより前）を改ざんしても DecryptionError が発生すること"""
        enc = _make_encryptor()
        stored = enc.encrypt("JBSWY3DPEHPK3PXP")

        prefix, _, encoded = stored.partition(":")
        data = bytearray(base64.urlsafe_b64decode(encoded))
        # IV(12) の直後のバイトを改ざん
        data[12] ^= 0x01
        tampered = f"{prefix}:{base64.urlsafe_b64encode(bytes(data)).decode()}"

        with pytest.raises(DecryptionError):
            enc.decrypt(tampered)


# ─── TestWrongKey ────────────────────────────────────────────────────────────


class TestWrongKey:
    def test_wrong_key_raises_decryption_error(self):
        """異なる鍵で復号すると DecryptionError が発生すること"""
        enc = _make_encryptor()
        stored = enc.encrypt("JBSWY3DPEHPK3PXP")

        other_key = base64.b64encode(b"other_key_32bytes_padding_abcdef").decode()
        other_enc = TotpEncryptor(other_key)

        with pytest.raises(DecryptionError):
            other_enc.decrypt(stored)


# ─── TestInvalidFormat ────────────────────────────────────────────────────────


class TestInvalidFormat:
    def test_no_prefix_raises_decryption_error(self):
        """バージョンプレフィックスなし → DecryptionError"""
        enc = _make_encryptor()
        with pytest.raises(DecryptionError):
            enc.decrypt("invaliddatawithoutseparator")

    def test_invalid_base64_raises_decryption_error(self):
        """Base64 不正 → DecryptionError"""
        enc = _make_encryptor()
        with pytest.raises(DecryptionError):
            enc.decrypt("gv1:not_valid_base64!!!")

    def test_too_short_data_raises_decryption_error(self):
        """データが短すぎる（IV + タグの最小長 28 バイト未満）→ DecryptionError"""
        enc = _make_encryptor()
        short_data = base64.urlsafe_b64encode(b"short").decode()
        with pytest.raises(DecryptionError):
            enc.decrypt(f"gv1:{short_data}")

    def test_empty_string_raises_decryption_error(self):
        """空文字列 → DecryptionError"""
        enc = _make_encryptor()
        with pytest.raises(DecryptionError):
            enc.decrypt("")


# ─── TestKeyLengthValidation ─────────────────────────────────────────────────


class TestKeyLengthValidation:
    def test_31_byte_key_raises_configuration_error(self):
        """31 バイトの鍵 → ConfigurationError"""
        key_31 = base64.b64encode(os.urandom(31)).decode()
        with pytest.raises(ConfigurationError):
            TotpEncryptor(key_31)

    def test_33_byte_key_raises_configuration_error(self):
        """33 バイトの鍵 → ConfigurationError"""
        key_33 = base64.b64encode(os.urandom(33)).decode()
        with pytest.raises(ConfigurationError):
            TotpEncryptor(key_33)

    def test_invalid_base64_key_raises_configuration_error(self):
        """不正 Base64 鍵 → ConfigurationError"""
        with pytest.raises(ConfigurationError):
            TotpEncryptor("not_valid_base64!!!")


# ─── TestUnsupportedVersion ───────────────────────────────────────────────────


class TestUnsupportedVersion:
    def test_gv2_prefix_raises_unsupported_version_error(self):
        """gv2: プレフィックス → UnsupportedVersionError"""
        enc = _make_encryptor()
        # 有効なデータだが gv1 ではなく gv2 プレフィックス
        stored = enc.encrypt("TESTSECRET")
        gv2_stored = stored.replace("gv1:", "gv2:", 1)

        with pytest.raises(UnsupportedVersionError):
            enc.decrypt(gv2_stored)

    def test_unknown_version_not_decrypt_error(self):
        """未知バージョンは DecryptionError ではなく UnsupportedVersionError"""
        enc = _make_encryptor()
        with pytest.raises(UnsupportedVersionError):
            enc.decrypt("gv99:somedata")


# ─── TestFromSettings ────────────────────────────────────────────────────────


class TestFromSettings:
    def test_from_settings_success(self):
        """TOTP_ENCRYPTION_KEY が設定済みなら TotpEncryptor が生成される"""
        class FakeSettings:
            TOTP_ENCRYPTION_KEY = _TEST_KEY_B64

        enc = TotpEncryptor.from_settings(FakeSettings())
        # 動作確認
        stored = enc.encrypt("TESTSECRET")
        assert enc.decrypt(stored) == "TESTSECRET"

    def test_from_settings_empty_key_raises_configuration_error(self):
        """TOTP_ENCRYPTION_KEY が空 → ConfigurationError"""
        class FakeSettings:
            TOTP_ENCRYPTION_KEY = ""

        with pytest.raises(ConfigurationError):
            TotpEncryptor.from_settings(FakeSettings())
