"""
SymbolConfigService — 銘柄設定 CRUD サービス

仕様書: 管理画面仕様書 v0.3 §3(SCR-04, SCR-05), §4(銘柄操作)

【設計方針】
- symbol_code は作成後変更不可（サービス層でガード）
- 削除は論理削除（deleted_at 設定）
- 変更前後のスナップショットを返して監査ログ記録を呼び出し元に委譲する
- 有効/無効の切り替えは update() で実施（toggle_enabled() ショートカットも提供）

【DB migration 保留中の注意】
strategy_id は文字列として保存するが、参照先テーブル (strategy_configs) が
まだ存在しない可能性がある（I-1, O-4 未確定）。
strategy_id のバリデーション（存在チェック）は migration 確定後に追加すること。
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.admin.models.symbol_config import SymbolConfig
from trade_app.admin.schemas.symbol_config import (
    SymbolConfigCreate,
    SymbolConfigFilter,
    SymbolConfigUpdate,
)

logger = logging.getLogger(__name__)


class SymbolConfigService:
    """銘柄設定の CRUD サービス"""

    def __init__(self, db: AsyncSession):
        self._db = db

    async def create(
        self, data: SymbolConfigCreate, created_by: str | None = None
    ) -> tuple[SymbolConfig, dict]:
        """
        銘柄設定を新規作成する。
        Returns: (作成したSymbolConfig, after_json)
        Raises: ValueError: symbol_code が既に存在する場合
        """
        # 重複チェック（論理削除済みを含む）
        existing = await self._db.execute(
            select(SymbolConfig).where(SymbolConfig.symbol_code == data.symbol_code)
        )
        if existing.scalar_one_or_none() is not None:
            raise ValueError(f"symbol_code '{data.symbol_code}' は既に存在します。")

        symbol = SymbolConfig(
            symbol_code=data.symbol_code,
            symbol_name=data.symbol_name,
            trade_type=data.trade_type,
            strategy_id=data.strategy_id,
            is_enabled=data.is_enabled,
            notes=data.notes,
            open_behavior=data.open_behavior,
            trading_start_time=data.trading_start_time,
            trading_end_time=data.trading_end_time,
            max_single_investment_jpy=data.max_single_investment_jpy,
            max_daily_investment_jpy=data.max_daily_investment_jpy,
            take_profit_pct=float(data.take_profit_pct),
            stop_loss_pct=float(data.stop_loss_pct),
            max_hold_minutes=data.max_hold_minutes,
            created_by=created_by,
            updated_by=created_by,
        )
        self._db.add(symbol)
        await self._db.flush()  # ID を確定

        after_json = self._to_dict(symbol)
        logger.info("銘柄設定作成: code=%s", data.symbol_code)
        return symbol, after_json

    async def get(self, symbol_id: str) -> SymbolConfig | None:
        """IDで銘柄設定を1件取得"""
        result = await self._db.execute(
            select(SymbolConfig).where(
                SymbolConfig.id == symbol_id,
                SymbolConfig.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def get_by_code(self, symbol_code: str) -> SymbolConfig | None:
        """銘柄コードで1件取得"""
        result = await self._db.execute(
            select(SymbolConfig).where(
                SymbolConfig.symbol_code == symbol_code,
                SymbolConfig.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def list(
        self,
        filters: SymbolConfigFilter,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[SymbolConfig], int]:
        """
        フィルタ条件で銘柄設定一覧を取得。
        Returns: (symbols, total_count)
        """
        stmt = select(SymbolConfig)
        count_stmt = select(func.count(SymbolConfig.id))

        # 論理削除フィルタ
        if not filters.include_deleted:
            stmt = stmt.where(SymbolConfig.deleted_at.is_(None))
            count_stmt = count_stmt.where(SymbolConfig.deleted_at.is_(None))

        if filters.trade_type:
            stmt = stmt.where(SymbolConfig.trade_type == filters.trade_type)
            count_stmt = count_stmt.where(SymbolConfig.trade_type == filters.trade_type)
        if filters.is_enabled is not None:
            stmt = stmt.where(SymbolConfig.is_enabled == filters.is_enabled)
            count_stmt = count_stmt.where(SymbolConfig.is_enabled == filters.is_enabled)
        if filters.strategy_id:
            stmt = stmt.where(SymbolConfig.strategy_id == filters.strategy_id)
            count_stmt = count_stmt.where(SymbolConfig.strategy_id == filters.strategy_id)
        if filters.search:
            pattern = f"%{filters.search}%"
            cond = or_(
                SymbolConfig.symbol_code.ilike(pattern),
                SymbolConfig.symbol_name.ilike(pattern),
            )
            stmt = stmt.where(cond)
            count_stmt = count_stmt.where(cond)

        stmt = stmt.order_by(SymbolConfig.symbol_code).offset(offset).limit(limit)

        result = await self._db.execute(stmt)
        symbols = list(result.scalars().all())

        count_result = await self._db.execute(count_stmt)
        total = count_result.scalar() or 0

        return symbols, total

    async def update(
        self, symbol_id: str, data: SymbolConfigUpdate, updated_by: str | None = None
    ) -> tuple[SymbolConfig, dict, dict]:
        """
        銘柄設定を更新する。symbol_code は変更不可。
        Returns: (更新したSymbolConfig, before_json, after_json)
        Raises: ValueError: symbol_id が存在しない場合
        """
        symbol = await self.get(symbol_id)
        if symbol is None:
            raise ValueError(f"銘柄設定が見つかりません: {symbol_id}")

        before_json = self._to_dict(symbol)

        update_data = data.model_dump(exclude_none=True)
        for key, value in update_data.items():
            setattr(symbol, key, value)

        symbol.updated_by = updated_by
        symbol.updated_at = datetime.now(timezone.utc)

        after_json = self._to_dict(symbol)
        logger.info("銘柄設定更新: id=%s code=%s", symbol_id[:8], symbol.symbol_code)
        return symbol, before_json, after_json

    async def toggle_enabled(
        self, symbol_id: str, enabled: bool, updated_by: str | None = None
    ) -> tuple[SymbolConfig, dict, dict]:
        """
        有効/無効を切り替える。update() のショートカット。
        Returns: (更新したSymbolConfig, before_json, after_json)
        """
        data = SymbolConfigUpdate(is_enabled=enabled)
        return await self.update(symbol_id, data, updated_by)

    async def soft_delete(
        self, symbol_id: str, deleted_by: str | None = None
    ) -> tuple[SymbolConfig, dict]:
        """
        論理削除する。deleted_at を設定する。
        Returns: (削除したSymbolConfig, before_json)
        Raises: ValueError: symbol_id が存在しない場合
        """
        symbol = await self.get(symbol_id)
        if symbol is None:
            raise ValueError(f"銘柄設定が見つかりません: {symbol_id}")

        before_json = self._to_dict(symbol)
        symbol.deleted_at = datetime.now(timezone.utc)
        symbol.updated_by = deleted_by
        symbol.updated_at = datetime.now(timezone.utc)

        logger.info("銘柄設定論理削除: id=%s code=%s", symbol_id[:8], symbol.symbol_code)
        return symbol, before_json

    @staticmethod
    def _to_dict(symbol: SymbolConfig) -> dict:
        """監査ログ用のスナップショットを生成する"""
        return {
            "id": symbol.id,
            "symbol_code": symbol.symbol_code,
            "symbol_name": symbol.symbol_name,
            "trade_type": symbol.trade_type,
            "strategy_id": symbol.strategy_id,
            "is_enabled": symbol.is_enabled,
            "open_behavior": symbol.open_behavior,
            "trading_start_time": str(symbol.trading_start_time) if symbol.trading_start_time else None,
            "trading_end_time": str(symbol.trading_end_time) if symbol.trading_end_time else None,
            "max_single_investment_jpy": symbol.max_single_investment_jpy,
            "max_daily_investment_jpy": symbol.max_daily_investment_jpy,
            "take_profit_pct": float(symbol.take_profit_pct),
            "stop_loss_pct": float(symbol.stop_loss_pct),
            "max_hold_minutes": symbol.max_hold_minutes,
        }
