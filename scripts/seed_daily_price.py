#!/usr/bin/env python3
"""
seed_daily_price.py — J-Quants から日次 OHLCV データを初期充填するスクリプト

概要:
  WATCHED_SYMBOLS に設定された銘柄について、J-Quants API から過去 N 取引日分の
  日次 OHLCV データを取得し、daily_price_history テーブルに upsert する。

  MA20 の計算には 20 取引日が必要なため、デフォルトで 30 取引日分を取得する。

使用方法:
  # 環境変数を設定してから実行
  export DATABASE_URL="postgresql+asyncpg://trade:trade_secret@localhost:5432/trade_db"
  export JQUANTS_EMAIL="your@email.com"
  export JQUANTS_PASSWORD="yourpassword"
  export WATCHED_SYMBOLS="7203,6758"   # カンマ区切り（config.py の WATCHED_SYMBOLS と同じ値）

  python scripts/seed_daily_price.py [--days 30] [--dry-run]

J-Quants コード形式:
  J-Quants API は5桁コードを使用する（例: "72030" for TSE Prime 7203）。
  本スクリプトは 4桁銘柄コード + "0" を自動付与する（TSE Prime 前提）。
  他市場（TSE Standard: "1"、TSE Growth: "4" 等）の場合は --market-suffix を指定する。

  例: 6758（ソニー）→ "67580"

初期充填順序:
  1. alembic upgrade head（migration 014 適用済みであること）
  2. このスクリプトを実行（過去 30 日分を upsert）
  3. MarketStateRunner を起動（daily metrics が ma5/ma20/atr/rsi として注入される）

注意:
  - J-Quants 無料プランは1日1回更新・過去12週分のデータを提供する
  - 既存データは CONFLICT（ticker, trading_date）が発生した場合、スキップする（上書きしない）
  - dry-run モードでは DB への書き込みを行わない（取得件数のみ表示）
"""
import argparse
import asyncio
import logging
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("seed_daily_price")

# ─── J-Quants API ─────────────────────────────────────────────────────────────

_JQUANTS_BASE = "https://api.jpx-jquants.com/v1"


async def _get_refresh_token(client: httpx.AsyncClient, email: str, password: str) -> str:
    resp = await client.post(
        f"{_JQUANTS_BASE}/token/auth_user",
        json={"mailaddress": email, "password": password},
    )
    resp.raise_for_status()
    data = resp.json()
    token = data.get("refreshToken")
    if not token:
        raise RuntimeError(f"refreshToken not found in response: {data}")
    return token


async def _get_id_token(client: httpx.AsyncClient, refresh_token: str) -> str:
    resp = await client.post(
        f"{_JQUANTS_BASE}/token/auth_refresh",
        params={"refreshtoken": refresh_token},
    )
    resp.raise_for_status()
    data = resp.json()
    token = data.get("idToken")
    if not token:
        raise RuntimeError(f"idToken not found in response: {data}")
    return token


