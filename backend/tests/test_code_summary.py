from __future__ import annotations

import os
from datetime import date

import pytest
from pydantic import ValidationError

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers.code_summaries import (
    DateRangeRequest,
    _commit_source,
    _insert_failed_run,
    _json_value,
    _total,
    router,
)
from services.code_summary import (
    aggregate_rows,
    classify_terminal,
    count_fullchain_registrations,
)
from services.code_summary import CodeSummaryError


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.lastrowid = 0
        self.result = None
        self.results = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.connection.executed_sql.append(normalized)
        if normalized.startswith("INSERT INTO _code_summary_runs"):
            self.connection.next_run_id += 1
            self.lastrowid = self.connection.next_run_id
        elif normalized.startswith("SELECT `创建时间` FROM t_fullchain"):
            if self.connection.fullchain_query_error:
                raise RuntimeError("database unavailable")
            self.results = [(value,) for value in self.connection.fullchain_created_times]
        elif normalized.startswith("SELECT COALESCE(MAX(version_no),0)"):
            self.result = (self.connection.versions.get((params[0], params[1]), 0),)
        elif normalized.startswith("INSERT INTO _code_daily_snapshots"):
            key = (params[0], params[1])
            self.connection.versions[key] = params[2]
            self.connection.snapshots.append(params)

    async def fetchone(self):
        return self.result

    async def fetchall(self):
        return self.results


class FakeConnection:
    def __init__(self):
        self.next_run_id = 0
        self.versions = {}
        self.snapshots = []
        self.begin_count = 0
        self.commit_count = 0
        self.rollback_count = 0
        self.fullchain_created_times = []
        self.fullchain_query_error = False
        self.executed_sql = []

    def cursor(self):
        return FakeCursor(self)

    async def begin(self):
        self.begin_count += 1

    async def commit(self):
        self.commit_count += 1

    async def rollback(self):
        self.rollback_count += 1


def _peace(identity: str, terminal: str, population: str, time: str, **extra):
    return {
        "idCard": identity,
        "terminal": terminal,
        "location": extra.pop("location", terminal),
        "population": population,
        "comparisonTime": time,
        "updateDate": extra.pop("updateDate", time),
        "id": extra.pop("id", "1"),
        "assignJgmc": "滨湖新城派出所",
        **extra,
    }


def test_terminal_classifier_prioritizes_halls_directories_and_shape_fallback():
    assert classify_terminal("滨湖所接警大厅", personnel_names=set(), place_names=set()) == "dispatch_hall"
    assert classify_terminal("苏州湾大厦", personnel_names=set(), place_names=set()) == "household_hall"
    assert classify_terminal("张三", personnel_names={"张三"}, place_names=set()) == "patrol"
    assert classify_terminal("青云小区", personnel_names=set(), place_names={"青云小区"}) == "social"
    assert classify_terminal("李四", personnel_names=set(), place_names=set()) == "patrol"
    assert classify_terminal("长板社区花园", personnel_names=set(), place_names=set()) == "social"
    assert classify_terminal("未知窗口", personnel_names=set(), place_names=set()) == "unclassified"


@pytest.mark.parametrize(
    "location",
    [
        "吴江醍拾贰餐娱馆",
        "苏州吴江聚宝财文化娱乐有限公司",
        "吴江区松陵镇聚星汇歌厅",
        "魏玖KTV",
        "苏州渔歌子文化娱乐有限公司",
        "苏州泰湖荟音乐酒吧有限公司",
    ],
)
def test_location_classifier_prioritizes_social_markers_before_patrol(location):
    assert classify_terminal(
        location, personnel_names={"吴江"}, place_names=set()
    ) == "social"


def test_location_classifier_prioritizes_halls_and_place_directory():
    assert classify_terminal(
        "滨湖所接警大厅", personnel_names=set(), place_names={"滨湖所接警大厅"}
    ) == "dispatch_hall"
    assert classify_terminal(
        "苏州湾大厦", personnel_names=set(), place_names={"苏州湾大厦"}
    ) == "household_hall"
    assert classify_terminal(
        "张三", personnel_names={"张三"}, place_names={"张三"}
    ) == "social"


def test_location_classifier_keeps_ambiguous_values_unclassified():
    assert classify_terminal("未知窗口", personnel_names=set(), place_names=set()) == "unclassified"


def test_peace_summary_deduplicates_by_identity_using_latest_record():
    rows = [
        _peace("110101199001010015", "张三", "流口未登记", "2026-08-18 08:00:00", id="1"),
        _peace("110101199001010015", "滨湖所接警大厅", "流口已登记", "2026-08-18 09:00:00", id="2"),
        _peace("110101199001010023", "长板社区花园", "流口已注销", "2026-08-18 10:00:00", id="3"),
    ]
    result = aggregate_rows(
        "peace", rows, date(2026, 8, 18), date(2026, 8, 18),
        personnel_names={"张三"}, place_names={"长板社区花园"},
    )
    day = result["rows"][0]
    assert day["total_people"] == 2
    assert day["dispatch_hall_scan_count"] == 1
    assert day["patrol_scan_count"] == 0
    assert day["social_scan_count"] == 1
    assert day["instruction_count"] == 1
    assert day["new_registration_count"] == 0
    assert day["duplicate_removed_count"] == 1


