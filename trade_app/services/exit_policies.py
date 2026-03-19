"""
Exit ポリシー実装

ExitWatcher が各ポジションに対して「今クローズすべきか」を判定するためのポリシー群。

設計原則:
  - 各ポリシーは Position と現在価格を受け取り bool を返す純粋関数に近い設計
  - BrokerAdapter や DB に依存しないため、将来差し替えても壊れない
  - 複数ポリシーを AND / OR で組み合わせ可能

ポリシー一覧:
  TakeProfitPolicy  : TP 価格到達でクローズ
  StopLossPolicy    : SL 価格到達でクローズ
  TimeStopPolicy    : 時間切れ（exit_deadline 超過）でクローズ
"""
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional

from trade_app.models.enums import ExitReason
from trade_app.models.position import Position

logger = logging.getLogger(__name__)


class ExitPolicy(ABC):
    """
    ポジション出口判定の抽象基底クラス。

    ExitWatcher は登録されたポリシーを順番に評価し、
    いずれかが True を返した時点でそのポリシーの exit_reason でクローズを開始する。
    """

    @abstractmethod
    def should_exit(
        self,
        position: Position,
        current_price: Optional[float],
    ) -> bool:
        """
        ポジションを今クローズすべきかを判定する。

        Args:
            position     : 対象ポジション（status=OPEN であることが前提）
            current_price: 現在の市場価格（取得できない場合は None）

        Returns:
            True の場合、exit_reason() で返す理由でクローズを開始する
        """
        ...

    @property
    @abstractmethod
    def exit_reason(self) -> ExitReason:
        """このポリシーが発動した場合のクローズ理由"""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """ポリシー名（ログ用）"""
        ...


class TakeProfitPolicy(ExitPolicy):
    """
    Take Profit（利確）ポリシー。

    ポジションの tp_price が設定されており、現在価格がそれを超えた場合にクローズする。

    方向性:
      BUY  ポジション: current_price >= tp_price → 利確
      SELL ポジション: current_price <= tp_price → 利確
    """

    @property
    def exit_reason(self) -> ExitReason:
        return ExitReason.TP_HIT

    @property
    def name(self) -> str:
        return "TakeProfit"

    def should_exit(self, position: Position, current_price: Optional[float]) -> bool:
        if position.tp_price is None:
            return False
        if current_price is None:
            return False

        if position.side == "buy":
            triggered = current_price >= position.tp_price
        else:
            triggered = current_price <= position.tp_price

        if triggered:
            logger.info(
                "[%s] TP 到達: pos=%s ticker=%s side=%s current=%.0f tp=%.0f",
                self.name, position.id[:8], position.ticker,
                position.side, current_price, position.tp_price,
            )
        return triggered


class StopLossPolicy(ExitPolicy):
    """
    Stop Loss（損切）ポリシー。

    ポジションの sl_price が設定されており、現在価格がそれを下回った（売りは超えた）場合にクローズする。

    方向性:
      BUY  ポジション: current_price <= sl_price → 損切
      SELL ポジション: current_price >= sl_price → 損切
    """

    @property
    def exit_reason(self) -> ExitReason:
        return ExitReason.SL_HIT

    @property
    def name(self) -> str:
        return "StopLoss"

    def should_exit(self, position: Position, current_price: Optional[float]) -> bool:
        if position.sl_price is None:
            return False
        if current_price is None:
            return False

        if position.side == "buy":
            triggered = current_price <= position.sl_price
        else:
            triggered = current_price >= position.sl_price

        if triggered:
            logger.warning(
                "[%s] SL 発動: pos=%s ticker=%s side=%s current=%.0f sl=%.0f",
                self.name, position.id[:8], position.ticker,
                position.side, current_price, position.sl_price,
            )
        return triggered


class TimeStopPolicy(ExitPolicy):
    """
    Time Stop（時間切れ）ポリシー。

    ポジションの exit_deadline が過去になった場合に強制クローズする。
    現在価格の有無に関わらず発動する（価格取得不可でも時間切れは有効）。

    exit_deadline は通常、大引け前（例: 14:50）に設定される。
    """

    @property
    def exit_reason(self) -> ExitReason:
        return ExitReason.TIMEOUT

    @property
    def name(self) -> str:
        return "TimeStop"

    def should_exit(self, position: Position, current_price: Optional[float]) -> bool:
        if position.exit_deadline is None:
            return False

        deadline = position.exit_deadline
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        triggered = now >= deadline

        if triggered:
            elapsed = (now - deadline).total_seconds()
            logger.warning(
                "[%s] 時間切れ: pos=%s ticker=%s deadline=%s elapsed=%.0fs",
                self.name, position.id[:8], position.ticker,
                deadline.isoformat(), elapsed,
            )
        return triggered


# ─── デフォルトポリシーセット ──────────────────────────────────────────────────

DEFAULT_EXIT_POLICIES: list[ExitPolicy] = [
    TakeProfitPolicy(),
    StopLossPolicy(),
    TimeStopPolicy(),
]
"""
ExitWatcher が使用するデフォルトポリシーリスト。
評価順序: TP → SL → TimeStop
（TimeStop は価格不要なので最後に評価する）
"""
