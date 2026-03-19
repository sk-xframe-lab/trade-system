#!/usr/bin/env python3
"""
立花証券 e支店 API 接続情報セットアップスクリプト

既存の .env を壊さずに Tachibana セクションのみ更新する。
パスワード類は getpass で非表示入力し、確認表示はマスクのみ。

使い方:
    python scripts/setup_tachibana_config.py
"""
import getpass
import re
import sys
from pathlib import Path

# ─── パス定数 ────────────────────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"
_GITIGNORE = _PROJECT_ROOT / ".gitignore"

# 更新対象キー（順序を保持するため list で定義）
_TACHIBANA_KEYS: list[str] = [
    "TACHIBANA_USER_ID",
    "TACHIBANA_PASSWORD",
    "TACHIBANA_SECOND_PASSWORD",
    "TACHIBANA_BASE_URL",
]


# ─── ユーティリティ ───────────────────────────────────────────────────────────

def _mask(value: str) -> str:
    """値をマスク表示する（先頭2文字のみ表示）"""
    if not value:
        return "(未設定)"
    if len(value) <= 4:
        return "****"
    return value[:2] + "****" + value[-2:]


def _abort(message: str) -> None:
    print(f"\nエラー: {message}", file=sys.stderr)
    sys.exit(1)


# ─── バリデーション ───────────────────────────────────────────────────────────

def _validate_not_empty(label: str, value: str) -> None:
    if not value.strip():
        _abort(f"{label} は必須項目です。空のまま Enter を押さないでください。")


def _validate_url(value: str) -> None:
    if not re.match(r"^https?://", value.strip()):
        _abort(
            "URL は http:// または https:// で始まる必要があります。\n"
            f"  入力値: {value!r}"
        )


# ─── .env 読み書き ────────────────────────────────────────────────────────────

def _read_env_file() -> dict[str, str]:
    """
    既存の .env を key→value dict として読み込む。
    コメント行・空行は無視する。値はクォートを除去しない（dotenv の解釈に任せる）。
    """
    if not _ENV_FILE.exists():
        return {}

    result: dict[str, str] = {}
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" in stripped:
            key, _, val = stripped.partition("=")
            result[key.strip()] = val  # 値のクォートはそのまま保持
    return result


def _write_env_file(updates: dict[str, str]) -> None:
    """
    .env に Tachibana セクションのキーを書き込む。

    既存ファイルがある場合:
      - Tachibana キーが既存行に存在すれば上書き
      - 存在しないキーはファイル末尾のセクションとして追記
    ファイルが存在しない場合: 新規作成
    """
    if not _ENV_FILE.exists():
        lines: list[str] = [
            "# ─── 立花証券 e支店 API ────────────────────────────────────────────────────",
            "# このファイルは .gitignore で除外されています。Git に含めないでください。",
            "",
        ]
        for key in _TACHIBANA_KEYS:
            lines.append(f"{key}={updates[key]}")
        _ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    # 既存ファイルを行単位で処理
    existing_lines = _ENV_FILE.read_text(encoding="utf-8").splitlines()
    new_lines: list[str] = []
    updated_keys: set[str] = set()

    for line in existing_lines:
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            key = stripped.partition("=")[0].strip()
            if key in _TACHIBANA_KEYS:
                new_lines.append(f"{key}={updates[key]}")
                updated_keys.add(key)
                continue
        new_lines.append(line)

    # 既存ファイルに存在しなかったキーを末尾セクションとして追加
    missing_keys = [k for k in _TACHIBANA_KEYS if k not in updated_keys]
    if missing_keys:
        if new_lines and new_lines[-1].strip():
            new_lines.append("")
        new_lines.append(
            "# ─── 立花証券 e支店 API ────────────────────────────────────────────────────"
        )
        for key in missing_keys:
            new_lines.append(f"{key}={updates[key]}")

    _ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


# ─── .gitignore ガード ────────────────────────────────────────────────────────

