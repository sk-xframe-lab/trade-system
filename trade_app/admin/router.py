"""
管理画面メインルーター

全ての管理画面サブルーターを /api/admin プレフィックスで集約する。
main.py でこの admin_router を app.include_router() する。

【認証】
各ルーターは RequireAdmin 依存関数でセッショントークン認証を行う。
既存の /api/admin/* (routes/admin.py) とは認証方式が異なる:
  - routes/admin.py   : API_TOKEN (Bearer、分析システム向け)
  - admin/router.py   : セッショントークン (管理画面 UI 向け)
"""
from fastapi import APIRouter

from trade_app.admin.routes.auth import router as auth_router
from trade_app.admin.routes.dashboard import router as dashboard_router
from trade_app.admin.routes.halt import router as halt_router
from trade_app.admin.routes.notifications import router as notifications_router
from trade_app.admin.routes.symbols import router as symbols_router
from trade_app.admin.routes.system_settings import router as system_settings_router
from trade_app.admin.routes.audit_logs import router as audit_logs_router

# 管理画面 API ルーター（プレフィックス: /api/ui-admin）
# 既存の /api/admin とは prefix を分けて混在を防ぐ
admin_router = APIRouter(prefix="/api/ui-admin")

admin_router.include_router(auth_router)
admin_router.include_router(dashboard_router)
admin_router.include_router(symbols_router)
admin_router.include_router(notifications_router)
admin_router.include_router(audit_logs_router)
admin_router.include_router(system_settings_router)
admin_router.include_router(halt_router)
