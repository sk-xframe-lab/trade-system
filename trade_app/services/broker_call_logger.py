"""
BrokerCallLogger サービス
ブローカーへの全 API 呼び出しをリクエスト/レスポンスとして DB に永続化する。

設計方針:
  - OrderRouter / OrderPoller / RecoveryManager が使用する
  - ブローカー呼び出しの前後にログを挿入するので BrokerAdapter をラップしない
  - 呼び出し側が「ログ取得 → ブローカー呼び出し → レスポンスログ記録」の順で使う
  - 障害時でも必ずログが残るよう、エラーもレスポンスとして記録する
"""
import dataclasses
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.models.broker_request import BrokerRequest
from trade_app.models.broker_response import BrokerResponse
from trade_app.brokers.base import CancelResult, OrderRequest, OrderStatusResponse, OrderResponse

logger = logging.getLogger(__name__)


class BrokerCallLogger:
    """
    ブローカー API 呼び出しのリクエスト/レスポンスを DB に記録するサービス。

    使用パターン:
        logger = BrokerCallLogger(db)
        br = await logger.before_place_order(order_id, request)
        try:
            resp = await broker.place_order(request)
            await logger.after_place_order(br, order_id, resp)
        except Exception as e:
            await logger.on_error(br, order_id, e)
            raise
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ─── 発注 ─────────────────────────────────────────────────────────────

    async def before_place_order(
        self, order_id: str, request: OrderRequest
    ) -> BrokerRequest:
        """place_order 呼び出し前にリクエストを記録する"""
        payload = {
            "client_order_id": request.client_order_id,
            "ticker": request.ticker,
            "order_type": request.order_type.value,
            "side": request.side.value,
            "quantity": request.quantity,
            "limit_price": request.limit_price,
        }
        return await self._log_request("place", order_id, payload)

    async def after_place_order(
        self,
        broker_request: BrokerRequest,
        order_id: str,
        response: OrderResponse,
    ) -> BrokerResponse:
        """place_order 成功レスポンスを記録する"""
        payload = {
            "broker_order_id": response.broker_order_id,
            "status": response.status.value,
            "message": response.message,
        }
        return await self._log_response(broker_request, order_id, "200", payload)

    # ─── 状態照会 ─────────────────────────────────────────────────────────

    async def before_status_query(
        self, order_id: str, broker_order_id: str
    ) -> BrokerRequest:
        """get_order_status 呼び出し前にリクエストを記録する"""
        return await self._log_request(
            "status_query", order_id, {"broker_order_id": broker_order_id}
        )

    async def after_status_query(
        self,
        broker_request: BrokerRequest,
        order_id: str,
        response: OrderStatusResponse,
    ) -> BrokerResponse:
        """get_order_status 成功レスポンスを記録する"""
        payload = {
            "broker_order_id": response.broker_order_id,
            "status": response.status.value,
            "filled_quantity": response.filled_quantity,
            "filled_price": response.filled_price,
            "message": response.message,
        }
        return await self._log_response(broker_request, order_id, "200", payload)

    # ─── キャンセル ────────────────────────────────────────────────────────

    async def before_cancel(
        self, order_id: str, broker_order_id: str
    ) -> BrokerRequest:
        """cancel_order 呼び出し前にリクエストを記録する"""
        return await self._log_request(
            "cancel", order_id, {"broker_order_id": broker_order_id}
        )

    async def after_cancel(
        self,
        broker_request: BrokerRequest,
        order_id: str,
        result: CancelResult,
    ) -> BrokerResponse:
        """cancel_order レスポンスを記録する"""
        return await self._log_response(
            broker_request,
            order_id,
            "200",
            {
                "success": result.success,
                "is_already_terminal": result.is_already_terminal,
                "reason": result.reason,
            },
        )

    # ─── エラー記録 ────────────────────────────────────────────────────────

    async def on_error(
        self,
        broker_request: BrokerRequest,
        order_id: str | None,
        error: Exception,
    ) -> BrokerResponse:
        """ブローカー呼び出し時の例外をエラーレスポンスとして記録する"""
        return await self._log_response(
            broker_request,
            order_id,
            "error",
            payload={"error_type": type(error).__name__},
            is_error=True,
            error_message=str(error),
        )

    # ─── 内部ヘルパー ──────────────────────────────────────────────────────

    async def _log_request(
        self,
        request_type: str,
        order_id: str | None,
        payload: dict[str, Any],
    ) -> BrokerRequest:
        now = datetime.now(timezone.utc)
        br = BrokerRequest(
            order_id=order_id,
            request_type=request_type,
            payload=payload,
            sent_at=now,
            created_at=now,
        )
        self._db.add(br)
        await self._db.flush()
        return br

    async def _log_response(
        self,
        broker_request: BrokerRequest,
        order_id: str | None,
        status_code: str,
        payload: dict[str, Any],
        is_error: bool = False,
        error_message: str | None = None,
    ) -> BrokerResponse:
        now = datetime.now(timezone.utc)
        resp = BrokerResponse(
            broker_request_id=broker_request.id,
            order_id=order_id,
            status_code=status_code,
            payload=payload,
            is_error=is_error,
            error_message=error_message,
            received_at=now,
            created_at=now,
        )
        self._db.add(resp)
        await self._db.flush()
        return resp
