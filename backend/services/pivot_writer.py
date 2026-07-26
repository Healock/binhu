"""透视表写入器 - 将 daily_stats 结果写回腾讯文档"汇总"子表"""

from datetime import datetime


class PivotWriter:
    """将统计结果写回在线文档的"汇总"工作表"""

    # 汇总表表头（15列）
    HEADERS = [
        "核查人", "下发日期", "数据总数", "已核查", "未核查", "核查完成率",
        "无法核实", "移交", "已登记", "通勤", "离苏", "空白",
        "无法见底数", "核查见底率", "更新时间",
    ]

    # 批量写入限制
    MAX_OPERATIONS_PER_BATCH = 5  # 每次 batchUpdate 最多 5 个操作
    CELLS_PER_OPERATION = 10000   # 每个 updateRange 最多 10000 单元格
    COLS = len(HEADERS)           # 15
    ROWS_PER_OPERATION = CELLS_PER_OPERATION // COLS  # 666 行/操作

    def __init__(self, client):
        self.client = client

    async def write_summary(
        self,
        conn,
        spreadsheet_id: int,
        file_id: str,
        summary_sheet_id: str,
    ) -> None:
        """
        将统计结果写回汇总表
        1. 确保汇总表存在
        2. 读取 stats 数据
        3. 按批次写入
        """
        # 确保汇总表存在
        sheet_id = await self.client.ensure_sheet(file_id, summary_sheet_id)

        # 获取统计结果
        stats = await self._get_stats_for_sheet(conn, spreadsheet_id)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        # 构建写入数据：表头 + 数据行
        all_rows = [self.HEADERS]
        for s in stats:
            all_rows.append([
                s["核查人"],
                s["下发日期"],
                s["数据总数"],
                s["已核查"],
                s["未核查"],
                float(s["核查完成率"]),
                s["无法核实"],
                s["移交"],
                s["已登记"],
                s["通勤"],
                s["离苏"],
                s["空白"],
                s["无法见底数"],
                float(s["核查见底率"]),
                now,
            ])

        if not stats:
            all_rows.append(["暂无数据", "", "", "", "", "", "", "", "", "", "", "", "", "", now])

        # 分批写入
        # 每批 5 个操作，每个操作最多 666 行
        batch_rows_per_batch = self.MAX_OPERATIONS_PER_BATCH * self.ROWS_PER_OPERATION

        for chunk_start in range(0, len(all_rows), batch_rows_per_batch):
            chunk = all_rows[chunk_start : chunk_start + batch_rows_per_batch]
            requests = []

            for op_idx in range(0, len(chunk), self.ROWS_PER_OPERATION):
                op_data = chunk[op_idx : op_idx + self.ROWS_PER_OPERATION]
                if not op_data:
                    continue

                start_row = chunk_start + op_idx
                req = self.client.build_update_range_request(
                    sheet_id=sheet_id,
                    start_row=start_row,
                    start_col=0,
                    data=op_data,
                )
                requests.append(req)

                if len(requests) >= self.MAX_OPERATIONS_PER_BATCH:
                    break

            if requests:
                await self.client.batch_update(file_id, requests)

    async def _get_stats_for_sheet(self, conn, spreadsheet_id: int) -> list[dict]:
        """获取指定表格的统计结果"""
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT
                    核查人, 下发日期, 数据总数, 已核查, 未核查, 核查完成率,
                    无法核实, 移交, 已登记, 通勤, 离苏, 空白,
                    无法见底数, 核查见底率
                FROM daily_stats
                WHERE spreadsheet_id = %s
                ORDER BY 下发日期 DESC, 核查人 ASC""",
                (spreadsheet_id,),
            )
            rows = await cur.fetchall()

        columns = [
            "核查人", "下发日期", "数据总数", "已核查", "未核查", "核查完成率",
            "无法核实", "移交", "已登记", "通勤", "离苏", "空白",
            "无法见底数", "核查见底率",
        ]
        return [dict(zip(columns, row)) for row in rows]
