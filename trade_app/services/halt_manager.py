"""
HaltManager サービス
取引停止（halt）の発動・解除・照会を管理する。

halt 状態の正本は trading_halts テーブル（DB）。
再起動後も DB から状態を復元するため、インメモリキャッシュは使用しない。

halt の種別:
  daily_loss        : 日次損失上限到達（DAILY_LOSS_LIMIT_JPY を超えた）
  consecutive_losses: N連続損失（CONSECUTIVE_LOSSES_STOP 件以上）
  manual            : API 経由の手動停止

使用例:
  # RiskManager.check() 内で halt チェック
  halt_mgr = HaltManager()
  if await halt_mgr.is_halted(db):
      raise RiskRejectedError("取引停止中")

  # ポジション決済後に連続損失チェック
  await halt_mgr.check_and_halt_if_needed(db, settings)
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.config import Settings, get_settings
from trade_app.models.enums import AuditEventType, HaltType, SystemEventType
from trade_app.models.trading_halt import TradingHalt
from trade_app.models.trade_result import TradeResult

logger = logging.getLogger(__name__)


class HaltManager:
    """
    取引停止状態を DB で管理するサービス。

    is_active=True の halt が1件でも存在すれば新規発注を全て拒否する。
    """

    async def is_halted(self, db: AsyncSession) -> tuple[bool, str]:
        """
        現在取引停止状態かどうかを返す。

        Returns:
            (is_halted, reason_summary): 停止中かどうかと理由のサマリー
        """
        result = await db.execute(
            select(TradingHalt)
            .where(TradingHalt.is_active == True)  # noqa: E712
            .order_by(TradingHalt.activated_at.asc())
        )
        active_halts = result.scalars().all()

        if not active_halts:
            return False, ""

        reasons = [f"{h.halt_type}: {h.reason}" for h in active_halts]
        return True, " / ".join(reasons)

    async def get_active_halts(self, db: AsyncSession) -> list[TradingHalt]:
        """アクティブな halt 一覧を返す"""
        result = await db.execute(
            select(TradingHalt)
            .where(TradingHalt.is_active == True)  # noqa: E712
            .order_by(TradingHalt.activated_at.desc())
        )
        return list(result.scalars().all())

    async def activate_halt(
        self,
        db: AsyncSession,
        halt_type: HaltType,
        reason: str,
        activated_by: str = "system",
        details: dict | None = None,
    ) -> TradingHalt:
        """
        halt を発動する。同一種別の halt がすでにアクティブな場合は重複して作成しない。

        Returns:
            新規作成した TradingHalt、または既存のアクティブ halt
        """
        # 同一種別の active halt が既に存在するか確認
        result = await db.execute(
            select(TradingHalt).where(
                TradingHalt.halt_type == halt_type.value,
                TradingHalt.is_active == True,  # noqa: E712
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            logger.debug(
                "HaltManager: %s halt は既にアクティブ id=%s",
                halt_type.value, existing.id[:8],
            )
            return existing

        now = datetime.now(timezone.utc)
        halt = TradingHalt(
            halt_type=halt_type.value,
            reason=reason,
            is_active=True,
            activated_at=now,
            activated_by=activated_by,
            details=details,
            created_at=now,
        )
        db.add(halt)
        await db.flush()

        logger.warning(
            "HaltManager: 取引停止発動 type=%s reason=%s by=%s",
            halt_type.value, reason, activated_by,
        )

        # システムイベントを記録
        from trade_app.models.system_event import SystemEvent
        event = SystemEvent(
            event_type=SystemEventType.HALT_ACTIVATED.value,
            details={"halt_id": halt.id, "halt_type": halt_type.value, **(details or {})},
            message=f"取引停止発動: {halt_type.value} — {reason}",
            created_at=now,
        )
        db.add(event)

        return halt

    async def deactivate_halt(
        self,
        db: AsyncSession,
        halt_id: str,
        deactivated_by: str = "manual",
    ) -> TradingHalt | None:
        """
        指定した halt を解除する。

        Returns:
            更新した TradingHalt、または None（見つからない場合）
        """
        result = await db.execute(
            select(TradingHalt).where(TradingHalt.id == halt_id)
        )
        halt = result.scalar_one_or_none()
        if halt is None:
            logger.warning("HaltManager: halt が見つかりません id=%s", halt_id)
            return None

        if not halt.is_active:
            logger.warning("HaltManager: halt は既に非アクティブ id=%s", halt_id)
            return halt

        now = datetime.now(timezone.utc)
        halt.is_active = False
        halt.deactivated_at = now
        halt.deactivated_by = deactivated_by

        await db.flush()

        logger.info(
            "HaltManager: 取引停止解除 id=%s type=%s by=%s",
            halt.id[:8], halt.halt_type, deactivated_by,
        )

        # システムイベントを記録
        from trade_app.models.system_event import SystemEvent
        event = SystemEvent(
            event_type=SystemEventType.HALT_DEACTIVATED.value,
            details={"halt_id": halt.id, "halt_type": halt.halt_type},
            message=f"取引停止解除: {halt.halt_type} by={deactivated_by}",
            created_at=now,
        )
        db.add(event)

        return halt

    async def deactivate_all_halts(
        self,
        db: AsyncSession,
        deactivated_by: str = "manual",
    ) -> int:
        """全アクティブ halt を解除する。解除件数を返す。"""
        halts = await self.get_active_halts(db)
        for halt in halts:
            await self.deactivate_halt(db, halt.id, deactivated_by)
        return len(halts)

    async def check_and_halt_if_needed(
        self,
        db: AsyncSession,
        settings: Settings | None = None,
    ) -> list[TradingHalt]:
        """
        ポジション決済後に呼び出すことで、日次損失・連続損失の halt 条件を評価する。
        条件を満たす場合は halt を発動し、発動した halt リストを返す。

        Args:
            db      : AsyncSession
            settings: 設定（None の場合はデフォルト設定を使用）

        Returns:
            新規に発動した TradingHalt のリスト（既存の halt は含まない）
        """
        from zoneinfo import ZoneInfo
        _JST = ZoneInfo("Asia/Tokyo")

        cfg = settings or get_settings()
        new_halts: list[TradingHalt] = []

        # ─── 日次損失チェック ──────────────────────────────────────────────
        today_jst = datetime.now(_JST).date()
        today_jst_start = datetime(
            today_jst.year, today_jst.month, today_jst.day, tzinfo=_JST
        )

        from sqlalchemy import func
        loss_result = await db.execute(
            select(func.coalesce(func.sum(TradeResult.pnl), 0.0)).where(
                TradeResult.created_at >= today_jst_start,
                TradeResult.pnl < 0,
            )
        )
        daily_loss = abs(loss_result.scalar() or 0.0)

        if daily_loss >= cfg.DAILY_LOSS_LIMIT_JPY:
            halt = await self.activate_halt(
                db=db,
                halt_type=HaltType.DAILY_LOSS,
                reason=(
                    f"日次損失上限到達: {daily_loss:,.0f}円 "
                    f"(上限: {cfg.DAILY_LOSS_LIMIT_JPY:,.0f}円)"
                ),
                details={"daily_loss": daily_loss, "limit": cfg.DAILY_LOSS_LIMIT_JPY},
            )
            # activate_halt は既存を返すこともあるので、新規作成分のみ追加
            if halt not in new_halts:
                new_halts.append(halt)

        # ─── 連続損失チェック ──────────────────────────────────────────────
        n = cfg.CONSECUTIVE_LOSSES_STOP
        if n > 0:
            recent_result = await db.execute(
                select(TradeResult.pnl)
                .order_by(TradeResult.created_at.desc())
                .limit(n)
            )
            recent_pnls = [row[0] for row in recent_result.fetchall()]

            if len(recent_pnls) == n and all(p < 0 for p in recent_pnls):
                total_recent_loss = abs(sum(recent_pnls))
                halt = await self.activate_halt(
                    db=db,
                    halt_type=HaltType.CONSECUTIVE_LOSSES,
                    reason=(
                        f"{n}連続損失を検出: "
                        f"直近損失合計 {total_recent_loss:,.0f}円"
                    ),
                    details={
                        "consecutive_count": n,
                        "recent_pnls": recent_pnls,
                        "total_recent_loss": total_recent_loss,
                    },
                )
                if halt not in new_halts:
                    new_halts.append(halt)

        return new_halts
