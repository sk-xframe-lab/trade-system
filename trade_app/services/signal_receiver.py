"""
SignalReceiver サービス
シグナルの受信・冪等性チェック・DB保存を担当する。

冪等性制御:
  - Idempotency-Key を Redis に TTL=24時間で保存
  - 同一キーの2回目以降は DuplicateSignalError を送出（409 で返す）
  - Redis 障害時はフォールバックとして DB の UNIQUE 制約に依存する

二重受信防止の2層構造:
  1. Redis (高速): 通常の重複をブロック
  2. PostgreSQL UNIQUE 制約: Redis 障害時のフォールバック
"""
import logging
from datetime import datetime, timezone

import redis.asyncio as redis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.models.enums import AuditEventType, SignalStatus
from trade_app.models.signal import TradeSignal
from trade_app.schemas.signal import SignalRequest
from trade_app.services.audit_logger import AuditLogger

logger = logging.getLogger(__name__)

# 冪等性キーの有効期間（24時間）
_IDEMPOTENCY_TTL_SEC = 86_400
# Redis キーのプレフィックス
_REDIS_KEY_PREFIX = "idem:"


class DuplicateSignalError(Exception):
    """同一 Idempotency-Key のシグナルが既に処理済みの場合に送出"""
    def __init__(self, signal_id: str) -> None:
        self.signal_id = signal_id
        super().__init__(f"既に処理済みのシグナル: {signal_id}")


class SignalReceiver:
    """
    シグナル受信・保存サービス。
    DBセッションと Redis クライアントをコンストラクタで受け取る。
    """

    def __init__(
        self,
        db: AsyncSession,
        redis_client: redis.Redis,
        audit: AuditLogger,
    ) -> None:
        self._db = db
        self._redis = redis_client
        self._audit = audit

    async def receive(
        self,
        request: SignalRequest,
        idempotency_key: str,
        source_system: str,
    ) -> TradeSignal:
        """
        シグナルを受信し、冪等性チェック後に DB へ保存する。

        処理フロー:
          1. Redis で Idempotency-Key の重複チェック
          2. TradeSignal を DB に保存（RECEIVED 状態）
          3. Redis にキーを登録（TTL=24時間）
          4. 監査ログを記録

        Args:
            request        : Pydantic バリデーション済みシグナルデータ
            idempotency_key: リクエストヘッダーの Idempotency-Key
            source_system  : リクエストヘッダーの X-Source-System

        Returns:
            保存した TradeSignal オブジェクト

        Raises:
            DuplicateSignalError: 同一キーが Redis に存在する場合
        """
        # ─── Step 1: Redis で冪等性チェック ──────────────────────────────
        redis_key = f"{_REDIS_KEY_PREFIX}{idempotency_key}"
        existing = await self._check_redis_idempotency(redis_key)
        if existing:
            logger.info("重複シグナル検出 (Redis): key=%s signal_id=%s", idempotency_key, existing)
            await self._audit.log(
                event_type=AuditEventType.SIGNAL_DUPLICATE,
                entity_type="signal",
                entity_id=existing,
                details={"idempotency_key": idempotency_key, "source_system": source_system},
                message=f"重複シグナル (Redis キャッシュヒット)",
            )
            await self._db.commit()
            raise DuplicateSignalError(signal_id=existing)

        # ─── Step 2: TradeSignal を DB に保存 ─────────────────────────────
        signal = TradeSignal(
            idempotency_key=idempotency_key,
            source_system=source_system,
            ticker=request.ticker,
            signal_type=request.signal_type,
            order_type=request.order_type.value,
            side=request.side.value,
            quantity=request.quantity,
            limit_price=request.limit_price,
            stop_price=request.stop_price,
            strategy=request.strategy,
            score=request.score,
            generated_at=request.generated_at,
            received_at=datetime.now(timezone.utc),
            status=SignalStatus.RECEIVED.value,
            metadata_json=request.metadata,
        )

        self._db.add(signal)

        try:
            await self._db.flush()  # ID を確定させる
        except IntegrityError:
            # PostgreSQL の UNIQUE 制約に引っかかった = DB フォールバック重複検知
            await self._db.rollback()
            existing_signal = await self._find_by_idempotency_key(idempotency_key)
            existing_id = existing_signal.id if existing_signal else "unknown"
            logger.warning(
                "重複シグナル検出 (DB UNIQUE): key=%s signal_id=%s",
                idempotency_key, existing_id
            )
            raise DuplicateSignalError(signal_id=existing_id)

        # ─── Step 3: Redis にキーを登録 ───────────────────────────────────
        await self._register_redis_idempotency(redis_key, signal.id)

        # ─── Step 4: 監査ログ ─────────────────────────────────────────────
        await self._audit.log(
            event_type=AuditEventType.SIGNAL_RECEIVED,
            entity_type="signal",
            entity_id=signal.id,
            details={
                "ticker": signal.ticker,
                "side": signal.side,
                "order_type": signal.order_type,
                "quantity": signal.quantity,
                "strategy": signal.strategy,
                "score": signal.score,
                "source_system": source_system,
            },
            message=f"シグナル受信: {signal.ticker} {signal.side} {signal.quantity}株",
        )

        await self._db.commit()
        await self._db.refresh(signal)

        logger.info(
            "シグナル保存完了: id=%s ticker=%s side=%s qty=%d",
            signal.id, signal.ticker, signal.side, signal.quantity
        )
        return signal

    # ─── 内部ヘルパー ──────────────────────────────────────────────────────

    async def _check_redis_idempotency(self, redis_key: str) -> str | None:
        """Redis で既存キーを確認する。存在すれば signal_id を返す。"""
        try:
            value = await self._redis.get(redis_key)
            if value:
                return value.decode() if isinstance(value, bytes) else value
        except Exception as e:
            # Redis 障害時はログを残してスルー（DB の UNIQUE 制約で保護）
            logger.error("Redis 冪等性チェックエラー（DB フォールバックで継続）: %s", e)
        return None

    async def _register_redis_idempotency(self, redis_key: str, signal_id: str) -> None:
        """Redis に冪等性キーを登録する。失敗してもロールバックしない（DB が保護）"""
        try:
            await self._redis.setex(redis_key, _IDEMPOTENCY_TTL_SEC, signal_id)
        except Exception as e:
            logger.error("Redis 冪等性キー登録エラー（DB の UNIQUE 制約で保護）: %s", e)

    async def _find_by_idempotency_key(self, idempotency_key: str) -> TradeSignal | None:
        """idempotency_key で TradeSignal を検索する"""
        from sqlalchemy import select
        try:
            result = await self._db.execute(
                select(TradeSignal).where(
                    TradeSignal.idempotency_key == idempotency_key
                )
            )
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error("TradeSignal 検索エラー: %s", e)
            return None
