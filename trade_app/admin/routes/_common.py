"""
admin ルーター共通ユーティリティ

【責務】
- クライアント IP / UA の取得（X-Forwarded-For 対応）
- admin API エラーレスポンス規約の定義

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
admin API エラーレスポンス規約
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

すべての 4xx/5xx レスポンスは FastAPI 標準の {"detail": "<メッセージ>"} 形式を使用する。

| HTTP ステータス | 用途                                       | 例                       |
|---------------|------------------------------------------|--------------------------|
| 400           | リクエストの意味的エラー（ロジック上の不正）   | 通知先フォーマット不正       |
| 401           | 認証失敗・セッション無効                     | トークン不正、有効期限切れ   |
| 403           | 認証済みだが権限不足（Phase 2 以降）         | viewer が admin 操作を試みる |
| 404           | リソースが存在しない                         | 指定 ID の銘柄が見つからない  |
| 409           | リソース作成時の一意制約違反                  | symbol_code が既に存在する   |
| 422           | Pydantic バリデーションエラー（FastAPI 自動）| 必須フィールド欠落           |
| 500           | 予期しないサーバーエラー                     | DB 接続障害                |

Pydantic バリデーション (422) は FastAPI が自動で返すため、ルーター側では特別な処理不要。
ValueError を except する場合は意味に応じて 400 / 404 / 409 を明示的に返す。

【エラーコード運用ルール】
- 重複作成 (create): 409 Conflict
- 存在しないリソースへの操作 (update/delete/get): 404 Not Found
- 論理的不正入力（スキーマは通るがビジネスルール違反）: 400 Bad Request
- 認証失敗: 401 Unauthorized（auth_guard が返す）
- ロール不足: 403 Forbidden（require_role が返す。Phase 2 以降）
"""
from fastapi import Request


def get_client_ip(request: Request) -> str | None:
    """
    クライアントの実際の IP アドレスを返す。

    X-Forwarded-For ヘッダーがある場合は最初の値（実クライアントIP）を返す。
    リバースプロキシ（nginx/ALB）後ろで動作する場合に正確な IP を取得するために必要。
    ヘッダーなし・client なしの場合は None を返す。
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def get_user_agent(request: Request) -> str | None:
    """クライアントの User-Agent を返す。存在しない場合は None。"""
    return request.headers.get("User-Agent")
