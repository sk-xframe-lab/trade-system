"""
システム設定ルーター (SCR-14)

仕様書: 管理画面仕様書 v0.3 §3(SCR-14)

【エンドポイント一覧】
GET   /admin/system-settings  — 現在の設定値一覧
PATCH /admin/system-settings  — 設定値更新（監査ログ記録）

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️  永続化の扱い（暫定実装）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PATCH で行う設定変更は「ランタイム上書き」のみ。

仕組み:
  - get_settings() は lru_cache で Settings シングルトンを返す
  - object.__setattr__() でシングルトンのフィールドを直接書き換える
  - 同一プロセス内のすべての get_settings() 呼び出しに反映される
  - ただしプロセス再起動（deploy / restart）で .env の値に戻る

制約:
  - Docker コンテナが複数起動している場合、各コンテナの in-memory 値は独立する
  - .env への書き込みは行わない
  - DB への永続化は行わない

TODO(Phase 2): .env ファイルへの永続化または DB 保存を実装する。
レスポンスの persistence_mode フィールドでこの挙動を明示する。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.admin.constants import AdminAuditEventType
from trade_app.admin.routes._common import get_client_ip, get_user_agent
from trade_app.admin.schemas.system_settings import (
    SystemSettingsResponse,
    SystemSettingsUpdateRequest,
    SystemSettingsUpdateResponse,
)
from trade_app.admin.services.audit_log_service import UiAuditLogService
from trade_app.admin.services.auth_guard import RequireAdmin, get_admin_db
from trade_app.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/system-settings", tags=["Admin System Settings"])


def _settings_to_dict(settings) -> dict:
    """Settings オブジェクトをシリアライズ可能な dict に変換する"""
    return {
        "daily_loss_limit_jpy": settings.DAILY_LOSS_LIMIT_JPY,
        "max_concurrent_positions": settings.MAX_CONCURRENT_POSITIONS,
        "consecutive_losses_stop": settings.CONSECUTIVE_LOSSES_STOP,
        "exit_watcher_interval_sec": settings.EXIT_WATCHER_INTERVAL_SEC,
        "strategy_runner_interval_sec": settings.STRATEGY_RUNNER_INTERVAL_SEC,
        "market_state_interval_sec": settings.MARKET_STATE_INTERVAL_SEC,
        "strategy_max_state_age_sec": settings.STRATEGY_MAX_STATE_AGE_SEC,
        "signal_max_decision_age_sec": settings.SIGNAL_MAX_DECISION_AGE_SEC,
        "watched_symbols": [
            s.strip() for s in settings.WATCHED_SYMBOLS.split(",") if s.strip()
        ],
    }


@router.get("", response_model=SystemSettingsResponse)
async def get_system_settings(
    current_user: RequireAdmin,
) -> SystemSettingsResponse:
    """
    現在のシステム設定値を返す。

    persistence_mode == "runtime_only" の場合、PATCH で変更してもプロセス再起動で .env に戻る。
    """
    settings = get_settings()
    watched = [s.strip() for s in settings.WATCHED_SYMBOLS.split(",") if s.strip()]
    return SystemSettingsResponse(
        daily_loss_limit_jpy=settings.DAILY_LOSS_LIMIT_JPY,
        max_concurrent_positions=settings.MAX_CONCURRENT_POSITIONS,
        consecutive_losses_stop=settings.CONSECUTIVE_LOSSES_STOP,
        exit_watcher_interval_sec=settings.EXIT_WATCHER_INTERVAL_SEC,
        order_poller_interval_sec=None,  # 現在は設定ファイル固定
        strategy_runner_interval_sec=settings.STRATEGY_RUNNER_INTERVAL_SEC,
        market_state_interval_sec=settings.MARKET_STATE_INTERVAL_SEC,
        strategy_max_state_age_sec=settings.STRATEGY_MAX_STATE_AGE_SEC,
        signal_max_decision_age_sec=settings.SIGNAL_MAX_DECISION_AGE_SEC,
        watched_symbols=watched,
    )


@router.patch("", response_model=SystemSettingsUpdateResponse)
async def update_system_settings(
    request: Request,
    body: SystemSettingsUpdateRequest,
    current_user: RequireAdmin,
    db: AsyncSession = Depends(get_admin_db),
) -> SystemSettingsUpdateResponse:
    """
    システム設定を更新する（ランタイム上書き）。

    変更は監査ログに記録される。
    プロセス再起動後は .env に戻る（TODO Phase 2: .env 永続化）。
    """
    settings = get_settings()
    before = _settings_to_dict(settings)

    updated_fields: list[str] = []
    update_data = body.model_dump(exclude_none=True)

    # ランタイム上書き
    # pydantic-settings は model_config = {"frozen": True} のため通常の setattr は禁止。
    # object.__setattr__() で lru_cache シングルトンを直接書き換える。
    # 注意: この変更はプロセス再起動後に .env の値に戻る（TODO Phase 2: 永続化未実装）。
    field_map = {
        "daily_loss_limit_jpy": "DAILY_LOSS_LIMIT_JPY",
        "max_concurrent_positions": "MAX_CONCURRENT_POSITIONS",
        "consecutive_losses_stop": "CONSECUTIVE_LOSSES_STOP",
        "exit_watcher_interval_sec": "EXIT_WATCHER_INTERVAL_SEC",
        "strategy_runner_interval_sec": "STRATEGY_RUNNER_INTERVAL_SEC",
        "market_state_interval_sec": "MARKET_STATE_INTERVAL_SEC",
        "strategy_max_state_age_sec": "STRATEGY_MAX_STATE_AGE_SEC",
        "signal_max_decision_age_sec": "SIGNAL_MAX_DECISION_AGE_SEC",
    }

    for field_name, setting_key in field_map.items():
        if field_name in update_data:
            object.__setattr__(settings, setting_key, update_data[field_name])
            updated_fields.append(field_name)

    if "watched_symbols" in update_data:
        new_watched = ",".join(update_data["watched_symbols"])
        object.__setattr__(settings, "WATCHED_SYMBOLS", new_watched)
        updated_fields.append("watched_symbols")

    after = _settings_to_dict(settings)

    if updated_fields:
        audit_svc = UiAuditLogService(db)
        await audit_svc.write(
            AdminAuditEventType.SYSTEM_SETTINGS_UPDATED,
            user_id=current_user.user_id,
            user_email=current_user.email,
            resource_type="system_settings",
            resource_label="システム設定",
            before_json={k: before[k] for k in updated_fields if k in before},
            after_json={k: after[k] for k in updated_fields if k in after},
            description=f"変更フィールド: {', '.join(updated_fields)}",
            ip_address=get_client_ip(request),
            user_agent=get_user_agent(request),
        )
        await db.commit()
        logger.info("システム設定更新: fields=%s by=%s", updated_fields, current_user.email)

    return SystemSettingsUpdateResponse(
        updated_fields=updated_fields,
        before={k: before[k] for k in updated_fields if k in before},
        after={k: after[k] for k in updated_fields if k in after},
        message=f"{len(updated_fields)} 項目を更新しました" if updated_fields else "変更なし",
    )
