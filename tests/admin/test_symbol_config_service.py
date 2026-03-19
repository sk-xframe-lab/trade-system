"""
SymbolConfigService のテスト

【検証内容】
- create(): 新規作成・重複コードで ValueError
- get() / get_by_code(): ID/コードで取得。論理削除済みは含まない。
- list(): フィルタ（trade_type, is_enabled, search）・ページネーション・総件数
- update(): 変更前後 JSON を返す。symbol_code 変更不可。
- toggle_enabled(): enable/disable ショートカット
- soft_delete(): deleted_at 設定。get() で見えなくなる。
"""
import uuid

import pytest

from trade_app.admin.schemas.symbol_config import (
    SymbolConfigCreate,
    SymbolConfigFilter,
    SymbolConfigUpdate,
)
from trade_app.admin.services.symbol_config_service import SymbolConfigService


def _make_create_data(symbol_code: str = "7203", **kwargs) -> SymbolConfigCreate:
    defaults = dict(
        symbol_code=symbol_code,
        trade_type="daytrading",
        is_enabled=False,
        take_profit_pct=3.0,
        stop_loss_pct=2.0,
        max_hold_minutes=120,
        max_single_investment_jpy=500000,
        max_daily_investment_jpy=1000000,
    )
    defaults.update(kwargs)
    return SymbolConfigCreate(**defaults)


class TestSymbolConfigCreate:
    @pytest.mark.asyncio
    async def test_create_success(self, db_session):
        svc = SymbolConfigService(db_session)
        symbol, after_json = await svc.create(_make_create_data("7203"), created_by="admin")
        assert symbol.id is not None
        assert symbol.symbol_code == "7203"
        assert after_json["symbol_code"] == "7203"

    @pytest.mark.asyncio
    async def test_create_duplicate_code_raises(self, db_session):
        svc = SymbolConfigService(db_session)
        await svc.create(_make_create_data("7203"))
        await db_session.flush()
        with pytest.raises(ValueError, match="既に存在"):
            await svc.create(_make_create_data("7203"))

    @pytest.mark.asyncio
    async def test_create_sets_created_by(self, db_session):
        svc = SymbolConfigService(db_session)
        user_id = str(uuid.uuid4())
        symbol, _ = await svc.create(_make_create_data("9984"), created_by=user_id)
        assert symbol.created_by == user_id
        assert symbol.updated_by == user_id

    @pytest.mark.asyncio
    async def test_create_is_enabled_default_false(self, db_session):
        svc = SymbolConfigService(db_session)
        symbol, _ = await svc.create(_make_create_data("6758"))
        assert symbol.is_enabled is False


class TestSymbolConfigGet:
    @pytest.mark.asyncio
    async def test_get_by_id(self, db_session):
        svc = SymbolConfigService(db_session)
        symbol, _ = await svc.create(_make_create_data("7203"))
        await db_session.flush()
        fetched = await svc.get(symbol.id)
        assert fetched is not None
        assert fetched.symbol_code == "7203"

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, db_session):
        svc = SymbolConfigService(db_session)
        result = await svc.get(str(uuid.uuid4()))
        assert result is None

    @pytest.mark.asyncio
    async def test_get_by_code(self, db_session):
        svc = SymbolConfigService(db_session)
        await svc.create(_make_create_data("9984"))
        await db_session.flush()
        fetched = await svc.get_by_code("9984")
        assert fetched is not None
        assert fetched.symbol_code == "9984"

    @pytest.mark.asyncio
    async def test_get_deleted_returns_none(self, db_session):
        svc = SymbolConfigService(db_session)
        symbol, _ = await svc.create(_make_create_data("7203"))
        await db_session.flush()
        await svc.soft_delete(symbol.id)
        await db_session.flush()
        assert await svc.get(symbol.id) is None
        assert await svc.get_by_code("7203") is None


