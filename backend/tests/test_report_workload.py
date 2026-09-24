import asyncio

from services.report_workload import load_effective_workload_by_community


class Cursor:
    def __init__(self):
        self.query = ""
        self.params = None

    async def execute(self, query, params=None):
        self.query = query
        self.params = params

    async def fetchall(self):
        return [("社区甲", 7), ("社区乙", 2)]


def test_workload_aggregation_uses_local_and_external_sources():
    cursor = Cursor()
    result = asyncio.run(load_effective_workload_by_community(
        cursor, "2026-09-19", "2026-09-23", ["全链条"]
    ))
    assert result == {"社区甲": 7, "社区乙": 2}
    assert "UNION ALL" in cursor.query
    assert "_txdocs_monitor_workload_ledger" in cursor.query
    assert cursor.params[:2] == ("2026-09-19", "2026-09-23")
