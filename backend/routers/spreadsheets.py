"""已下线的腾讯在线表格配置兼容接口。

历史配置表继续作为迁移审计材料保留，但应用不再读取、修改或测试配置。
"""

from fastapi import APIRouter, Depends, HTTPException

from deps import require_super_admin


router = APIRouter(
    prefix="/api/spreadsheets",
    tags=["已下线的在线表格配置"],
    dependencies=[Depends(require_super_admin)],
)


def _txdocs_gone() -> None:
    raise HTTPException(
        status_code=410,
        detail="腾讯文档数据源已正式下线，在线表格配置不可用",
    )


@router.api_route("", methods=["GET", "POST", "PUT", "DELETE"])
async def spreadsheets_root():
    _txdocs_gone()


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def spreadsheets_compat(path: str):
    _txdocs_gone()
