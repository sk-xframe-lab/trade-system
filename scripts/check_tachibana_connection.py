#!/usr/bin/env python3
"""
立花証券 e支店 API 接続確認スクリプト

.env に設定された認証情報で CLMAuthLoginRequest を送信し、
p_errno / sResultCode / 仮想 URL の取得状況を表示する。

【認証 I/F 仕様】
    送信形式: GET {認証URL}/auth/?{JSON文字列}
    例: https://demo-kabuka.e-shiten.jp/e_api_v4r8/auth/
          ?{"p_no":"1","p_sd_date":"2026.03.18-09:00:00.000",
            "sCLMID":"CLMAuthLoginRequest","sUserId":"xxxx","sPassword":"xxxx"}

    TACHIBANA_BASE_URL にはベース URL（/auth/ より上のパス）を設定する。
    このスクリプトが /auth/ を自動補完する。

使い方:
    python scripts/check_tachibana_connection.py

前提:
    python scripts/setup_tachibana_config.py で .env に接続情報が設定済みであること。
"""
import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path

# ─── 依存ライブラリの存在確認 ────────────────────────────────────────────────

try:
    from dotenv import load_dotenv
except ImportError:
    print(
        "エラー: python-dotenv が見つかりません。\n"
        "  pip install python-dotenv を実行してください。",
        file=sys.stderr,
    )
    sys.exit(1)

try:
    import httpx
except ImportError:
    print(
        "エラー: httpx が見つかりません。\n"
        "  pip install httpx を実行してください。",
        file=sys.stderr,
    )
    sys.exit(1)

import os

# ─── パス・.env 読み込み ──────────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"

load_dotenv(_ENV_FILE)


# ─── 数値キー → 文字列キー マッピング ────────────────────────────────────────
#
# 立花証券 e支店 API は JSON レスポンスのキーを数値文字列で返す。
# 実レスポンスから確認済みのマッピングを定義する。
# 未知の数値キーはそのまま残し、生レスポンスで確認できるようにする。

_NUMERIC_KEY_MAP: dict[str, str] = {
    # ─── 共通フィールド ───────────────────────────────────────────────────────
    "286": "p_err_msg",               # p_errno メッセージ
    "287": "p_errno",                 # 通信レベルエラーコード（0=正常）
    "288": "p_no",                    # リクエスト連番エコー
    "289": "p_sd_date",               # リクエスト日時エコー
    "290": "p_date",                  # レスポンス日時
    "334": "sCLMID",                  # コマンド識別子
    "688": "sResultCode",             # 業務レベル結果コード（"0"=正常）
    "689": "sResultText",             # 業務レベル結果メッセージ
    # ─── ログイン応答 CLMAuthLoginRequest ─────────────────────────────────────
    "700": "sKinsyouhouMidokuFlg",    # 禁止事項未読フラグ（"1"=未読）
    "868": "sUrlEvent",               # イベント通知 URL
    "869": "sUrlEventWebSocket",      # WebSocket URL（仕様書未記載・実測確認済み）
    "870": "sUrlMaster",              # マスターデータ URL
    "871": "sUrlPrice",               # 価格照会 URL
    "872": "sUrlRequest",             # 業務 API URL（注文・照会・残高・建玉）
    # ─── 残高照会 CLMZanKaiKanougaku ──────────────────────────────────────────
    "744": "sSummaryGenkabuKaituke",  # 現物買付可能額（円、整数文字列・実測確認済み）
    # ─── 残高照会 CLMZanShinkiKanoIjiritu ─────────────────────────────────────
    "747": "sSummarySinyouSinkidate", # 信用新規建余力（円、整数文字列・実測確認済み）
}


def _normalize_response(data: dict) -> dict:
    """
    数値キー形式のレスポンスを文字列キー形式に変換して返す。

    - 既知の数値キー（_NUMERIC_KEY_MAP）は対応する文字列キーに変換する
    - 未知の数値キーはそのまま保持する（将来のキー発見のため）
    - 元の data は変更しない（生レスポンス表示に使用）
    """
    result: dict = {}
    for k, v in data.items():
        mapped = _NUMERIC_KEY_MAP.get(str(k))
        result[mapped if mapped else k] = v
    return result


