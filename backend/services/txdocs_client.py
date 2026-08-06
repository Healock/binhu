"""腾讯文档 OpenAPI v3 客户端

封装对 docs.qq.com 的 HTTP 调用，包括：
- 读取表格数据（分页）
- 批量写入数据
- 确保子表存在
"""

import asyncio
import json
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


class TxDocsAPIError(RuntimeError):
    """腾讯文档返回了 HTTP 或业务层错误。"""

    def __init__(self, message: str, *, code: str | int | None = None):
        self.code = code
        prefix = f"腾讯文档接口错误 {code}" if code not in (None, "") else "腾讯文档接口错误"
        super().__init__(f"{prefix}：{message or '未知错误'}")


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
                try:
                    payload = resp.json()
                except (json.JSONDecodeError, ValueError) as exc:
                    if resp.status_code >= 500 and attempt < max_retries - 1:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    raise TxDocsAPIError(
                        f"HTTP {resp.status_code} 返回了无法解析的响应"
                    ) from exc
                if resp.status_code >= 400:
                    if resp.status_code >= 500 and attempt < max_retries - 1:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    raise self._api_error(payload, http_status=resp.status_code)
                business_error = self._business_error(payload)
                if business_error is not None:
                    raise business_error
                # 请求间延迟，避免限频
                await asyncio.sleep(settings.API_RATE_LIMIT_DELAY_MS / 1000)
                return payload
            except httpx.RequestError:
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise
        raise Exception(f"请求失败，已重试 {max_retries} 次: {url}")

    @staticmethod
    def _api_error(payload: object, *, http_status: int | None = None) -> TxDocsAPIError:
        code: str | int | None = http_status
        message = "请求失败"
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                code = error.get("code", code)
                message = str(error.get("message") or error.get("msg") or message)
            else:
                code = payload.get("code", payload.get("errorCode", code))
                message = str(payload.get("message") or payload.get("msg") or message)
        return TxDocsAPIError(message[:500], code=code)

    @classmethod
    def _business_error(cls, payload: object) -> TxDocsAPIError | None:
        if not isinstance(payload, dict):
            return None
        error = payload.get("error")
        if error:
            return cls._api_error(payload)
        for key in ("code", "errorCode", "error_code", "errcode", "retcode", "ret"):
            if key not in payload or payload[key] in (None, ""):
                continue
            raw_code = payload[key]
            try:
                success = int(raw_code) in {0, 200}
            except (TypeError, ValueError):
                success = str(raw_code).strip().lower() in {"ok", "success"}
            if not success:
                return cls._api_error(payload)
        return None

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

    async def resolve_column_layout(
        self,
        file_id: str,
        sheet_id: str,
        header_row: int,
        layouts: list[list[str]],
    ) -> list[str]:
        """根据真实表头选择兼容的物理列布局。"""
        if not layouts:
            raise ValueError("未配置腾讯表格列布局")
        if len(layouts) == 1:
            return list(layouts[0])

        max_columns = max(len(layout) for layout in layouts)
        response = await self.read_range(
            file_id,
            sheet_id,
            f"A{header_row}:{column_letter(max_columns - 1)}{header_row}",
        )
        raw_rows = self._raw_rows(response)
        raw_row = raw_rows[0] if raw_rows else []
        cells = raw_row.get("values", []) if isinstance(raw_row, dict) else raw_row
        if not isinstance(cells, list):
            cells = []
        headers = []
        for cell in cells:
            if isinstance(raw_row, list):
                value = "" if cell is None else str(cell)
            else:
                value, _ = self._decode_cell(cell)
            headers.append("".join(value.split()))

        def score(layout: list[str]) -> int:
            matched = 0
            for index, expected in enumerate(layout):
                if index >= len(headers):
                    continue
                actual = headers[index]
                normalized = "".join(str(expected).split())
                if actual == normalized or (
                    normalized == "身份证号"
                    and actual in {"身份证号码", "身份证"}
                ):
                    matched += 1
            return matched

        ranked = sorted(
            ((score(layout), index, layout) for index, layout in enumerate(layouts)),
            key=lambda item: (-item[0], item[1]),
        )
        matched, _, selected = ranked[0]
        required_matches = min(3, len(selected))
        if matched < required_matches:
            raise ValueError("腾讯表格表头与业务列配置不一致，请检查表头行设置")
        return list(selected)

    async def read_all_source_rows(
        self,
        file_id: str,
        sheet_id: str,
        header_row: int = 1,
        column_names: list[str] | None = None,
        include_detected_headers: bool = False,
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
        row_total = await self.get_sheet_row_total(file_id, sheet_id)

        while True:
            if row_total is not None and current_row > row_total:
                break
            end_row = current_row + rows_per_page - 1
            if row_total is not None:
                end_row = min(end_row, row_total)
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
                is_header = self._looks_like_header(values, column_names)
                if is_header and not include_detected_headers:
                    continue
                result_rows.append({
                    "physical_row": current_row + offset,
                    "values": values,
                    "cell_meta": metadata,
                    "is_header": is_header,
                })

            # 终止条件必须使用 API 原始行数，不能使用过滤空白行后的数量。
            requested_rows = end_row - current_row + 1
            if raw_count < requested_rows:
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

    @staticmethod
    def _looks_like_header(values: dict[str, str], column_names: list[str]) -> bool:
        """识别错误配置后混入数据区的重复表头。"""
        nonempty = 0
        matches = 0
        for column in column_names:
            value = "".join(str(values.get(column, "") or "").split())
            if not value:
                continue
            nonempty += 1
            expected = "".join(str(column).split())
            if value == expected or (
                expected == "身份证号" and value in {"身份证号码", "身份证"}
            ):
                matches += 1
        return matches >= 3 and matches * 2 >= nonempty

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
            # 腾讯的范围读取接口只返回浮点值，不返回原始输入或数字显示格式。
            # 例如数值单元格 7.30 和 7.3 都会成为 7.3，不能靠补零猜回原值；
            # 保留接口值可避免把真实的 8.3 误改成 8.30。需要保留尾零时，
            # 来源表必须把该单元格设置为文本，让接口通过 cellValue.text 返回。
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
        cell_type = metadata.get("write_type") or metadata.get("type")
        cell_value: dict
        if cell_type == "number" and str(value).strip():
            try:
                number = float(str(value).strip())
            except ValueError as exc:
                raise ValueError("该单元格必须填写数字") from exc
            cell_value = {"number": number}
        elif cell_type == "select":
            options = metadata.get("write_options") or metadata.get("options") or []
            by_text = {str(option.get("text") or ""): option for option in options}
            requested = (
                [part.strip() for part in str(value).split(",") if part.strip()]
                if metadata.get("write_multiple", metadata.get("multiple"))
                else ([str(value).strip()] if str(value).strip() else [])
            )
            missing = [item for item in requested if item not in by_text]
            if missing:
                raise ValueError(f"无效的下拉选项: {', '.join(missing)}")
            cell_value = {
                "select": {
                    "value": [by_text[item].get("id") for item in requested],
                    "options": options,
                    "multiple": bool(
                        metadata.get("write_multiple", metadata.get("multiple"))
                    ),
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
    def build_delete_row_request(sheet_id: str, physical_row: int) -> dict:
        return {
            "deleteDimensionRequest": {
                "sheetId": sheet_id,
                "dimension": "ROW",
                "startIndex": physical_row,
                "endIndex": physical_row + 1,
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

    async def get_sheet_row_total(self, file_id: str, sheet_id: str) -> int | None:
        """读取工作表现有总行数，避免请求超出腾讯表格有效区域。"""
        file_info = await self.get_file_info(file_id)
        properties = file_info.get("properties")
        sheets = (
            properties
            if isinstance(properties, list)
            else file_info.get("sheets")
            or (file_info.get("data") or {}).get("sheets")
            or (properties or {}).get("sheets", [])
        )
        for sheet in sheets or []:
            if not isinstance(sheet, dict):
                continue
            sheet_properties = sheet.get("properties") or sheet
            candidate_id = sheet_properties.get("sheetId") or sheet_properties.get("id")
            if str(candidate_id or "") != str(sheet_id):
                continue
            raw_total = sheet_properties.get("rowTotal")
            if raw_total is None:
                raw_total = sheet_properties.get("rowCount")
            try:
                total = int(raw_total)
            except (TypeError, ValueError):
                return None
            return total if total > 0 else None
        return None

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
