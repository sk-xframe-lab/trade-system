"""
共通スキーマ — ページネーション・共通レスポンス形式

仕様書: 管理画面仕様書 v0.3 全体（一覧API共通）
"""
from typing import Generic, TypeVar
from pydantic import BaseModel, Field

T = TypeVar("T")


class PaginationQuery(BaseModel):
    """一覧取得の共通クエリパラメータ"""
    page: int = Field(default=1, ge=1, description="ページ番号（1始まり）")
    per_page: int = Field(default=50, ge=1, le=200, description="1ページあたりの件数")

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.per_page


class PaginatedResponse(BaseModel, Generic[T]):
    """ページネーション付き一覧レスポンス"""
    items: list[T]
    total: int
    page: int
    per_page: int
    total_pages: int

    model_config = {"from_attributes": True}

    @classmethod
    def build(cls, items: list[T], total: int, page: int, per_page: int) -> "PaginatedResponse[T]":
        total_pages = (total + per_page - 1) // per_page if per_page > 0 else 0
        return cls(items=items, total=total, page=page, per_page=per_page, total_pages=total_pages)


class MessageResponse(BaseModel):
    """シンプルなメッセージレスポンス"""
    message: str
    success: bool = True


class AdminErrorResponse(BaseModel):
    """
    admin API エラーレスポンスの標準形式（ドキュメント用）。

    FastAPI は HTTPException を {"detail": "<message>"} 形式で自動シリアライズする。
    Pydantic バリデーションエラー (422) は {"detail": [{...}, ...]} 形式。

    ルーターで HTTPException を raise する場合は detail に日本語メッセージを渡す。
    クライアントは常に "detail" キーを参照すること。

    例:
        {"detail": "銘柄設定が見つかりません"}           # 404
        {"detail": "このsymbol_codeは既に存在します"}    # 409
        {"detail": "認証が必要です。..."}               # 401
    """
    detail: str
