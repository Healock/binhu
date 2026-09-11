"""Closed transformation of parser-defined task values and local source IDs."""
from __future__ import annotations
import hashlib
import json
import re
from datetime import date, datetime
from .codec import Codec, SnapshotError, enum, normalized

TASK_TYPES = ("全链条", "出租房屋核查", "疑似未注销模型三", "疑似返苏", "寄递业", "苏州涉警", "交通涉警")
IDENTITIES = {"身份证号", "身份证号码", "参考身份证号码"}
PHONES = {"电话号码", "手机号码", "联系方式", "联系号码"}
ADDRESSES = {"地址", "地址1", "房屋地址", "现住址", "高频抓拍小区", "疑似现住址"}
NAMES = {"姓名", "参考姓名"}
DATES = {"下发日期", "下发时间", "截止日期", "截止时间", "创建时间", "出警日期"}
REDACTED = {"来源", "登记情况", "研判", "二次反馈", "二次核查结果", "备注", "出警内容", "出警类别", "出警单位", "参考派出所", "入住方式"}


def result_categories(workflow):
    allowed = set(workflow.result_options) | set(workflow.valid_results)
    if workflow.parser_type == '疑似返苏':
        allowed.add('无需登记，原因写备注')
    return allowed


def business_date(value):
    text = str(value or "").strip()
    if not text:
        return ""
    # Current dispatch writes %m-%d. Validate month/day against a leap year
    # solely to allow February 29; never invent or export an absent year.
    if re.fullmatch(r"\d{1,2}[-.]\d{1,2}", text):
        month, day = re.split(r"[-.]", text)
        try:
            date(2000, int(month), int(day))
        except ValueError:
            raise SnapshotError("invalid_business_date") from None
        return text
    # Preserve supported date precision, and never copy arbitrary free text
    # merely because it was stored in a date-labelled VARCHAR column.
    for pattern, fmt in ((r"\d{4}-\d{2}-\d{2}", "%Y-%m-%d"),
                         (r"\d{4}/\d{1,2}/\d{1,2}", "%Y/%m/%d"),
                         (r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", "%Y-%m-%dT%H:%M:%S"),
                         (r"\d{4}/\d{1,2}/\d{1,2} \d{2}:\d{2}:\d{2}", "%Y/%m/%d %H:%M:%S"),
                         (r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", "%Y-%m-%d %H:%M:%S")):
        if re.fullmatch(pattern, text):
            try:
                datetime.strptime(text, fmt)
            except ValueError:
                raise SnapshotError("invalid_business_date") from None
            return text
    raise SnapshotError("unrecognized_business_date")


def transform_values(parser, workflow, values, community_by_name, codec: Codec):
    if parser.parser_type not in TASK_TYPES or set(values) != set(parser.COLUMNS):
        raise SnapshotError("task_value_contract_mismatch")
    raw_community = values[parser.COMMUNITY_COLUMN]
    try:
        community_id = community_by_name[normalized(raw_community)]
    except KeyError:
        raise SnapshotError("task_community_unresolved") from None
    community = codec.reference("community", community_id, nullable=False)
    codec.remember(raw_community)
    result = {}
    for field, raw in values.items():
        if not isinstance(raw, (str, type(None))):
            raise SnapshotError("nontext_task_value")
        value = str(raw or "").strip()
        if field == parser.COMMUNITY_COLUMN:
            result[field] = "验证社区" + str(community)
        elif field in IDENTITIES:
            result[field] = codec.identity(value)
        elif field in PHONES:
            result[field] = codec.phone(value)
        elif field in ADDRESSES:
            result[field] = codec.address(community_id, value)
        elif field in NAMES:
            codec.remember(value)
            identity = values.get("参考身份证号码") if field == "参考姓名" else next((values.get(k) for k in workflow.identity_fields if values.get(k)), None)
            result[field] = codec.text("person", identity or value, "验证人员") if value else ""
        elif field == "核查人":
            result[field] = codec.text("staff", value, "验证核查员")
        elif field in DATES:
            result[field] = business_date(value)
        elif field == workflow.result_field:
            result[field] = enum(value, result_categories(workflow))
        elif field == "接警编号":
            result[field] = codec.text("case_reference", value, "staging-case-")
        elif field in REDACTED:
            codec.remember(value)
            result[field] = "脱敏验证内容" if value else ""
        else:
            raise SnapshotError("unreviewed_task_field")
    codec.assert_tables_safe({'OnlineData.' + parser.table_name: [result]})
    return result


def source_record(parser, source, safe_values, codec: Codec):
    if set(source) not in ({"id", "physical_row", "revision", "row_key"},
                           {"id", "physical_row", "revision", "row_key", "source_kind"}):
        raise SnapshotError("source_column_contract_mismatch")
    if not isinstance(source["revision"], int) or not 1 <= source["revision"] <= 2**63-1:
        raise SnapshotError("invalid_source_revision")
    row_key = parser.make_row_key(safe_values)
    values_json = json.dumps(safe_values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    row_hash = hashlib.sha256(values_json.encode("utf-8")).hexdigest()
    task_id = codec.reference(parser.table_name, source["physical_row"], nullable=False)
    source_id = codec.reference("source", source["id"], nullable=False)
    reference = f"{parser.table_name}:{task_id}"
    source_kind = source.get("source_kind", "local_table")
    if source_kind not in {"local_table", "local_dispatch", "one_time_continuation_import"}:
        raise SnapshotError("unsupported_source_kind")
    return {"task": {"id": task_id, "_row_key": row_key, **safe_values},
            "source": {"id": source_id, "spreadsheet_id": 0, "parser_type": parser.parser_type,
                "sheet_id": "local:" + parser.parser_type, "physical_row": task_id,
                "row_key": row_key, "row_hash": row_hash, "values_json": values_json,
                "cell_meta_json": "{}", "revision": source["revision"],
                "source_kind": source_kind, "source_ref": reference, "archived_at": None},
            "local_record": {"parser_type": parser.parser_type, "local_task_id": task_id,
                "business_key": row_key, "source_kind": source_kind, "source_ref": reference,
                "values_json": values_json, "content_hash": row_hash, "revision": source["revision"], "status": "active"}}
