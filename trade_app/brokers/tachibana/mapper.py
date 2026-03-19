"""
立花証券 e_api ↔ 内部データクラス 変換

責務:
  - OrderRequest → CLMKabuNewOrder リクエスト dict
  - CLMKabuNewOrder レスポンス → OrderResponse
  - CLMOrderListDetail レスポンス → OrderStatusResponse
  - CLMOrderListDetail の約定明細 → ExecutionDetail（内部表現）
  - ステータスコードマッピング（仕様書確認済み）
  - composite broker_order_id のエンコード / デコード
  - execution_key 生成（約定固有 ID が存在しない代替）
  - 残高 / 建玉の変換

設計制約:
  Tachibana 固有フィールドはこの mapper 層に閉じ込める。
  pipeline / risk / planning には漏らさないこと。
"""
import logging
from dataclasses import dataclass
from typing import Any, List, Optional

from trade_app.brokers.base import (
    BalanceInfo,
    BrokerPosition,
    OrderRequest,
    OrderResponse,
    OrderStatusResponse,
)
from trade_app.models.enums import OrderStatus, Side

logger = logging.getLogger(__name__)


# ─── composite broker_order_id ───────────────────────────────────────────────
#
# 立花証券の注文番号 (sOrderNumber) は営業日単位でのみ一意。
# 真の注文識別子として (sEigyouDay, sOrderNumber) のペアが必要。
# 内部では "{sEigyouDay}_{sOrderNumber}" の複合文字列として扱う。
#
# 確認済み: place_order (CLMKabuNewOrder) レスポンスで sEigyouDay / sOrderNumber の
# 両フィールドが実在することを確認済み（Phase 10-C place_order 実装・テスト）。
#
# 例: "20260316_00123"

_BROKER_ORDER_ID_SEP = "_"


def encode_broker_order_id(eigyou_day: str, order_number: str) -> str:
    """
    sEigyouDay と sOrderNumber から composite broker_order_id を生成する。

    例: encode_broker_order_id("20260316", "00123") → "20260316_00123"
    """
    return f"{eigyou_day}{_BROKER_ORDER_ID_SEP}{order_number}"