def test_peace_summary_uses_location_instead_of_scan_channel():
    result = aggregate_rows(
        "peace",
        [
            _peace(
                "110101199001010015",
                "微信",
                "流口未登记",
                "2026-08-18 08:00:00",
                location="滨湖所接警大厅",
            ),
            _peace(
                "110101199001010023",
                "支付宝扫一扫",
                "流口未登记",
                "2026-08-18 08:00:00",
                location="苏州吴江聚宝财文化娱乐有限公司",
            ),
        ],
        date(2026, 8, 18),
        date(2026, 8, 18),
    )
    day = result["rows"][0]
    assert day["dispatch_hall_scan_count"] == 1
    assert day["social_scan_count"] == 1
    assert day["patrol_scan_count"] == 0


def test_latest_record_uses_numeric_id_as_final_tie_breaker():
    rows = [
        _peace("110101199001010015", "张三", "流口未登记", "2026-08-18 08:00:00", id="2"),
        _peace("110101199001010015", "苏州湾大厦", "流口已登记", "2026-08-18 08:00:00", id="10"),
    ]
    result = aggregate_rows(
        "peace", rows, date(2026, 8, 18), date(2026, 8, 18)
    )
    day = result["rows"][0]
    assert day["household_hall_scan_count"] == 1
    assert day["new_registration_count"] == 0


def test_source_hash_changes_even_when_aggregate_metrics_are_equal():
    first = aggregate_rows(
        "peace",
        [_peace("110101199001010015", "张三", "常口", "2026-08-18 08:00:00")],
        date(2026, 8, 18),
        date(2026, 8, 18),
    )
    second = aggregate_rows(
        "peace",
        [_peace("110101199001010015", "李四", "常口", "2026-08-18 08:00:00")],
        date(2026, 8, 18),
        date(2026, 8, 18),
    )
    assert first["rows"] == second["rows"]
    assert first["source_hash"] != second["source_hash"]


def test_peace_summary_uses_fullchain_registration_counts_when_supplied():
    result = aggregate_rows(
        "peace",
        [_peace("110101199001010015", "张三", "流口未登记", "2026-08-18 08:00:00")],
        date(2026, 8, 18), date(2026, 8, 18),
        new_registration_counts={date(2026, 8, 18): 7},
    )
    assert result["rows"][0]["new_registration_count"] == 7


def test_peace_total_recalculates_registration_rate_without_estimate_fields():
    total = _total("peace", [
        {
            "raw_count": 100,
            "total_people": 80,
            "instruction_count": 20,
            "new_registration_count": 2,
        },
        {
            "raw_count": 200,
            "total_people": 120,
            "instruction_count": 30,
            "new_registration_count": 3,
        },
    ])
    assert total["new_registration_count"] == 5
    assert total["effective_scan_rate"] == 0.025
    assert "new_registration_estimate_ratio" not in total
    assert "new_registration_estimated" not in total


@pytest.mark.asyncio
async def test_fullchain_registration_count_uses_created_date_and_only_safe_fields():
    connection = FakeConnection()
    connection.fullchain_created_times = [
        "2026-08-18 08:00:00",
        "2026-08-18T09:00:00Z",
        "2026-08-19 10:00:00",
        "not-a-date",
    ]
    counts = await count_fullchain_registrations(
        connection, date(2026, 8, 18), date(2026, 8, 18)
    )
    assert counts == {date(2026, 8, 18): 2}
    sql = next(item for item in connection.executed_sql if "t_fullchain" in item)
    assert "SELECT `创建时间`" in sql
    assert "核查结果" in sql
    assert "创建时间" in sql
    assert "身份证" not in sql
    assert "手机号" not in sql


@pytest.mark.asyncio
async def test_fullchain_registration_count_failure_is_safe():
    connection = FakeConnection()
    connection.fullchain_query_error = True
    with pytest.raises(CodeSummaryError, match="暂时无法读取"):
        await count_fullchain_registrations(
            connection, date(2026, 8, 18), date(2026, 8, 18)
        )


def test_manager_summary_counts_unique_accounts_and_instruction_states():
    rows = [
        {
            "zhUserIdCard": "110101199001010015", "gjUserName": "管家甲",
            "population": "流口已注销", "comparisonTime": "2026-08-18 08:00:00",
            "updateDate": "2026-08-18 08:00:00", "id": "1", "pcsdm": "320584710000", "pcsname": "滨湖新城派出所",
        },
        {
            "zhUserIdCard": "110101199001010015", "gjUserName": "管家甲",
            "population": "流口已登记", "comparisonTime": "2026-08-18 09:00:00",
            "updateDate": "2026-08-18 09:00:00", "id": "2", "pcsdm": "320584710000", "pcsname": "滨湖新城派出所",
        },
        {
            "zhUserIdCard": "110101199001010023", "gjUserName": "管家乙",
            "population": "流口未登记", "comparisonTime": "2026-08-18 10:00:00",
            "updateDate": "2026-08-18 10:00:00", "id": "3", "pcsdm": "320584710000", "pcsname": "滨湖新城派出所",
        },
    ]
    result = aggregate_rows("manager", rows, date(2026, 8, 18), date(2026, 8, 18))
    day = result["rows"][0]
    assert day["total_people"] == 2
    assert day["active_accounts"] == 2
    assert day["instruction_count"] == 1
    assert day["duplicate_removed_count"] == 1


