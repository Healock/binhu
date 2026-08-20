"""滨湖智慧平台 - FastAPI 入口"""

import os
import asyncio
from contextlib import suppress
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app_version import APP_VERSION
from config import settings
from database import init_db, close_db
from deps import get_current_user
from routers.spreadsheets import router as spreadsheets_router
from routers.sync import router as sync_router
from routers.stats import router as stats_router
from routers.auth import router as auth_router
from routers.query import router as query_router
from routers.grid_members import router as grid_members_router
from routers.system import router as system_router
from routers.users import router as users_router
from routers.notifications import router as notifications_router
from routers.admin_ops import router as admin_ops_router
from routers.visits import router as visits_router
from routers.visit_sources import router as visit_sources_router
from routers.code_summaries import router as code_summaries_router
from routers.personnel_attendance import router as personnel_attendance_router
from routers.work_logs import router as work_logs_router
from routers.permission_groups import router as permission_groups_router
from routers.mobile_tasks import router as mobile_tasks_router
from routers.police_dispatch import router as police_dispatch_router
from routers.profiles import router as profiles_router
from routers.dashboard import router as dashboard_router
from routers.exports import router as exports_router
from routers.registry import router as registry_router
from routers.registry_extended import router as registry_extended_router
from routers.watch_import import router as watch_import_router
from routers.workflow import router as workflow_router
from routers.workflow_extended import router as workflow_extended_router
from routers.workflow_photo_sheet import router as workflow_photo_sheet_router
from routers.qmf_registration import router as qmf_registration_router
from routers.task_graph import router as task_graph_router
from routers.maintenance import router as maintenance_router
from routers.app_bootstrap import router as app_bootstrap_router
from services.backup_scheduler import run_backup_scheduler
from services.backups import recover_interrupted_backups, stop_backup_tasks
from services.sync_scheduler import run_sync_scheduler
from services.sync_tasks import recover_interrupted_tasks, stop_sync_tasks
from services.visit_import import recover_interrupted_visit_imports
from services.workflow_scheduler import run_workflow_scheduler
from services.photo_sheet_sync import run_photo_sheet_scheduler, stop_photo_sheet_tasks
from services.online_local_writeback import (
    run_online_writeback_scheduler,
    stop_online_writeback_tasks,
)
from services.client_compatibility import ClientCompatibilityMiddleware
from services.txdocs_usage import stop_txdocs_usage_tasks
from services.registry_certificate_jobs import (
    recover_interrupted_certificate_source_runs,
    stop_certificate_source_tasks,
)
from services.registry_certificate_scheduler import run_registry_certificate_scheduler
from services.police_dispatch_publish_jobs import (
    recover_interrupted_police_publish_runs,
    stop_police_publish_tasks,
)
from services.qmf_status_scan import (
    recover_status_scans,
    run_status_scan_scheduler,
    stop_status_scan_tasks,
)
from services.external_acquisition_jobs import recover_interrupted_jobs, stop_external_acquisition_tasks
from routers.external_acquisition import router as external_acquisition_router
from services.presence import run_presence_cleanup_scheduler
from routers.presence import router as presence_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时初始化数据库连接池，关闭时清理"""
    await init_db()
    interrupted = await recover_interrupted_tasks()
    if interrupted:
        print(f"[SYNC] 已关闭 {interrupted} 个服务重启前遗留的同步任务")
    interrupted_backups = await recover_interrupted_backups()
    if interrupted_backups:
        print(
            f"[BACKUP] 已关闭 {interrupted_backups} "
            "个服务重启前遗留的备份任务"
        )
    interrupted_visit_imports = await recover_interrupted_visit_imports()
    if interrupted_visit_imports:
        print(
            f"[VISIT] 已关闭 {interrupted_visit_imports} "
            "个服务重启前遗留的导入任务"
        )
    interrupted_certificate_runs = await recover_interrupted_certificate_source_runs()
    if interrupted_certificate_runs:
        print(
            f"[REGISTRY_CERTIFICATE] 已保留 {interrupted_certificate_runs} 个"
            "服务重启前遗留任务的分页进度"
        )
    interrupted_publish_runs = await recover_interrupted_police_publish_runs()
    if interrupted_publish_runs:
        print(
            f"[POLICE_DISPATCH] 已安全关闭 {interrupted_publish_runs} 个"
            "服务重启前遗留的后台发布任务"
        )
    recovered_qmf_scans = await recover_status_scans()
    if recovered_qmf_scans:
        print("[QMF_STATUS_SCAN] 已恢复服务重启前的只读扫描任务")
    recovered_external_jobs = await recover_interrupted_jobs()
    if recovered_external_jobs:
        print(f"[EXTERNAL] 已关闭 {recovered_external_jobs} 个外部获取遗留任务")
    scheduler_task = asyncio.create_task(run_sync_scheduler())
    backup_scheduler_task = asyncio.create_task(run_backup_scheduler())
    workflow_scheduler_task = asyncio.create_task(run_workflow_scheduler())
    photo_sheet_scheduler_task = asyncio.create_task(run_photo_sheet_scheduler())
    online_writeback_task = asyncio.create_task(run_online_writeback_scheduler())
    certificate_scheduler_task = asyncio.create_task(run_registry_certificate_scheduler())
    qmf_status_scan_scheduler_task = asyncio.create_task(run_status_scan_scheduler())
    presence_cleanup_task = asyncio.create_task(run_presence_cleanup_scheduler())
    try:
        yield
    finally:
        scheduler_task.cancel()
        backup_scheduler_task.cancel()
        workflow_scheduler_task.cancel()
        photo_sheet_scheduler_task.cancel()
        online_writeback_task.cancel()
        certificate_scheduler_task.cancel()
        qmf_status_scan_scheduler_task.cancel()
        presence_cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await scheduler_task
        with suppress(asyncio.CancelledError):
            await backup_scheduler_task
        with suppress(asyncio.CancelledError):
            await workflow_scheduler_task
        with suppress(asyncio.CancelledError):
            await photo_sheet_scheduler_task
        with suppress(asyncio.CancelledError):
            await online_writeback_task
        with suppress(asyncio.CancelledError):
            await certificate_scheduler_task
        with suppress(asyncio.CancelledError):
            await qmf_status_scan_scheduler_task
        with suppress(asyncio.CancelledError):
            await presence_cleanup_task
        await stop_sync_tasks()
        await stop_backup_tasks()
        await stop_photo_sheet_tasks()
        await stop_online_writeback_tasks()
        await stop_txdocs_usage_tasks()
        await stop_certificate_source_tasks()
        await stop_police_publish_tasks()
        await stop_status_scan_tasks()
        await stop_external_acquisition_tasks()
        await close_db()


app = FastAPI(
    title="滨湖智慧平台",
    description="从腾讯文档获取数据，统计核查结果，生成数据透视表",
    version=APP_VERSION,
    lifespan=lifespan,
)

app.add_middleware(ClientCompatibilityMiddleware)

# 生产环境由 Nginx 同源代理，不需要 CORS。确有外部调用时必须显式列出来源。
if settings.cors_allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Accept",
            "Authorization",
            "Content-Type",
            "X-User-Activity",
            "X-Binhu-Client-Platform",
            "X-Binhu-Client-Version",
        ],
    )

# 健康检查（无需鉴权）
@app.get("/api/health")
async def health_check():
    return {
        "status": "ok",
        "message": "滨湖智慧平台运行中",
        "version": APP_VERSION,
    }

# auth 路由（login 端点无需鉴权，logout/me 需要鉴权在路由内处理）
app.include_router(auth_router)
app.include_router(presence_router)
app.include_router(maintenance_router)
app.include_router(app_bootstrap_router)

# 业务路由（全部需要登录）
auth_dep = [Depends(get_current_user)]
app.include_router(spreadsheets_router, dependencies=auth_dep)
app.include_router(sync_router, dependencies=auth_dep)
app.include_router(stats_router, dependencies=auth_dep)
app.include_router(query_router, dependencies=auth_dep)
app.include_router(mobile_tasks_router, dependencies=auth_dep)
app.include_router(police_dispatch_router, dependencies=auth_dep)
app.include_router(grid_members_router, dependencies=auth_dep)
app.include_router(system_router, dependencies=auth_dep)
app.include_router(notifications_router, dependencies=auth_dep)
app.include_router(visits_router, dependencies=auth_dep)
app.include_router(visit_sources_router, dependencies=auth_dep)
app.include_router(code_summaries_router, dependencies=auth_dep)
app.include_router(personnel_attendance_router, dependencies=auth_dep)
app.include_router(work_logs_router, dependencies=auth_dep)
app.include_router(permission_groups_router, dependencies=auth_dep)
app.include_router(profiles_router, dependencies=auth_dep)
app.include_router(dashboard_router, dependencies=auth_dep)
app.include_router(exports_router, dependencies=auth_dep)
app.include_router(registry_router, dependencies=auth_dep)
app.include_router(registry_extended_router, dependencies=auth_dep)
app.include_router(watch_import_router, dependencies=auth_dep)
app.include_router(workflow_router, dependencies=auth_dep)
app.include_router(workflow_extended_router, dependencies=auth_dep)
app.include_router(workflow_photo_sheet_router, dependencies=auth_dep)
app.include_router(qmf_registration_router, dependencies=auth_dep)
app.include_router(task_graph_router, dependencies=auth_dep)
app.include_router(external_acquisition_router, dependencies=auth_dep)

# 用户管理路由（超管专用，dependencies 在路由内 Depends(require_super_admin)）
app.include_router(users_router, dependencies=auth_dep)
app.include_router(admin_ops_router, dependencies=auth_dep)


# ========== 前端静态文件一体化托管（单端口模式，无需 Nginx） ==========
STATIC_DIR = os.environ.get("STATIC_DIR", "../frontend/dist")

# 挂载 assets 目录（JS/CSS 等构建产物）
_assets_dir = os.path.join(STATIC_DIR, "assets")
if os.path.isdir(_assets_dir):
    app.mount("/assets", StaticFiles(directory=_assets_dir), name="assets")


@app.get("/{full_path:path}")
async def spa_fallback(full_path: str):
    """非 API 路径：返回静态文件，找不到则回退到 index.html（SPA 路由）"""
    if full_path.startswith("api/"):
        return {"error": "Not found", "path": f"/{full_path}"}
    # 尝试返回对应静态文件（favicon、vite.svg 等）
    file_path = os.path.join(STATIC_DIR, full_path)
    if os.path.isfile(file_path):
        return FileResponse(file_path)
    # SPA 路由回退
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.isfile(index_path):
        return FileResponse(index_path)
    return {"error": "Frontend not built", "static_dir": STATIC_DIR}