class TestSymbolConfigList:
    @pytest.mark.asyncio
    async def test_list_all(self, db_session):
        svc = SymbolConfigService(db_session)
        for code in ["7203", "9984", "6758"]:
            await svc.create(_make_create_data(code))
        await db_session.flush()

        symbols, total = await svc.list(SymbolConfigFilter())
        assert total == 3
        assert len(symbols) == 3

    @pytest.mark.asyncio
    async def test_list_filter_is_enabled(self, db_session):
        svc = SymbolConfigService(db_session)
        await svc.create(_make_create_data("7203", is_enabled=True))
        await svc.create(_make_create_data("9984", is_enabled=False))
        await db_session.flush()

        symbols, total = await svc.list(SymbolConfigFilter(is_enabled=True))
        assert total == 1
        assert symbols[0].symbol_code == "7203"

    @pytest.mark.asyncio
    async def test_list_search_by_code(self, db_session):
        svc = SymbolConfigService(db_session)
        await svc.create(_make_create_data("7203"))
        await svc.create(_make_create_data("9984"))
        await db_session.flush()

        symbols, total = await svc.list(SymbolConfigFilter(search="720"))
        assert total == 1
        assert symbols[0].symbol_code == "7203"

    @pytest.mark.asyncio
    async def test_list_excludes_deleted_by_default(self, db_session):
        svc = SymbolConfigService(db_session)
        s1, _ = await svc.create(_make_create_data("7203"))
        s2, _ = await svc.create(_make_create_data("9984"))
        await db_session.flush()
        await svc.soft_delete(s2.id)
        await db_session.flush()

        symbols, total = await svc.list(SymbolConfigFilter())
        assert total == 1
        assert symbols[0].symbol_code == "7203"

    @pytest.mark.asyncio
    async def test_list_include_deleted(self, db_session):
        svc = SymbolConfigService(db_session)
        s1, _ = await svc.create(_make_create_data("7203"))
        await db_session.flush()
        await svc.soft_delete(s1.id)
        await db_session.flush()

        symbols, total = await svc.list(SymbolConfigFilter(include_deleted=True))
        assert total == 1

    @pytest.mark.asyncio
    async def test_list_pagination(self, db_session):
        svc = SymbolConfigService(db_session)
        for i in range(5):
            await svc.create(_make_create_data(f"000{i}"))
        await db_session.flush()

        symbols, total = await svc.list(SymbolConfigFilter(), offset=0, limit=2)
        assert total == 5
        assert len(symbols) == 2


class TestSymbolConfigUpdate:
    @pytest.mark.asyncio
    async def test_update_returns_before_after(self, db_session):
        svc = SymbolConfigService(db_session)
        symbol, _ = await svc.create(_make_create_data("7203", is_enabled=False))
        await db_session.flush()

        update = SymbolConfigUpdate(is_enabled=True)
        updated, before, after = await svc.update(symbol.id, update)
        assert before["is_enabled"] is False
        assert after["is_enabled"] is True

    @pytest.mark.asyncio
    async def test_update_nonexistent_raises(self, db_session):
        svc = SymbolConfigService(db_session)
        with pytest.raises(ValueError, match="見つかりません"):
            await svc.update(str(uuid.uuid4()), SymbolConfigUpdate(is_enabled=True))

    @pytest.mark.asyncio
    async def test_update_partial_fields(self, db_session):
        svc = SymbolConfigService(db_session)
        symbol, _ = await svc.create(_make_create_data("7203", max_hold_minutes=60))
        await db_session.flush()

        update = SymbolConfigUpdate(max_hold_minutes=90)
        updated, _, after = await svc.update(symbol.id, update)
        assert updated.max_hold_minutes == 90
        assert after["max_hold_minutes"] == 90


class TestSymbolConfigToggleEnabled:
    @pytest.mark.asyncio
    async def test_toggle_enable(self, db_session):
        svc = SymbolConfigService(db_session)
        symbol, _ = await svc.create(_make_create_data("7203", is_enabled=False))
        await db_session.flush()

        updated, before, after = await svc.toggle_enabled(symbol.id, enabled=True)
        assert updated.is_enabled is True
        assert before["is_enabled"] is False
        assert after["is_enabled"] is True

    @pytest.mark.asyncio
    async def test_toggle_disable(self, db_session):
        svc = SymbolConfigService(db_session)
        symbol, _ = await svc.create(_make_create_data("7203", is_enabled=True))
        await db_session.flush()

        updated, _, after = await svc.toggle_enabled(symbol.id, enabled=False)
        assert updated.is_enabled is False
        assert after["is_enabled"] is False


class TestSymbolConfigSoftDelete:
    @pytest.mark.asyncio
    async def test_soft_delete_sets_deleted_at(self, db_session):
        svc = SymbolConfigService(db_session)
        symbol, _ = await svc.create(_make_create_data("7203"))
        await db_session.flush()

        deleted, before_json = await svc.soft_delete(symbol.id)
        assert deleted.deleted_at is not None
        assert before_json["symbol_code"] == "7203"

    @pytest.mark.asyncio
    async def test_soft_delete_nonexistent_raises(self, db_session):
        svc = SymbolConfigService(db_session)
        with pytest.raises(ValueError, match="見つかりません"):
            await svc.soft_delete(str(uuid.uuid4()))

    @pytest.mark.asyncio
    async def test_soft_deleted_is_invisible_to_get(self, db_session):
        svc = SymbolConfigService(db_session)
        symbol, _ = await svc.create(_make_create_data("7203"))
        await db_session.flush()
        await svc.soft_delete(symbol.id)
        await db_session.flush()

        assert await svc.get(symbol.id) is None