def test_invalid_identity_is_excluded_without_failing_the_day():
    result = aggregate_rows(
        "peace",
        [_peace("invalid", "未知", "流口未登记", "2026-08-18 08:00:00")],
        date(2026, 8, 18), date(2026, 8, 18),
    )
    assert result["rows"][0]["total_people"] == 0
    assert result["rows"][0]["excluded_identity_count"] == 1


def test_invalid_source_time_is_skipped_and_reported_without_failing_valid_rows():
    valid = {
        "zhUserIdCard": "110101199001010015", "gjUserName": "管家甲",
        "population": "流口已登记", "comparisonTime": "2026-08-18 08:00:00",
        "updateDate": "2026-08-18 08:00:00", "id": "1",
        "pcsname": "滨湖新城派出所",
    }
    invalid = {
        **valid,
        "zhUserIdCard": "110101199001010023",
        "comparisonTime": None,
        "id": "2",
    }

    result = aggregate_rows(
        "manager", [valid, invalid], date(2026, 8, 18), date(2026, 8, 18)
    )

    assert result["raw_count"] == 2
    assert result["valid_count"] == 1
    assert result["invalid_time_count"] == 1
    assert result["rows"][0]["total_people"] == 1


def test_all_invalid_source_times_still_stop_the_batch():
    row = {
        "zhUserIdCard": "110101199001010015", "gjUserName": "管家甲",
        "population": "流口已登记", "comparisonTime": "",
        "updateDate": "", "id": "1", "pcsname": "滨湖新城派出所",
    }

    with pytest.raises(CodeSummaryError, match="全部不是有效时间"):
        aggregate_rows("manager", [row], date(2026, 8, 18), date(2026, 8, 18))


def test_code_summary_source_quality_count_is_safe_to_expose():
    row = {
        "zhUserIdCard": "110101199001010015", "gjUserName": "管家甲",
        "population": "流口已登记", "comparisonTime": "2026-08-18 08:00:00",
        "updateDate": "2026-08-18 08:00:00", "id": "1",
        "pcsname": "滨湖新城派出所",
    }
    result = aggregate_rows(
        "manager",
        [row],
        date(2026, 8, 18),
        date(2026, 8, 18),
    )
    assert result["invalid_time_count"] == 0


def test_run_summary_exposes_only_safe_quality_counters():
    assert _json_value('{"invalid_time_count":1,"raw_count":25}') == {
        "invalid_time_count": 1,
        "raw_count": 25,
    }
    assert _json_value("not-json") == {}


def test_empty_response_creates_zero_rows_for_each_requested_day():
    result = aggregate_rows(
        "peace", [], date(2026, 8, 17), date(2026, 8, 18)
    )
    assert [row["date"] for row in result["rows"]] == ["2026-08-17", "2026-08-18"]
    assert all(row["raw_count"] == 0 for row in result["rows"])
    assert all(row["total_people"] == 0 for row in result["rows"])


def test_date_range_rejects_more_than_31_days():
    with pytest.raises(ValidationError):
        DateRangeRequest(start_date=date(2026, 7, 1), end_date=date(2026, 8, 1))


def test_routes_use_separate_view_and_fetch_permissions():
    dependencies = {
        route.path: {dependency.call.__name__ for dependency in route.dependant.dependencies}
        for route in router.routes
    }
    assert "require_visit_source_manage" in dependencies["/api/code-summaries/fetch"]
    assert "require_visit_summary_view" in dependencies["/api/code-summaries/search"]


@pytest.mark.asyncio
async def test_duplicate_fetch_increments_daily_snapshot_version():
    connection = FakeConnection()
    payload = DateRangeRequest(start_date=date(2026, 8, 18), end_date=date(2026, 8, 18))
    result = aggregate_rows("peace", [], payload.start_date, payload.end_date)

    await _commit_source(connection, source="peace", user_id=1, payload=payload, result=result)
    await _commit_source(connection, source="peace", user_id=1, payload=payload, result=result)

    assert [item[2] for item in connection.snapshots] == [1, 2]
    assert connection.commit_count == 2
    assert connection.rollback_count == 0


@pytest.mark.asyncio
async def test_failed_source_run_is_committed_without_snapshot():
    connection = FakeConnection()
    payload = DateRangeRequest(start_date=date(2026, 8, 18), end_date=date(2026, 8, 18))

    run_id = await _insert_failed_run(
        connection,
        source="manager",
        user_id=1,
        payload=payload,
        error=CodeSummaryError("timeout", "旧平台码数据请求超时"),
    )

    assert run_id == 1
    assert connection.commit_count == 1
    assert connection.snapshots == []