# ─── p_errno コード表 ─────────────────────────────────────────────────────────
#
# デモ接続確認計画書の「p_errno コード表」に準拠。

_P_ERRNO_TABLE: dict[int, str] = {
    0:   "正常",
    2:   "session inactive — セッションが無効。再ログインが必要",
    6:   "p_no is no progress — p_no が単調増加していない",
    8:   "p_sd_date is exceed limit time — 実行環境の時刻がサーバーと大きくずれている",
    9:   "service offline — デモ環境のサービスが停止中",
    -2:  "database access error — サーバー側 DB エラー",
    -3:  "sapsv access error — サーバー側認証サービスエラー",
    -62: "information service offline — 情報系サービス停止中",
}

# 業務エラーコードのヒント（よくある認証失敗）
_RESULT_CODE_HINTS: dict[str, str] = {
    "10031":  "ユーザーID またはパスワードが正しくありません。",
    "900002": "パスワードが正しくありません。",
    "991036": "第二パスワードが正しくありません。",
}


# ─── ユーティリティ ───────────────────────────────────────────────────────────

def _mask(value: str, show: int = 2) -> str:
    """値をマスク表示する（先頭 show 文字のみ表示）"""
    if not value:
        return "(未設定)"
    if len(value) <= show * 2:
        return "****"
    return value[:show] + "****" + value[-show:]


def _describe_p_errno(code: int) -> str:
    return _P_ERRNO_TABLE.get(code, f"未定義のコード ({code})")


def _get_required_env(key: str) -> str:
    """
    環境変数を取得する。未設定・空文字の場合はエラーを出して終了する。
    パスワード類のキー名にはヒントを付与する。
    """
    value = os.getenv(key, "").strip()
    if not value:
        print(f"\nエラー: {key} が .env に設定されていません。", file=sys.stderr)
        print(
            "  python scripts/setup_tachibana_config.py を先に実行してください。",
            file=sys.stderr,
        )
        sys.exit(1)
    return value


def _print_section(title: str) -> None:
    print(f"\n{'─' * 40}")
    print(title)
    print("─" * 40)


def _safe_response_for_display(data: dict) -> dict:
    """
    ログ・表示用にレスポンスから資格情報を除去した dict を返す。
    サーバーが万一パスワードをエコーした場合も除去する。
    """
    sensitive_keys = {"spassword", "ssecondpassword", "p_password", "suserid"}
    return {k: v for k, v in data.items() if k.lower() not in sensitive_keys}


def _build_auth_url(base_url: str) -> str:
    """
    TACHIBANA_BASE_URL から認証エンドポイント URL を構築する。

    /auth/ で終わっていない場合は自動補完する。
      https://demo-kabuka.e-shiten.jp/e_api_v4r8/
        → https://demo-kabuka.e-shiten.jp/e_api_v4r8/auth/
      https://demo-kabuka.e-shiten.jp/e_api_v4r8/auth/
        → そのまま
    """
    stripped = base_url.rstrip("/")
    if stripped.endswith("/auth"):
        return stripped + "/"
    return stripped + "/auth/"


def _build_p_sd_date() -> str:
    """
    現在時刻を認証 I/F 仕様の形式で返す。
    形式: yyyy.mm.dd-hh:mn:ss.ttt
    例  : 2026.03.18-09:30:00.123
    """
    now = datetime.now()
    ms = now.microsecond // 1000
    return now.strftime("%Y.%m.%d-%H:%M:%S.") + f"{ms:03d}"


def _extract_html_title(text: str) -> str:
    """HTML から <title> タグの内容を抽出する。"""
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
    return m.group(1).strip() if m else "(title タグなし)"


def _is_html_response(content_type: str, raw_bytes: bytes) -> bool:
    """Content-Type またはコンテンツ先頭でHTML応答かどうかを判定する。"""
    if "text/html" in content_type.lower():
        return True
    sniff = raw_bytes[:10].lower()
    return sniff.startswith((b"<!doctype", b"<html"))


