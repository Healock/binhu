"""测试数据管理 API - 初始化模拟数据用于验证工作量统计"""

from datetime import date, timedelta
from fastapi import APIRouter, Depends
from database import get_db

router = APIRouter(prefix="/api/test", tags=["测试数据"])

# 模拟表结构：与全链条一致
CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS t_test_mock (
        _row_key VARCHAR(200) NOT NULL,
        社区 VARCHAR(100) DEFAULT '',
        核查人 VARCHAR(100) DEFAULT '',
        姓名 VARCHAR(100) DEFAULT '',
        现住址 VARCHAR(500) DEFAULT '',
        核查结果 VARCHAR(500) DEFAULT '',
        下发日期 VARCHAR(50) DEFAULT '',
        截止日期 VARCHAR(50) DEFAULT '',
        _first_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        _last_updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uk_row_key (_row_key)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """


@router.post("/init-mock-data")
async def init_mock_data(conn=Depends(get_db)):
    """初始化测试数据 - 模拟4种场景验证工作量统计

    用"昨天"和"今天"模拟日期，确保不管哪天测试都能工作：
    - test_1: 昨天入库、今天更新现住址 → 今天日报"已核查"
    - test_2: 昨天入库、今天更新核查结果 → 今天日报"已完成"
    - test_3: 昨天入库、今天没更新 → 今天日报不出现
    - test_4: 今天入库、现住址空 → 今天日报"未核查"
    """
    today = date.today()
    yesterday = today - timedelta(days=1)
    today_str = today.isoformat()
    yesterday_str = yesterday.isoformat()

    async with conn.cursor() as cur:
        await cur.execute(CREATE_TABLE_SQL)
        await cur.execute("TRUNCATE TABLE t_test_mock")

        # 显式设置 _first_seen_at 和 _last_updated_at 模拟不同场景
        rows = [
            # test_1: 昨天入库，今天更新了现住址（核查人今天做了工作→已核查）
            ("test_1", "三船港", "吴波", "测试A", "天铂1幢101", "",
             f"{yesterday_str} 09:00:00", f"{today_str} 14:00:00"),
            # test_2: 昨天入库，今天更新了核查结果（核查人今天做了工作→已完成）
            ("test_2", "三船港", "李四", "测试B", "某小区5栋", "已登记",
             f"{yesterday_str} 09:00:00", f"{today_str} 15:00:00"),
            # test_3: 昨天入库，今天没更新（核查人今天没做工作→不出现）
            ("test_3", "三船港", "张三", "测试C", "", "",
             f"{yesterday_str} 09:00:00", f"{yesterday_str} 09:00:00"),
            # test_4: 今天新入库（新数据→未核查）
            ("test_4", "三船港", "王五", "测试D", "", "",
             f"{today_str} 10:00:00", f"{today_str} 10:00:00"),
        ]

        for row in rows:
            await cur.execute(
                "INSERT INTO t_test_mock "
                "(_row_key, 社区, 核查人, 姓名, 现住址, 核查结果, _first_seen_at, _last_updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                row,
            )

        # 验证数据
        await cur.execute(
            "SELECT _row_key, 社区, 核查人, 姓名, 现住址, 核查结果, "
            "DATE(_first_seen_at) AS 入库日, DATE(_last_updated_at) AS 更新日 "
            "FROM t_test_mock ORDER BY _row_key"
        )
        verify = await cur.fetchall()

    return {
        "message": f"测试数据已初始化（{today_str}），共 {len(rows)} 条",
        "today": today_str,
        "yesterday": yesterday_str,
        "data": [
            {
                "key": r[0], "社区": r[1], "核查人": r[2], "姓名": r[3],
                "现住址": r[4] or "(空)", "核查结果": r[5] or "(空)",
                "入库日": str(r[6]), "更新日": str(r[7]),
                "预期今天日报": (
                    "已核查" if r[4] and not r[5] and str(r[7]) == today_str
                    else "已完成" if r[5] and str(r[7]) == today_str
                    else "未核查" if not r[4] and str(r[7]) == today_str
                    else "不出现"
                )
            }
            for r in verify
        ],
    }


@router.get("/mock-data")
async def get_mock_data(conn=Depends(get_db)):
    """查看当前测试数据"""
    async with conn.cursor() as cur:
        await cur.execute(CREATE_TABLE_SQL)
        await cur.execute(
            "SELECT _row_key, 社区, 核查人, 姓名, 现住址, 核查结果, "
            "DATE(_first_seen_at) AS 入库日, DATE(_last_updated_at) AS 更新日 "
            "FROM t_test_mock ORDER BY _row_key"
        )
        rows = await cur.fetchall()

    today_str = date.today().isoformat()
    return {
        "today": today_str,
        "data": [
            {
                "key": r[0], "社区": r[1], "核查人": r[2], "姓名": r[3],
                "现住址": r[4] or "(空)", "核查结果": r[5] or "(空)",
                "入库日": str(r[6]), "更新日": str(r[7]),
                "预期今天日报": (
                    "已核查" if r[4] and not r[5] and str(r[7]) == today_str
                    else "已完成" if r[5] and str(r[7]) == today_str
                    else "未核查" if not r[4] and str(r[7]) == today_str
                    else "不出现"
                )
            }
            for r in rows
        ],
    }


@router.post("/save-snapshot")
async def save_mock_snapshot(conn=Depends(get_db)):
    """为测试数据手动保存快照（不依赖同步引擎）"""
    from datetime import date as _date
    from database import db_manager

    today_str = _date.today().isoformat()
    snap_table = f"{today_str}_snapshot_testMock"
    pool = db_manager.get_pool("daily_report")
    snap_conn = await pool.acquire()
    try:
        async with snap_conn.cursor() as cur:
            await cur.execute(f"DROP TABLE IF EXISTS daily_report.`{snap_table}`")
            await cur.execute(
                f"CREATE TABLE daily_report.`{snap_table}` AS SELECT * FROM OnlineData.t_test_mock"
            )
            await cur.execute(
                "INSERT INTO daily_report._daily_report_meta (table_name, report_date, parser_type, generation_method) "
                "VALUES (%s, %s, %s, 'snapshot') ON DUPLICATE KEY UPDATE generated_at = NOW()",
                (snap_table, today_str, "测试数据_snapshot"),
            )
        return {"message": f"快照已保存: {snap_table}", "date": today_str}
    finally:
        pool.release(snap_conn)