def decode_broker_order_id(broker_order_id: str) -> tuple[str, str]:
    """
    composite broker_order_id を (sEigyouDay, sOrderNumber) に分解する。

    例: decode_broker_order_id("20260316_00123") → ("20260316", "00123")

    Raises:
        ValueError: 形式が不正な場合（セパレータなし、いずれかが空等）
    """
    parts = broker_order_id.split(_BROKER_ORDER_ID_SEP, 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(
            f"Invalid broker_order_id: {broker_order_id!r}. "
            f"Expected '{{sEigyouDay}}{_BROKER_ORDER_ID_SEP}{{sOrderNumber}}'"
        )
    return parts[0], parts[1]


# ─── execution_key 生成 ───────────────────────────────────────────────────────
#
# 【設計確定】立花証券 e_api には約定固有 ID (broker_execution_id) が存在しない。
# これは「仕様書未確認」ではなく「ID がない前提での設計判断」として確定する。
# （仕様書確認でブローカー側に約定 ID フィールドが見つかった場合は
#  make_execution_key を廃止して sYakuzyouID 等に置き換えること）
#
# 代替として (sEigyouDay, sOrderNumber, sYakuzyouDate, qty) の複合キーを
# execution_key として生成し、OrderPoller の broker_execution_id に充てる。
#
# 一意性: 同一注文で同一時刻・同一数量の複数約定は実運用上まず発生しない。
# hhmmss 精度で 1 秒以内の同一数量重複が発生した場合は execution_key が衝突するが、
# Execution テーブルの UNIQUE 制約により重複 INSERT がブロックされるため安全側に倒る。
#
# 仕様書確認済み: sYakuzyouDate フィールド名（CLMOrderListDetail aYakuzyouSikkouList）


def make_execution_key(
    eigyou_day: str,
    order_number: str,
    yakuzyou_time: str,
    qty: int,
) -> str:
    """
    約定固有 ID の代替として安定した execution_key を生成する。

    Args:
        eigyou_day:    営業日 (sEigyouDay)  例: "20260316"
        order_number:  注文番号 (sOrderNumber) 例: "00123"
        yakuzyou_time: 約定時刻 hhmmss 形式 (sYakuzyouDate) 例: "153045"
        qty:           約定数量

    Returns:
        "{sEigyouDay}_{sOrderNumber}_{yakuzyou_time}_{qty}"
        例: "20260316_00123_153045_100"
    """
    return f"{eigyou_day}_{order_number}_{yakuzyou_time}_{qty}"


# ─── ステータスコードマッピング ────────────────────────────────────────────────
#
# 仕様書確認済み: CLMOrderListDetail の sOrderStatusCode 一覧
#
# sOrderStatusCode | 意味              | 内部ステータス
# -----------------|-------------------|----------------
# "0"              | 受付未済          | PENDING
# "1"              | 未約定            | SUBMITTED
# "2"              | 受付エラー        | REJECTED
# "3"              | 訂正中            | SUBMITTED  （注文はまだ有効）
# "4"              | 訂正完了          | SUBMITTED  （注文はまだ有効）
# "5"              | 訂正失敗          | SUBMITTED  （暫定: 注文は有効と推定）
# "6"              | 取消中            | SUBMITTED  （取消完了まで注文は有効）
# "7"              | 取消完了          | CANCELLED
# "8"              | 取消失敗          | SUBMITTED  （暫定: 注文は有効と推定）
# "9"              | 一部約定          | PARTIAL
# "10"             | 全部約定          | FILLED
#
# 暫定項目 ("5", "8"):
#   「訂正失敗」「取消失敗」時に元注文が有効かどうかは仕様書本文で要確認。
#   現時点では「有効（SUBMITTED）」として安全側に倒す。
#   TODO: 本番投入前に "5"/"8" の挙動を仕様書で確認すること。
#
# 【安全設計】: 未知コードは UNKNOWN にフォールバックする。

_ORDER_STATUS_MAP: dict[str, OrderStatus] = {
    "0":  OrderStatus.PENDING,
    "1":  OrderStatus.SUBMITTED,
    "2":  OrderStatus.REJECTED,
    "3":  OrderStatus.SUBMITTED,   # 訂正中（注文はまだ有効）
    "4":  OrderStatus.SUBMITTED,   # 訂正完了（注文はまだ有効）
    "5":  OrderStatus.SUBMITTED,   # 訂正失敗（暫定: 元注文は有効と推定）
    "6":  OrderStatus.SUBMITTED,   # 取消中（取消完了まで注文は有効）
    "7":  OrderStatus.CANCELLED,   # 取消完了
    "8":  OrderStatus.SUBMITTED,   # 取消失敗（暫定: 元注文は有効と推定）
    "9":  OrderStatus.PARTIAL,
    "10": OrderStatus.FILLED,
}


def _map_status_code(code: str) -> OrderStatus:
    """立花証券 sOrderStatusCode を内部 OrderStatus に変換する。未知コードは UNKNOWN。"""
    status = _ORDER_STATUS_MAP.get(code)
    if status is None:
        logger.warning("未知の sOrderStatusCode: %r → UNKNOWN として扱います", code)
        return OrderStatus.UNKNOWN
    return status


# ─── 売買区分マッピング ──────────────────────────────────────────────────────
#
# 仕様書確認済み:
#   sBaibaiKubun: 方向のみを示す（現物/信用の区別は sGenkinShinyouKubun で行う）
#     "1" = 売
#     "3" = 買
#     "5" = 現渡
#     "7" = 現引
#
#   sGenkinShinyouKubun: 現物/信用の区別
#     "0" = 現物
#     "2" = 新規制度6ヶ月（信用新規: 暫定 — 制度/一般・期間は仕様書で要確認）
#     "4" = 返済制度6ヶ月
#     "6" = 新規一般6ヶ月
#     "8" = 返済一般6ヶ月
#
# 注文マッピング: (side, account_type) → (sBaibaiKubun, sGenkinShinyouKubun)
_SIDE_ACCOUNT_TO_BAIBAI_GENKIN: dict[tuple[str, str], tuple[str, str]] = {
    ("buy",  "cash"):   ("3", "0"),   # 現物買
    ("sell", "cash"):   ("1", "0"),   # 現物売
    ("buy",  "margin"): ("3", "2"),   # 信用新規買（暫定: 制度6ヶ月）
    ("sell", "margin"): ("1", "2"),   # 信用新規売（暫定: 制度6ヶ月）
}

# 逆引き: sBaibaiKubun → Side（方向のみ）
_BAIBAI_TO_SIDE: dict[str, Side] = {
    "1": Side.SELL,
    "3": Side.BUY,
    "5": Side.SELL,   # 現渡
    "7": Side.BUY,    # 現引
}

# OrderType → sCondition
# sCondition | 意味
# -----------|---------
# "0"        | 指値
# "2"        | 成行
_ORDER_TYPE_TO_CONDITION: dict[str, str] = {
    "market": "2",
    "limit":  "0",
}


# ─── 注文変換 ─────────────────────────────────────────────────────────────────

def map_new_order_request(
    request: OrderRequest,
    second_password: str,
    tax_type: str = "3",
    market_code: str = "00",
) -> dict[str, str]:
    """
    OrderRequest → CLMKabuNewOrder リクエスト dict に変換する。

    返す dict の全値は str 型（e_api の form-encoded リクエストに合わせる）。

    Args:
        request:         内部 OrderRequest
        second_password: 第二パスワード (sSecondPassword)。注文に必須。
        tax_type:        譲渡益課税区分 ("1"=一般, "2"=特定, "3"=NISA) → sZyoutoekiKazeiC
        market_code:     市場コード ("00"=東証) → sSizyouC

    Returns:
        e_api に POST する dict（全値 str 型）

    Raises:
        ValueError: 対応していない side / account_type の組み合わせ
    """
    side_str = request.side.value if hasattr(request.side, "value") else str(request.side)
    order_type_str = (
        request.order_type.value
        if hasattr(request.order_type, "value")
        else str(request.order_type)
    )

    mapping = _SIDE_ACCOUNT_TO_BAIBAI_GENKIN.get((side_str, request.account_type))
    if mapping is None:
        raise ValueError(
            f"Unsupported combination: side={side_str!r}, "
            f"account_type={request.account_type!r}"
        )
    baibai_kubun, genkin_shinyou_kubun = mapping

    condition = _ORDER_TYPE_TO_CONDITION.get(order_type_str, "2")

    # 成行は price="0"、指値は実際の価格を文字列化
    if order_type_str == "limit" and request.limit_price is not None:
        price_str = str(request.limit_price)
    else:
        price_str = "0"

    return {
        "sCLMID":               "CLMKabuNewOrder",
        "sIssueCode":           request.ticker,
        "sSizyouC":             market_code,          # 仕様書確認済み（旧: sSizyouCode）
        "sBaibaiKubun":         baibai_kubun,          # 方向のみ: 1=売, 3=買
        "sGenkinShinyouKubun":  genkin_shinyou_kubun,  # 仕様書確認済み
        "sOrderSuryou":         str(request.quantity),
        "sCondition":           condition,
        "sOrderPrice":          price_str,
        "sSecondPassword":      second_password,
        "sZyoutoekiKazeiC":     tax_type,             # 仕様書確認済み（旧: sTaxType）
    }


def map_order_response(raw: dict[str, Any]) -> OrderResponse:
    """
    CLMKabuNewOrder レスポンス → OrderResponse に変換する。

    broker_order_id は "{sEigyouDay}_{sOrderNumber}" の複合 ID とする。
    """
    eigyou_day   = raw.get("sEigyouDay",   "")
    order_number = raw.get("sOrderNumber", "")
    broker_order_id = encode_broker_order_id(eigyou_day, order_number)

    return OrderResponse(
        broker_order_id=broker_order_id,
        status=OrderStatus.SUBMITTED,
        message=raw.get("sResultText", ""),
    )


# ─── 注文取消リクエスト変換 ────────────────────────────────────────────────────

def map_cancel_request(
    eigyou_day: str,
    order_number: str,
    second_password: str,
) -> dict[str, str]:
    """
    (sEigyouDay, sOrderNumber) から取消リクエスト dict を生成する。

    取消モデル:
      立花証券 e_api の取消は非同期モデル。
      正常応答（sResultCode=0）は「取消受付済み」であり「取消完了」ではない。
      取消完了は別途 get_order_status で sOrderStatusCode=7 を確認すること。

    仕様未確定 NOTE（本番投入前に仕様書で確認すること）:
      - NOTE: sCLMID "CLMKabuCancelOrder" は推定。
        CLMKabuNewOrder（発注）の命名規則から "NewOrder" → "CancelOrder" と推定。
        TODO: 仕様書の取消 API sCLMID を確認すること。
      - NOTE: sSecondPassword の要否は仕様書未確認。
        発注（CLMKabuNewOrder）では必須フィールドだったため取消も必須と推定。
        TODO: 仕様書の取消 API フィールド定義を確認すること。

    Args:
        eigyou_day:      営業日 (sEigyouDay)
        order_number:    注文番号 (sOrderNumber)
        second_password: 第二パスワード (sSecondPassword)

    Returns:
        e_api に POST する dict（全値 str 型）
    """
    return {
        "sCLMID":          "CLMKabuCancelOrder",   # TODO: 仕様書確認
        "sEigyouDay":      eigyou_day,
        "sOrderNumber":    order_number,
        "sSecondPassword": second_password,         # TODO: 要否を仕様書確認
    }


# ─── 注文照会変換 ──────────────────────────────────────────────────────────────

@dataclass
class ExecutionDetail:
    """
    約定明細の内部表現。CLMOrderListDetail の aYakuzyouSikkouList 1要素から変換する。

    立花証券 e_api には約定固有 ID がないため execution_key で識別する。
    この execution_key を OrderPoller の broker_execution_id として使い
    Execution レコードの重複防止に充てる。
    """
    execution_key:  str    # make_execution_key() で生成
    eigyou_day:     str
    order_number:   str
    qty:            int
    price:          float
    yakuzyou_time:  str    # sYakuzyouDate フィールド（hhmmss 形式）


def map_order_status(raw: dict[str, Any]) -> OrderStatusResponse:
    """
    CLMOrderListDetail の1注文レコード → OrderStatusResponse に変換する。

    filled_price は sYakuzyouKingaku / sYakuzyouSuryou の加重平均で算出する。
    aYakuzyouSikkouList（約定明細リスト）が存在する場合、配列末尾の約定から
    execution_key を生成して broker_execution_id として返す。
    aYakuzyouSikkouList が空・欠損の場合は broker_execution_id=None で安全にフォールバック。

    仕様書確認済みフィールド:
      sOrderStatusCode  : 注文状態コード（旧: sState）
      aYakuzyouSikkouList: 約定明細リスト（旧: sYakuzyouList）
      sYakuzyouDate     : 約定日時（旧: sYakuzyouTime）

    Args:
        raw: 1注文分の dict（sOrderNumber / sEigyouDay / sOrderStatusCode 等を含む）
    """
    eigyou_day   = raw.get("sEigyouDay",   "")
    order_number = raw.get("sOrderNumber", "")
    broker_order_id = encode_broker_order_id(eigyou_day, order_number)

    state_code = str(raw.get("sOrderStatusCode", ""))
    status = _map_status_code(state_code)

    order_qty  = _to_int(raw.get("sOrderSuryou",    "0"))
    filled_qty = _to_int(raw.get("sYakuzyouSuryou", "0"))
    cancel_qty = _to_int(raw.get("sCancelSuryou",   "0"))
    remaining  = max(0, order_qty - filled_qty - cancel_qty)

    # 加重平均約定価格: sYakuzyouKingaku（約定金額合計）/ sYakuzyouSuryou
    yakuzyou_kingaku = _to_float(raw.get("sYakuzyouKingaku", "0"))
    filled_price: Optional[float] = None
    if filled_qty > 0 and yakuzyou_kingaku > 0:
        filled_price = yakuzyou_kingaku / filled_qty

    # 最新約定の execution_key（重複防止用 broker_execution_id として使用）
    # 配列末尾 [-1] を「最新約定」とみなしているが、順序（FIFO/LIFO）は仕様書未確認。
    # NOTE: sYakuzyouDate は hhmmss 文字列を推定。欠損時は "000000" にフォールバック。
    execution_key: Optional[str] = None
    details_raw = raw.get("aYakuzyouSikkouList", [])
    if isinstance(details_raw, list) and details_raw:
        last = details_raw[-1]
        if isinstance(last, dict):
            execution_key = make_execution_key(
                eigyou_day,
                order_number,
                str(last.get("sYakuzyouDate", "000000")),
                _to_int(last.get("sYakuzyouSuryou", "0")),
            )

    return OrderStatusResponse(
        broker_order_id=broker_order_id,
        status=status,
        filled_quantity=filled_qty,
        filled_price=filled_price,
        remaining_qty=remaining,
        broker_execution_id=execution_key,
        cancel_qty=cancel_qty,
        message=raw.get("sResultText", ""),
    )


def map_order_list_detail(
    raw: dict[str, Any],
    eigyou_day: str,
    order_number: str,
) -> ExecutionDetail:
    """
    CLMOrderListDetail の約定明細行 → ExecutionDetail に変換する。

    仕様書確認済みフィールド:
      sYakuzyouSuryou: 約定数量
      sYakuzyouPrice:  約定価格
      sYakuzyouDate:   約定日時（旧: sYakuzyouTime）

    Args:
        raw:          約定明細 dict（aYakuzyouSikkouList の1要素）
        eigyou_day:   親注文の sEigyouDay
        order_number: 親注文の sOrderNumber
    """
    qty           = _to_int(raw.get("sYakuzyouSuryou", "0"))
    price         = _to_float(raw.get("sYakuzyouPrice", "0"))
    yakuzyou_time = str(raw.get("sYakuzyouDate", "000000"))
    key = make_execution_key(eigyou_day, order_number, yakuzyou_time, qty)

    return ExecutionDetail(
        execution_key=key,
        eigyou_day=eigyou_day,
        order_number=order_number,
        qty=qty,
        price=price,
        yakuzyou_time=yakuzyou_time,
    )


# ─── 残高変換 ─────────────────────────────────────────────────────────────────
#
# 2 API モデル:
#   (A) CLMZanKaiKanougaku: 現物余力 → sSummaryGenkabuKaituke = 現物買付可能額
#   (B) CLMZanShinkiKanoIjiritu: 信用余力 → sSummarySinyouSinkidate = 信用新規建余力
#
# (A) が失敗した場合は例外を伝播する（現物余力なしは許容しない）。
# (B) が失敗した場合は margin_available=0 にデグレードする（信用口座なしの場合を考慮）。
# adapter.get_balance() がこの2 API 呼び出しとデグレード処理を実装している。
#
# 仕様書確認済みフィールド:
#   sSummaryGenkabuKaituke  : 現物買付可能額（CLMZanKaiKanougaku レスポンス）
#   sSummarySinyouSinkidate : 信用新規建余力（CLMZanShinkiKanoIjiritu レスポンス）
#
# 暫定: total_equity の算出元フィールドは仕様書未確認。現状は 0.0 を返す。
# TODO: 仕様書で純資産・総資産相当フィールドを確認すること。


def map_cash_balance(raw: dict[str, Any]) -> float:
    """
    CLMZanKaiKanougaku レスポンス → 現物買付可能額（円）。

    仕様書確認済み: sSummaryGenkabuKaituke = 現物買付可能額
    """
    return _to_float(raw.get("sSummaryGenkabuKaituke", "0"))


def map_margin_available(raw: dict[str, Any]) -> float:
    """
    CLMZanShinkiKanoIjiritu レスポンス → 信用新規建余力（円）。

    仕様書確認済み: sSummarySinyouSinkidate = 信用新規建余力
    """
    return _to_float(raw.get("sSummarySinyouSinkidate", "0"))


def map_balance(raw_cash: dict[str, Any], raw_margin: Optional[dict[str, Any]] = None) -> BalanceInfo:
    """
    現物余力 + 信用余力レスポンス → BalanceInfo に変換する。

    Args:
        raw_cash:   CLMZanKaiKanougaku レスポンス（必須）
        raw_margin: CLMZanShinkiKanoIjiritu レスポンス（None の場合 margin_available=0）

    Returns:
        BalanceInfo

    暫定: total_equity は仕様書未確認のため 0.0 を返す。
    TODO: 仕様書で純資産相当フィールドを確認して実装すること。
    """
    cash   = map_cash_balance(raw_cash)
    margin = map_margin_available(raw_margin) if raw_margin is not None else 0.0
    # 暫定: total_equity の算出元フィールド未確認
    equity = 0.0   # TODO: 仕様書確認

    return BalanceInfo(
        cash_balance=cash,
        margin_available=margin,
        total_equity=equity,
    )


# ─── 建玉変換 ─────────────────────────────────────────────────────────────────
#
# 現物保有: CLMGenbutuKabuList
#   リストキー: aGenbutuKabuList
#   フィールド: sUriOrderIssueCode（銘柄コード）, sUriOrderZanKabuSuryou（残数量）,
#              sUriOrderGaisanBokaTanka（概算簿価単価）
#
# 信用建玉: CLMShinyouTategyokuList
#   リストキー: aShinyouTategyokuList
#   フィールド: sOrderIssueCode（銘柄コード）, sBaibaiKubun（売買区分: 1=売, 3=買）,
#              sOrderTategyokuSuryou（建玉数量）, sOrderTategyokuTanka（建値）
#
# 仕様書確認済みフィールド名（旧推定値から修正済み）


def map_spot_positions(items: List[dict[str, Any]]) -> List[BrokerPosition]:
    """
    CLMGenbutuKabuList の aGenbutuKabuList 各行 → BrokerPosition リスト。

    現物保有には注文番号が存在しないため broker_order_id は
    "spot_{sUriOrderIssueCode}" 形式の合成キーとする。

    sUriOrderIssueCode が空の行はスキップする（ヘッダー行・集計行の可能性）。

    仕様書確認済みフィールド:
      sUriOrderIssueCode     : 銘柄コード
      sUriOrderZanKabuSuryou : 残数量
      sUriOrderGaisanBokaTanka: 概算簿価単価
    """
    result: List[BrokerPosition] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ticker = item.get("sUriOrderIssueCode", "")
        if not ticker:
            continue
        qty       = _to_int(item.get("sUriOrderZanKabuSuryou",    "0"))
        avg_price = _to_float(item.get("sUriOrderGaisanBokaTanka", "0"))
        result.append(BrokerPosition(
            broker_order_id=f"spot_{ticker}",   # 現物は注文 ID なし → 合成キー
            ticker=ticker,
            side=Side.BUY,    # 現物保有は常に買い方向
            quantity=qty,
            average_price=avg_price,
        ))
    return result


def parse_spot_positions_response(raw: dict[str, Any]) -> List[BrokerPosition]:
    """
    CLMGenbutuKabuList レスポンス全体 → BrokerPosition リスト。

    仕様書確認済みリストキー: aGenbutuKabuList
    """
    items = raw.get("aGenbutuKabuList", [])
    if not isinstance(items, list):
        return []
    return map_spot_positions(items)


def map_margin_positions(items: List[dict[str, Any]]) -> List[BrokerPosition]:
    """
    CLMShinyouTategyokuList の aShinyouTategyokuList 各行 → BrokerPosition リスト。

    仕様書確認済みフィールド:
      sOrderIssueCode       : 銘柄コード
      sBaibaiKubun          : 売買区分（1=売, 3=買）
      sOrderTategyokuSuryou : 建玉数量
      sOrderTategyokuTanka  : 建値
    """
    result: List[BrokerPosition] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ticker = item.get("sOrderIssueCode", "")
        if not ticker:
            continue
        baibai_kubun = item.get("sBaibaiKubun", "3")
        side = _BAIBAI_TO_SIDE.get(baibai_kubun, Side.BUY)
        qty       = _to_int(item.get("sOrderTategyokuSuryou", "0"))
        avg_price = _to_float(item.get("sOrderTategyokuTanka", "0"))
        result.append(BrokerPosition(
            broker_order_id=f"margin_{ticker}_{baibai_kubun}",  # 建玉は注文 ID なし → 合成キー
            ticker=ticker,
            side=side,
            quantity=qty,
            average_price=avg_price,
        ))
    return result


def parse_margin_positions_response(raw: dict[str, Any]) -> List[BrokerPosition]:
    """
    CLMShinyouTategyokuList レスポンス全体 → BrokerPosition リスト。

    仕様書確認済みリストキー: aShinyouTategyokuList
    """
    items = raw.get("aShinyouTategyokuList", [])
    if not isinstance(items, list):
        return []
    return map_margin_positions(items)


# ─── 後方互換エイリアス ────────────────────────────────────────────────────────
# map_positions は旧 test_tachibana_phase10c.py の TestMapPositions で使用されている。
# 信用建玉変換 (map_margin_positions) の alias として残す。

def map_positions(raw: List[dict[str, Any]]) -> List[BrokerPosition]:
    """後方互換エイリアス。新規コードは map_margin_positions を使用すること。"""
    return map_margin_positions(raw)


# ─── 現在価格変換 ─────────────────────────────────────────────────────────────
#
# 価格照会 API: CLMMfdsGetMarketPrice (sUrlPrice)
# リクエスト:
#   sTargetIssueCode: 銘柄コード
#   sTargetColumn:    取得フィールド指定 (暫定: "pDPP" = 現在値)
#                     TODO: 仕様書で sTargetColumn の選択肢を確認すること。
# レスポンス:
#   aCLMMfdsMarketPrice: 価格データ配列（1要素目を使用）
#   各要素の価格フィールド: pDPP（現在値 暫定）
#
# フォールバック:
#   pDPP が 0 / 空文字 / 欠損 → None（価格取得不能な正常系）
#   ExitWatcher は None を受け取ると TP/SL をスキップ（TimeStop のみ発火）
#
# 暫定項目:
#   sTargetColumn = "pDPP" は推定値。仕様書で正式フィールド名を確認すること。
#   aCLMMfdsMarketPrice の構造（配列か / 1要素か）は仕様書で確認すること。


def map_market_price_from_entry(entry: dict[str, Any]) -> Optional[float]:
    """
    aCLMMfdsMarketPrice の1要素 → 現在価格 (Optional[float])。

    pDPP が正の値なら float で返す。0 以下・空・欠損なら None を返す。

    暫定: フィールド名 "pDPP" は仕様書未確認。
    TODO: 仕様書の CLMMfdsGetMarketPrice レスポンス定義を確認すること。
    """
    price = _to_float(entry.get("pDPP", ""))
    if price > 0.0:
        return price
    return None


def map_market_price(raw: dict[str, Any]) -> Optional[float]:
    """
    CLMMfdsGetMarketPrice レスポンス → 現在価格 (Optional[float])。

    価格が取得できない正常系（取引時間外・データなし等）は None を返す。
    ExitWatcher は None を受け取った場合 TP/SL をスキップする（TimeStop は発火する）。

    aCLMMfdsMarketPrice 配列の先頭要素の pDPP を使用する。
    配列が空 / 欠損 / pDPP が 0 以下の場合は None を返す。

    暫定: aCLMMfdsMarketPrice の構造・pDPP フィールド名は仕様書未確認。
    TODO: 仕様書の CLMMfdsGetMarketPrice レスポンス定義を確認すること。
    """
    entries = raw.get("aCLMMfdsMarketPrice", [])
    if not isinstance(entries, list) or not entries:
        return None
    first = entries[0]
    if not isinstance(first, dict):
        return None
    return map_market_price_from_entry(first)


# ─── 数値変換ユーティリティ ────────────────────────────────────────────────────

def _to_int(value: Any) -> int:
    """文字列（カンマ区切り可）を int に変換する。失敗時は 0 を返す。"""
    try:
        return int(str(value).replace(",", ""))
    except (ValueError, TypeError):
        return 0


def _to_float(value: Any) -> float:
    """文字列（カンマ区切り可）を float に変換する。失敗時は 0.0 を返す。"""
    try:
        return float(str(value).replace(",", ""))
    except (ValueError, TypeError):
        return 0.0