def _ensure_gitignore() -> None:
    """
    .gitignore に .env が含まれていない場合は警告を表示する。
    自動書き込みはしない（既存行の順序・コメントを壊す恐れがあるため）。
    """
    if not _GITIGNORE.exists():
        print(
            "\n⚠️  .gitignore が見つかりません。"
            " .env を Git に含めないよう手動で追加してください。",
            file=sys.stderr,
        )
        return

    content = _GITIGNORE.read_text(encoding="utf-8")
    patterns = [line.strip() for line in content.splitlines()]
    if ".env" not in patterns and "*.env" not in patterns:
        print(
            "\n⚠️  .gitignore に .env が含まれていません。"
            " 以下の行を .gitignore に追加してください:",
            file=sys.stderr,
        )
        print("      .env", file=sys.stderr)


# ─── メイン ──────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 60)
    print("立花証券 e支店 API 接続情報セットアップ")
    print("=" * 60)
    print(f"保存先: {_ENV_FILE}")
    print()

    # 既存設定の表示
    existing = _read_env_file()
    has_existing = any(existing.get(k, "").strip() for k in _TACHIBANA_KEYS)
    if has_existing:
        print("既存の設定が見つかりました（上書きします）:")
        for key in _TACHIBANA_KEYS:
            val = existing.get(key, "")
            if "PASSWORD" in key:
                display = "****" if val else "(未設定)"
            else:
                display = _mask(val) if val else "(未設定)"
            print(f"  {key}: {display}")
        print()

    # ─── 入力プロンプト ──────────────────────────────────────────────────────
    print("接続情報を入力してください。パスワード項目は非表示入力です。")
    print()

    user_id = input("sUserId (ユーザーID): ").strip()
    _validate_not_empty("sUserId", user_id)

    password = getpass.getpass("sPassword (パスワード): ").strip()
    _validate_not_empty("sPassword", password)

    second_password = getpass.getpass("sSecondPassword (第二パスワード): ").strip()
    _validate_not_empty("sSecondPassword", second_password)

    base_url = input(
        "ベース URL（/auth/ より上のパス）\n"
        "  認証 I/F 仕様: GET {ベースURL}/auth/?{JSON文字列}\n"
        "  例: https://demo-kabuka.e-shiten.jp/e_api_v4r8/\n"
        "  ※ /auth/ はスクリプトが自動補完します。入力不要です。\n"
        "  URL: "
    ).strip()
    _validate_not_empty("エントリーポイントURL", base_url)
    _validate_url(base_url)

    # ─── 確認表示（マスク済み） ──────────────────────────────────────────────
    print()
    print("─" * 40)
    print("以下の内容で保存します:")
    print(f"  TACHIBANA_USER_ID        : {_mask(user_id)}")
    print(f"  TACHIBANA_PASSWORD       : ****")
    print(f"  TACHIBANA_SECOND_PASSWORD: ****")
    print(f"  TACHIBANA_BASE_URL       : {base_url}")
    print("─" * 40)
    print()

    confirm = input("保存しますか？ [y/N]: ").strip().lower()
    if confirm != "y":
        print("キャンセルしました。設定は保存されていません。")
        sys.exit(0)

    # ─── 書き込み ────────────────────────────────────────────────────────────
    updates = dict(existing)
    updates["TACHIBANA_USER_ID"]        = user_id
    updates["TACHIBANA_PASSWORD"]       = password
    updates["TACHIBANA_SECOND_PASSWORD"] = second_password
    updates["TACHIBANA_BASE_URL"]       = base_url

    _write_env_file(updates)
    _ensure_gitignore()

    print()
    print(f"✅ 保存完了: {_ENV_FILE}")
    print()
    print("次のステップ:")
    print("  python scripts/check_tachibana_connection.py")
    print()
    print("注意: .env は .gitignore で除外されています。Git に含めないでください。")
    print()


if __name__ == "__main__":
    main()