async def _fetch_daily_quotes(
    client: httpx.AsyncClient,
    id_token: str,
    jquants_code: str,
    from_date: date,
    to_date: date,
) -> list[dict]:
    """
    J-Quants 日次株価 API からデータを取得する。

    返却形式（各要素）:
      {"Code": "72030", "Date": "20240104",
       "Open": 3350.0, "High": 3390.0, "Low": 3330.0,
       "Close": 3380.0, "Volume": 12345678.0, ...}

    Date フォーマットは "YYYYMMDD"（ハイフンなし）。
    """
    resp = await client.get(
        f"{_JQUANTS_BASE}/prices/daily_quotes",
        headers={"Authorization": f"Bearer {id_token}"},
        params={
            "code": jquants_code,
            "from": from_date.strftime("%Y%m%d"),
            "to": to_date.strftime("%Y%m%d"),
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json().get("daily_quotes", [])


# ─── DB Upsert ────────────────────────────────────────────────────────────────

async def _upsert_rows(
    session: AsyncSession,
    ticker: str,
    quotes: list[dict],
    dry_run: bool,
) -> int:
    """
    取得した quotes を daily_price_history に upsert する。
    CONFLICT（ticker, trading_date）の場合はスキップ（INSERT OR IGNORE 相当）。

    Returns: 新規挿入件数
    """
    inserted = 0
    for q in quotes:
        # J-Quants の Date は "YYYYMMDD" 形式
        raw_date = str(q.get("Date", ""))
        if len(raw_date) != 8:
            logger.warning("skip: unexpected Date format %r", raw_date)
            continue

        trading_date = date(int(raw_date[:4]), int(raw_date[4:6]), int(raw_date[6:8]))
        close_raw = q.get("Close")
        if close_raw is None:
            logger.debug("skip: Close is None for %s %s", ticker, trading_date)
            continue

        if dry_run:
            inserted += 1
            continue

        # SELECT → INSERT（重複は挿入しない）
        exist = await session.execute(
            text(
                "SELECT 1 FROM daily_price_history WHERE ticker = :t AND trading_date = :d"
            ),
            {"t": ticker, "d": trading_date},
        )
        if exist.fetchone():
            continue  # 既存行はスキップ

        await session.execute(
            text(
                """
                INSERT INTO daily_price_history
                  (id, ticker, trading_date, open, high, low, close, volume, source, created_at)
                VALUES
                  (:id, :ticker, :trading_date, :open, :high, :low, :close, :volume, :source, :now)
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "ticker": ticker,
                "trading_date": trading_date,
                "open": q.get("Open"),
                "high": q.get("High"),
                "low": q.get("Low"),
                "close": close_raw,
                "volume": int(q["Volume"]) if q.get("Volume") is not None else None,
                "source": "j_quants",
                "now": datetime.now(timezone.utc),
            },
        )
        inserted += 1

    if not dry_run:
        await session.commit()

    return inserted


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main(days: int, dry_run: bool, market_suffix: str) -> None:
    email = os.environ.get("JQUANTS_EMAIL", "")
    password = os.environ.get("JQUANTS_PASSWORD", "")
    db_url = os.environ.get("DATABASE_URL", "")
    watched_raw = os.environ.get("WATCHED_SYMBOLS", "")

    if not email or not password:
        logger.error("JQUANTS_EMAIL / JQUANTS_PASSWORD が未設定")
        sys.exit(1)
    if not db_url:
        logger.error("DATABASE_URL が未設定")
        sys.exit(1)

    tickers = [s.strip() for s in watched_raw.split(",") if s.strip()]
    if not tickers:
        logger.error("WATCHED_SYMBOLS が未設定（カンマ区切りで銘柄コードを指定してください）")
        sys.exit(1)

    to_date = date.today()
    # days 分 + 休日バッファ（取引日 N 日分を確保するため 1.5 倍のカレンダー日数）
    from_date = to_date - timedelta(days=int(days * 1.5))

    logger.info("対象銘柄: %s", tickers)
    logger.info("取得期間: %s 〜 %s (%d取引日目安)", from_date, to_date, days)
    if dry_run:
        logger.info("[DRY-RUN] DB への書き込みは行いません")

    engine = create_async_engine(db_url, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with httpx.AsyncClient() as client:
        logger.info("J-Quants 認証中...")
        refresh_token = await _get_refresh_token(client, email, password)
        id_token = await _get_id_token(client, refresh_token)
        logger.info("J-Quants 認証成功")

        for ticker in tickers:
            jquants_code = ticker + market_suffix
            logger.info("取得中: ticker=%s → J-Quants code=%s", ticker, jquants_code)

            try:
                quotes = await _fetch_daily_quotes(client, id_token, jquants_code, from_date, to_date)
            except httpx.HTTPStatusError as e:
                logger.error("HTTP エラー: ticker=%s code=%d — %s", ticker, e.response.status_code, e)
                continue
            except Exception as e:
                logger.error("取得失敗: ticker=%s — %s", ticker, e)
                continue

            logger.info("  取得件数: %d 行", len(quotes))

            if not quotes:
                logger.warning("  データなし: ticker=%s (コード %s が J-Quants に存在しない可能性)", ticker, jquants_code)
                continue

            async with session_factory() as session:
                inserted = await _upsert_rows(session, ticker, quotes, dry_run)

            logger.info("  挿入件数: %d 行 %s", inserted, "(dry-run)" if dry_run else "")

    await engine.dispose()
    logger.info("完了")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="J-Quants から日次 OHLCV データを初期充填する")
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="取得する取引日数の目安（デフォルト: 30）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="DB への書き込みを行わず取得件数のみ表示する",
    )
    parser.add_argument(
        "--market-suffix",
        default="0",
        help="J-Quants の市場コードサフィックス（デフォルト: 0 = TSE Prime）",
    )
    args = parser.parse_args()
    asyncio.run(main(days=args.days, dry_run=args.dry_run, market_suffix=args.market_suffix))
