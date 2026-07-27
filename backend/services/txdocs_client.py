"""腾讯文档 OpenAPI v3 客户端

封装对 docs.qq.com 的 HTTP 调用，包括：
- 读取表格数据（分页）
- 批量写入数据
- 确保子表存在
"""

import asyncio
import httpx
from typing import Optional
from config import settings

# 14 个业务列的列号映射（A=0, N=13）
COLUMNS = [
    "下发日期", "截止日期", "核查人", "社区", "来源",
    "姓名", "身份证号", "电话号码", "地址", "创建时间",
    "现住址", "核查结果", "研判", "二次反馈",
]
COLUMN_COUNT = len(COLUMNS)  # 14

# API 同时限制单次读取的单元格数和行数。
# 即使单元格没有超过 10000，读取 1001 行以上也可能返回空数据。
MAX_CELLS_PER_PAGE = 10000
MAX_ROWS_PER_PAGE = 1000


class TxDocsClient:
    """腾讯文档 API 客户端"""

    def __init__(
        self,
        client_id: str,
        access_token: str,
        open_id: str,
        http_client: Optional[httpx.AsyncClient] = None,
    ):
        self.client_id = client_id
        self.access_token = access_token
        self.open_id = open_id
        self._http = http_client
        self._owns_http = http_client is None

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=settings.TXDOCS_BASE_URL,
                timeout=30.0,
            )
        return self._http

    def _headers(self) -> dict:
        return {
            "Access-Token": self.access_token,
            "Client-Id": self.client_id,
            "Open-Id": self.open_id,
            "Content-Type": "application/json",
        }

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        **kwargs,
    ) -> dict:
        """带重试和限频的请求"""
        http = await self._get_http()
        max_retries = 3
        for attempt in range(max_retries):
            try:
                resp = await http.request(method, url, **kwargs)
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 2 ** attempt))
                    await asyncio.sleep(retry_after)
                    continue
                resp.raise_for_status()
                # 请求间延迟，避免限频
                await asyncio.sleep(settings.API_RATE_LIMIT_DELAY_MS / 1000)
                return resp.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code >= 500 and attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise
        raise Exception(f"请求失败，已重试 {max_retries} 次: {url}")

    async def read_range(
        self,
        file_id: str,
        sheet_id: str,
        range_str: str,
    ) -> dict:
        """
        读取指定范围的数据
        GET /files/{file_id}/{sheet_id}/{range_str}
        """
        url = f"/files/{file_id}/{sheet_id}/{range_str}"
        return await self._request_with_retry("GET", url, headers=self._headers())

    async def read_all_data(
        self,
        file_id: str,
        sheet_id: str,
        header_row: int = 1,
        column_names: list[str] | None = None,
    ) -> list[dict]:
        """
        分页读取全部数据，返回字典列表
        column_names 指定列名映射（按列位置 A, B, C...），默认用全局 COLUMNS
        """
        if column_names is None:
            column_names = COLUMNS

        col_count = len(column_names)
        rows_per_page = min(
            MAX_ROWS_PER_PAGE,
            MAX_CELLS_PER_PAGE // col_count,
        )

        # 列字母映射
        col_start = "A"
        if col_count <= 26:
            col_end = chr(ord("A") + col_count - 1)
        else:
            col_end = chr(ord("A") + (col_count - 1) // 26 - 1) + chr(ord("A") + (col_count - 1) % 26)

        all_data = []
        data_start_row = header_row + 1
        current_row = data_start_row

        while True:
            end_row = current_row + rows_per_page - 1
            range_str = f"{col_start}{current_row}:{col_end}{end_row}"

            result = await self.read_range(file_id, sheet_id, range_str)
            rows = self._extract_rows(result, column_names)

            if not rows:
                break

            all_data.extend(rows)

            if len(rows) < rows_per_page:
                break

            current_row = end_row + 1

        return all_data

    def _extract_rows(self, response: dict, column_names: list[str] | None = None) -> list[dict]:
        """从 API 响应中提取数据行，映射为字典

        支持两种响应格式：
        - v3: {"gridData": {"rows": [{"values": [{"cellValue": {"text": "xxx"}}, ...]}]}}
        - 旧: {"data": [["val1", "val2", ...]]}
        """
        if column_names is None:
            column_names = COLUMNS

        # v3 格式
        grid_data = response.get("gridData", {})
        raw_rows = grid_data.get("rows", [])

        # 旧格式兼容
        if not raw_rows:
            raw_rows = response.get("data", [])
        if not raw_rows:
            return []

        result = []
        for row in raw_rows:
            # v3 格式: {"values": [{"cellValue": {"text": "xxx"}, ...}]}
            if isinstance(row, dict) and "values" in row:
                cells = row["values"]
                values = []
                for cell in cells:
                    cv = cell.get("cellValue") if isinstance(cell, dict) else None
                    if cv is None:
                        values.append("")
                    elif isinstance(cv, dict):
                        if "text" in cv:
                            values.append(str(cv["text"]) if cv["text"] else "")
                        elif "select" in cv:
                            sel = cv.get("select", {})
                            vals = sel.get("value", [])
                            options = sel.get("options", [])
                            is_multiple = sel.get("multiple", False)
                            if vals:
                                if is_multiple:
                                    # 多选：拼接所有值的text
                                    texts = []
                                    for v in vals:
                                        found = None
                                        for opt in options:
                                            if str(opt.get("id")) == str(v):
                                                found = opt.get("text", "")
                                                break
                                        texts.append(found if found is not None else str(v))
                                    values.append(",".join(texts))
                                else:
                                    # 单选：取第一个值
                                    raw = vals[0]
                                    found = None
                                    for opt in options:
                                        if str(opt.get("id")) == str(raw):
                                            found = opt.get("text", "")
                                            break
                                    values.append(found if found is not None else str(raw))
                            else:
                                values.append("")
                        elif "number" in cv:
                            values.append(str(cv["number"]))
                        else:
                            values.append("")
                    else:
                        values.append(str(cv) if cv else "")
            # 旧格式: ["val1", "val2", ...]
            elif isinstance(row, list):
                values = [str(v) if v else "" for v in row]
            else:
                continue

            if not values or not any(values):
                continue

            record = {}
            for i, col_name in enumerate(column_names):
                record[col_name] = values[i] if i < len(values) else ""
            result.append(record)

        return result

    async def batch_update(
        self,
        file_id: str,
        requests: list[dict],
    ) -> dict:
        """
        批量更新
        POST /files/{file_id}/batchUpdate
        每次最多 5 个操作
        """
        if len(requests) > 5:
            raise ValueError(f"batchUpdate 最多 5 个操作，收到 {len(requests)} 个")

        url = f"/files/{file_id}/batchUpdate"
        return await self._request_with_retry(
            "POST",
            url,
            headers=self._headers(),
            json={"requests": requests},
        )

    def build_update_range_request(
        self,
        sheet_id: str,
        start_row: int,
        start_col: int,
        data: list[list],
    ) -> dict:
        """
        构建 updateRange 请求体
        start_row/start_col 从 0 开始
        data 是二维列表，每个单元格可以是 str 或数字
        """
        rows = []
        for row_data in data:
            values = []
            for cell in row_data:
                if isinstance(cell, (int, float)):
                    values.append({"cellValue": {"number": cell}})
                else:
                    values.append({"cellValue": {"text": str(cell)}})
            rows.append({"values": values})

        return {
            "updateRangeRequest": {
                "sheetId": sheet_id,
                "gridData": {
                    "startRow": start_row,
                    "startColumn": start_col,
                    "rows": rows,
                },
            }
        }

    def build_add_sheet_request(self, title: str, rows: int = 200, cols: int = 20) -> dict:
        """构建创建子表的请求"""
        return {
            "addSheetRequest": {
                "title": title,
                "rowCount": rows,
                "columnCount": cols,
            }
        }

    async def get_file_info(self, file_id: str) -> dict:
        """获取文件元数据（含所有子表信息）"""
        url = f"/files/{file_id}?concise=1"
        return await self._request_with_retry("GET", url, headers=self._headers())

    async def ensure_sheet(self, file_id: str, sheet_name: str) -> str:
        """
        确保子表存在，如果不存在则创建
        返回 sheetId
        """
        try:
            file_info = await self.get_file_info(file_id)
            sheets = file_info.get("sheets", [])
            for sheet in sheets:
                props = sheet.get("properties", {})
                if props.get("title") == sheet_name:
                    return props.get("sheetId", sheet_name)
        except Exception:
            pass

        # 子表不存在，创建
        add_req = self.build_add_sheet_request(sheet_name)
        await self.batch_update(file_id, [add_req])
        # 重新获取以确认 sheetId
        file_info = await self.get_file_info(file_id)
        sheets = file_info.get("sheets", [])
        for sheet in sheets:
            props = sheet.get("properties", {})
            if props.get("title") == sheet_name:
                return props.get("sheetId", sheet_name)
        # 返回名称作为 fallback
        return sheet_name

    async def close(self):
        if self._owns_http and self._http:
            await self._http.aclose()
