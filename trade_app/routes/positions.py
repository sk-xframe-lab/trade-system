"""
GET /api/positions      — ポジション一覧
GET /api/positions/{id} — ポジション詳細
"""
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.config import get_settings
from trade_app.models.database import get_db
from trade_app.models.enums import PositionStatus
from trade_app.models.position import Position
from trade_app.schemas.position import PositionResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/positions", tags=["positions"])
settings = get_settings()


def _verify_auth(authorization: str) -> None:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="認証エラー")
    token = authorization.removeprefix("Bearer ").strip()
    if token != settings.API_TOKEN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="無効なトークン")


@router.get("", response_model=list[PositionResponse], summary="オープンポジション一覧")
async def list_positions(
    db: AsyncSession = Depends(get_db),
    authorization: str = Header(...),
    include_closed: bool = False,
):
    """
    ポジション一覧を返す。
    デフォルトはオープン中のみ。include_closed=true で決済済みも含む。
    """
    _verify_auth(authorization)

    query = select(Position).order_by(Position.opened_at.desc())
    if not include_closed:
        query = query.where(
            Position.status.in_([PositionStatus.OPEN.value, PositionStatus.CLOSING.value])
        )

    result = await db.execute(query)
    return [PositionResponse.model_validate(p) for p in result.scalars().all()]


@router.get("/{position_id}", response_model=PositionResponse, summary="ポジション詳細")
async def get_position(
    position_id: str,
    db: AsyncSession = Depends(get_db),
    authorization: str = Header(...),
):
    """指定したポジションの詳細を返す"""
    _verify_auth(authorization)

    result = await db.execute(
        select(Position).where(Position.id == position_id)
    )
    position = result.scalar_one_or_none()

    if position is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"ポジションが見つかりません: {position_id}",
        )

    return PositionResponse.model_validate(position)