def _print_http_diagnostics(
    status_code: int,
    final_url: str,
    content_type: str,
    redirect_history: list,
) -> None:
    """HTTP レベルの診断情報を表示する。"""
    _print_section("HTTP 診断情報")
    print(f"  HTTP ステータス  : {status_code}")
    print(f"  最終 URL        : {final_url}")
    print(f"  Content-Type    : {content_type}")
    if redirect_history:
        print(f"  リダイレクト回数 : {len(redirect_history)} 回")
        for i, r in enumerate(redirect_history, 1):
            print(f"    [{i}] {r.status_code} → {r.url}")
    else:
        print("  リダイレクト    : なし")


# ─── 接続確認本体 ─────────────────────────────────────────────────────────────

async def check_connection() -> None:
    # ─── 設定読み込み ────────────────────────────────────────────────────────
    user_id         = _get_required_env("TACHIBANA_USER_ID")
    password        = _get_required_env("TACHIBANA_PASSWORD")
    base_url        = _get_required_env("TACHIBANA_BASE_URL")
    # SECOND_PASSWORD はログイン API では使用しないが設定漏れを事前検出する
    _second         = _get_required_env("TACHIBANA_SECOND_PASSWORD")   # noqa: F841

    # ─── 認証 URL 構築 ───────────────────────────────────────────────────────
    auth_url   = _build_auth_url(base_url)
    p_sd_date  = _build_p_sd_date()
    p_no       = "1"

    print("=" * 60)
    print("立花証券 e支店 API 接続確認")
    print("=" * 60)
    print(f"  ユーザーID    : {_mask(user_id)}")
    print(f"  パスワード    : ****")
    print(f"  第二パスワード: (設定済み) ****")
    print(f"  ベース URL    : {base_url}")
    print(f"  認証 URL      : {auth_url}  ← /auth/ 自動補完済み")
    print(f"  p_no          : {p_no}")
    print(f"  p_sd_date     : {p_sd_date}")
    print()
    print("CLMAuthLoginRequest を送信しています...")
    print("送信形式: GET {auth_url}?{JSON文字列}")

    # ─── リクエストペイロード（JSON文字列としてクエリに付与）────────────────
    payload: dict[str, str] = {
        "p_no":     p_no,
        "p_sd_date": p_sd_date,
        "sCLMID":   "CLMAuthLoginRequest",
        "sUserId":  user_id,
        "sPassword": password,
    }
    # 資格情報をログに出さないため、送信 URL はパスのみ表示する
    json_query = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    request_url = f"{auth_url}?{json_query}"

    # ─── HTTP リクエスト ──────────────────────────────────────────────────────
    raw_bytes: bytes | None = None
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            response = await client.get(request_url)
            raw_bytes = response.content

    except httpx.TimeoutException:
        print("\n❌ タイムアウト: サーバーから応答がありません。")
        print("   確認事項:")
        print("     - TACHIBANA_BASE_URL が正しいか")
        print("     - デモ環境が稼働中か（サービス時間帯か）")
        sys.exit(1)

    except httpx.NetworkError as exc:
        print(f"\n❌ ネットワークエラー: {exc}")
        print("   確認事項:")
        print("     - インターネット接続が有効か")
        print("     - TACHIBANA_BASE_URL の URL に誤りがないか")
        sys.exit(1)

    # ─── HTTP 診断情報の収集 ──────────────────────────────────────────────────
    status_code     = response.status_code
    final_url       = str(response.url)
    content_type    = response.headers.get("content-type", "(なし)")
    redirect_history = list(response.history)

    _print_http_diagnostics(status_code, final_url, content_type, redirect_history)

    # HTTP エラーステータスの判定（HTML 判定より先に実施しない — 先に内容を確認）
    if status_code >= 400:
        print(f"\n❌ HTTP エラー: ステータス {status_code}")
        print(f"   レスポンス先頭: {raw_bytes[:300]!r}")
        sys.exit(1)

    # ─── Shift-JIS デコード ───────────────────────────────────────────────────
    try:
        text = raw_bytes.decode("shift-jis")
    except UnicodeDecodeError:
        try:
            text = raw_bytes.decode("utf-8", errors="replace")
        except Exception:
            text = raw_bytes.decode("latin-1", errors="replace")

    # ─── HTML 応答の検出 ──────────────────────────────────────────────────────
    if _is_html_response(content_type, raw_bytes):
        html_title = _extract_html_title(text)
        _print_section("HTML 応答を検出 ❌")
        print(f"  HTML title    : {html_title}")
        print(f"  先頭 300 文字 :")
        print(f"    {text[:300]}")
        print()
        print("  ⚠️  認証エンドポイント (/auth/) ではなく API メニュー / 親ディレクトリを")
        print("      踏んでいる可能性があります。")
        print()
        print("  【次に確認すべき事項】")
        print("  TACHIBANA_BASE_URL にはベース URL（/auth/ より上のパス）を設定してください。")
        print("  認証 I/F 仕様:")
        print("    送信形式: GET {ベースURL}/auth/?{JSON文字列}")
        print("    例: https://demo-kabuka.e-shiten.jp/e_api_v4r8/auth/")
        print("          ?{\"p_no\":\"1\",\"sCLMID\":\"CLMAuthLoginRequest\",...}")
        print()
        print("  ベース URL の確認方法:")
        print("    1. 立花証券から提供された REQUEST I/F 資料（PDF / Excel）を開く")
        print("    2. 認証機能の「e支店・API専用URL」欄を確認する")
        print("    3. サンプルコードがある場合は GET 先のベースパスを確認する")
        print()
        print("  確認後、以下を実行して URL を再設定してください。")
        print("    python scripts/setup_tachibana_config.py")
        sys.exit(1)

    # ─── JSON パース ─────────────────────────────────────────────────────────
    try:
        raw_data: dict = json.loads(text)
    except json.JSONDecodeError:
        _print_section("JSON パース失敗 ❌")
        print("  JSON でも HTML でもないレスポンスが返りました。")
        print(f"  Content-Type : {content_type}")
        print(f"  先頭 300 文字: {text[:300]!r}")
        print()
        print("  確認事項:")
        print("    - TACHIBANA_BASE_URL のベースパスが正しいか")
        print("    - 認証 I/F 仕様: GET {ベースURL}/auth/?{JSON文字列}")
        print("    - 資料・サンプルコードの GET 先ベース URL を再確認してください")
        sys.exit(1)

    # ─── 数値キー正規化 ───────────────────────────────────────────────────────
    # 立花証券 API は数値キー形式で返す。文字列キーにマッピングして以降の処理に使用する。
    # raw_data は生レスポンス表示用に保持する。
    data = _normalize_response(raw_data)

    # ─── p_errno チェック ─────────────────────────────────────────────────────
    p_errno_raw = data.get("p_errno")
    p_errno = 0
    if p_errno_raw is not None:
        try:
            p_errno = int(p_errno_raw)
        except (ValueError, TypeError):
            pass

    # ─── sResultCode チェック ────────────────────────────────────────────────
    s_result_code = str(data.get("sResultCode", ""))
    s_result_text = str(data.get("sResultText", ""))

    # ─── 仮想 URL ─────────────────────────────────────────────────────────────
    url_request      = str(data.get("sUrlRequest",          ""))
    url_price        = str(data.get("sUrlPrice",            ""))
    url_master       = str(data.get("sUrlMaster",           ""))
    url_event        = str(data.get("sUrlEvent",            ""))
    url_event_ws     = str(data.get("sUrlEventWebSocket",   ""))
    kinsyouhou       = str(data.get("sKinsyouhouMidokuFlg", "0"))

    # ─── 結果表示 ─────────────────────────────────────────────────────────────
    _print_section("レスポンス概要")
    print(f"  p_errno     : {p_errno_raw!r}")
    print(f"  p_errno 解釈: {_describe_p_errno(p_errno)}")
    print(f"  sResultCode : {s_result_code!r}")
    print(f"  sResultText : {s_result_text!r}")

    # p_errno エラー
    if p_errno != 0:
        print(f"\n❌ 通信レベルエラー: p_errno={p_errno}")
        print(f"   意味: {_describe_p_errno(p_errno)}")
        _print_raw_response(raw_data)
        sys.exit(1)

    # sResultCode エラー
    if s_result_code != "0":
        print(f"\n❌ 業務レベルエラー: sResultCode={s_result_code}")
        print(f"   sResultText: {s_result_text!r}")
        hint = _RESULT_CODE_HINTS.get(s_result_code)
        if hint:
            print(f"   ヒント: {hint}")
        _print_raw_response(raw_data)
        sys.exit(1)

    # ─── 認証成功 ─────────────────────────────────────────────────────────────
    _print_section("認証結果 ✅")
    print(f"  sResultCode          : {s_result_code!r}  （0 = 正常）")
    print(f"  sResultText          : {s_result_text!r}")
    print(f"  sKinsyouhouMidokuFlg : {kinsyouhou!r}  （0 = 取引可能）")

    # ─── 仮想 URL 確認 ────────────────────────────────────────────────────────
    _print_section("仮想 URL 取得状況")
    url_fields = {
        "sUrlRequest":        url_request,
        "sUrlPrice":          url_price,
        "sUrlMaster":         url_master,
        "sUrlEvent":          url_event,
        "sUrlEventWebSocket": url_event_ws,
    }
    for field, value in url_fields.items():
        status = "✅" if value else "⚠️  (空)"
        print(f"  {field:<22}: {status}  {value or '—'}")

    # ─── sKinsyouhouMidokuFlg ────────────────────────────────────────────────
    _print_section("取引可否フラグ")
    if kinsyouhou == "1":
        print("  sKinsyouhouMidokuFlg: ⚠️  1 (禁止事項未読 — 取引不可)")
        print()
        print("  立花証券の Web サイトにログインして未読通知を確認してください。")
        print("  確認後、セッションを再確立すると取引可能になります。")
    else:
        print(f"  sKinsyouhouMidokuFlg: ✅  {kinsyouhou!r} (取引可能)")

    # ─── 最終判定 ─────────────────────────────────────────────────────────────
    _print_section("最終判定")

    critical_urls_ok = bool(url_request and url_price)

    if kinsyouhou == "1":
        print("⚠️  認証成功 / 取引不可 (sKinsyouhouMidokuFlg=1)")
        print("   未読通知を確認後に再実行してください。")
    elif not critical_urls_ok:
        print("⚠️  認証成功 / 仮想 URL が一部取得できませんでした")
        missing = [f for f, v in url_fields.items() if not v]
        print(f"   取得できなかったフィールド: {', '.join(missing)}")
        print("   確認計画書に従い実際のフィールド名を確認してください。")
    else:
        print("✅  認証成功。デモ接続確認に進めます。")

    # ─── 生レスポンス（補助）────────────────────────────────────────────────
    _print_raw_response(raw_data)

    print()


def _print_raw_response(raw_data: dict) -> None:
    """
    生レスポンス（数値キーのまま）と既知キーの対訳を表示する。
    資格情報キーは除去済み。未知の数値キーが含まれる場合は対訳なしで表示する。
    """
    safe = _safe_response_for_display(raw_data)
    _print_section("生レスポンス（参考・資格情報除去済み）")
    print(json.dumps(safe, ensure_ascii=False, indent=2))

    # 未知の数値キーがあれば表示（将来のキー発見のため）
    unknown = {k: v for k, v in safe.items() if k not in _NUMERIC_KEY_MAP and k.isdigit()}
    if unknown:
        print()
        print("  ─ 未知の数値キー（マッピング未定義）─")
        for k, v in unknown.items():
            print(f"    {k!r}: {v!r}")


# ─── エントリーポイント ───────────────────────────────────────────────────────

def main() -> None:
    asyncio.run(check_connection())


if __name__ == "__main__":
    main()
