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


def column_letter(index: int) -> str:
    """把从 0 开始的列号转换为 Excel 列字母。"""
    value = index + 1
    result = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


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

        rows = await self.read_all_source_rows(
            file_id,
            sheet_id,
            header_row,
            column_names,
        )
        return [row["values"] for row in rows]

    async def read_all_source_rows(
        self,
        file_id: str,
        sheet_id: str,
        header_row: int = 1,
        column_names: list[str] | None = None,
    ) -> list[dict]:
        """读取非空数据行，并保留腾讯表中的一基物理行号和单元格类型。"""
        if column_names is None:
            column_names = COLUMNS
        col_count = len(column_names)
        rows_per_page = min(
            MAX_ROWS_PER_PAGE,
            max(1, MAX_CELLS_PER_PAGE // col_count),
        )
        col_end = column_letter(col_count - 1)
        current_row = header_row + 1
        result_rows: list[dict] = []

        while True:
            end_row = current_row + rows_per_page - 1
            response = await self.read_range(
                file_id,
                sheet_id,
                f"A{current_row}:{col_end}{end_row}",
            )
            raw_rows = self._raw_rows(response)
            raw_count = len(raw_rows)
            if raw_count == 0:
                break

            for offset, raw_row in enumerate(raw_rows):
                values, metadata = self._decode_row(raw_row, column_names)
                if not any(values.values()):
                    continue
                result_rows.append({
                    "physical_row": current_row + offset,
                    "values": values,
                    "cell_meta": metadata,
                })

            # 终止条件必须使用 API 原始行数，不能使用过滤空白行后的数量。
            if raw_count < rows_per_page:
                break
            current_row = end_row + 1

        return result_rows

    @staticmethod
    def _raw_rows(response: dict) -> list:
        raw_rows = (response.get("gridData") or {}).get("rows", [])
        if not raw_rows:
            raw_rows = response.get("data", [])
        return raw_rows if isinstance(raw_rows, list) else []

    @staticmethod
    def _decode_cell(cell) -> tuple[str, dict]:
        cv = cell.get("cellValue") if isinstance(cell, dict) else None
        if cv is None:
            return "", {"type": "text"}
        if not isinstance(cv, dict):
            return (str(cv) if cv else ""), {"type": "text"}
        if "text" in cv:
            return (str(cv.get("text") or ""), {"type": "text"})
        if "number" in cv:
            number = cv.get("number")
            return ("" if number is None else str(number), {"type": "number"})
        if "select" in cv:
            select = cv.get("select") or {}
            selected = select.get("value") or []
            options = select.get("options") or []
            by_id = {str(option.get("id")): str(option.get("text") or "") for option in options}
            texts = [by_id.get(str(value), str(value)) for value in selected]
            multiple = bool(select.get("multiple"))
            value = ",".join(texts) if multiple else (texts[0] if texts else "")
            return value, {
                "type": "select",
                "multiple": multiple,
                "options": options,
            }
        return "", {"type": "text"}

    def _decode_row(
        self,
        raw_row,
        column_names: list[str],
    ) -> tuple[dict[str, str], dict[str, dict]]:
        cells = raw_row.get("values", []) if isinstance(raw_row, dict) else raw_row
        if not isinstance(cells, list):
            cells = []
        values: dict[str, str] = {}
        metadata: dict[str, dict] = {}
        for index, column in enumerate(column_names):
            cell = cells[index] if index < len(cells) else None
            if isinstance(raw_row, list):
                value = "" if cell is None else str(cell)
                meta = {"type": "text"}
            else:
                value, meta = self._decode_cell(cell)
            values[column] = value.strip()
            metadata[column] = meta
        return values, metadata

    def _extract_rows(self, response: dict, column_names: list[str] | None = None) -> list[dict]:
        """从 API 响应中提取数据行，映射为字典

        支持两种响应格式：
        - v3: {"gridData": {"rows": [{"values": [{"cellValue": {"text": "xxx"}}, ...]}]}}
        - 旧: {"data": [["val1", "val2", ...]]}
        """
        if column_names is None:
            column_names = COLUMNS

        raw_rows = self._raw_rows(response)
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

    async def read_source_row(
        self,
        file_id: str,
        sheet_id: str,
        physical_row: int,
        column_names: list[str],
    ) -> dict:
        col_end = column_letter(len(column_names) - 1)
        response = await self.read_range(
            file_id,
            sheet_id,
            f"A{physical_row}:{col_end}{physical_row}",
        )
        raw_rows = self._raw_rows(response)
        raw_row = raw_rows[0] if raw_rows else []
        values, metadata = self._decode_row(raw_row, column_names)
        return {
            "physical_row": physical_row,
            "values": values,
            "cell_meta": metadata,
        }

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

    def build_update_cell_request(
        self,
        sheet_id: str,
        physical_row: int,
        column_index: int,
        value: str,
        metadata: dict | None = None,
    ) -> dict:
        """按原单元格类型构建单格更新，物理行号为一基。"""
        metadata = metadata or {"type": "text"}
        cell_type = metadata.get("type")
        cell_value: dict
        if cell_type == "number" and str(value).strip():
            try:
                number = float(str(value).strip())
            except ValueError as exc:
                raise ValueError("该单元格必须填写数字") from exc
            cell_value = {"number": number}
        elif cell_type == "select":
            options = metadata.get("options") or []
            by_text = {str(option.get("text") or ""): option for option in options}
            requested = (
                [part.strip() for part in str(value).split(",") if part.strip()]
                if metadata.get("multiple")
                else ([str(value).strip()] if str(value).strip() else [])
            )
            missing = [item for item in requested if item not in by_text]
            if missing:
                raise ValueError(f"无效的下拉选项: {', '.join(missing)}")
            cell_value = {
                "select": {
                    "value": [by_text[item].get("id") for item in requested],
                    "options": options,
                    "multiple": bool(metadata.get("multiple")),
                }
            }
        else:
            cell_value = {"text": str(value)}
        return {
            "updateRangeRequest": {
                "sheetId": sheet_id,
                "gridData": {
                    "startRow": physical_row - 1,
                    "startColumn": column_index,
                    "rows": [{"values": [{"cellValue": cell_value}]}],
                },
            }
        }

    @staticmethod
    def build_insert_row_request(sheet_id: str, physical_row: int) -> dict:
        return {
            "insertDimensionRequest": {
                "sheetId": sheet_id,
                "dimension": "ROWS",
                "startIndex": physical_row - 1,
                "endIndex": physical_row,
            }
        }

    @staticmethod
    def build_delete_row_request(sheet_id: str, physical_row: int) -> dict:
        return {
            "deleteDimensionRequest": {
                "sheetId": sheet_id,
                "dimension": "ROWS",
                "startIndex": physical_row - 1,
                "endIndex": physical_row,
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
