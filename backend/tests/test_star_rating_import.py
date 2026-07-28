from datetime import datetime, timedelta
from decimal import Decimal
from io import BytesIO
import unittest

from openpyxl import Workbook

from services.star_rating_import import (
    MATCH_WINDOW_SECONDS,
    STAR_RATING_HEADERS,
    VisitCandidate,
    choose_star_rating_matches,
    import_star_rating_workbook,
    parse_star_rating_workbook,
)


def rating_row(
    *,
    address="长板路 1 号",
    score=95,
    star_level="五星出租房\r\n",
    collected_at=datetime(2026, 7, 24, 10, 0),
):
    return [
        "滨湖派出所",
        "长板社区",
        address,
        score,
        star_level,
        collected_at,
        "无",
    ]


def workbook_bytes(rows, *, headers=STAR_RATING_HEADERS):
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "星级评定"
    worksheet.append(list(headers))
    for row in rows:
        worksheet.append(row)
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


class StarRatingWorkbookTests(unittest.TestCase):
    def test_parses_numeric_score_and_cleans_star_level(self):
        parsed = parse_star_rating_workbook(
            workbook_bytes([rating_row()]),
            "Asia/Shanghai",
        )

        self.assertEqual(len(parsed.rows), 1)
        rating = parsed.rows[0]
        self.assertEqual(rating.score, Decimal("95"))
        self.assertEqual(rating.star_level, "五星出租房")
        self.assertEqual(rating.community, "长板")
        self.assertEqual(rating.collected_at, datetime(2026, 7, 24, 2, 0))

    def test_invalid_score_and_star_level_are_skipped(self):
        parsed = parse_star_rating_workbook(
            workbook_bytes(
                [
                    rating_row(score="不是数字"),
                    rating_row(address="另一个地址", star_level="五星"),
                ]
            ),
            "Asia/Shanghai",
        )

        self.assertEqual(parsed.rows, [])
        self.assertEqual(
            sum(issue.severity == "error" for issue in parsed.issues),
            2,
        )

    def test_same_address_and_collection_time_uses_later_file_row(self):
        parsed = parse_star_rating_workbook(
            workbook_bytes(
                [
                    rating_row(score=90),
                    rating_row(score=95),
                ]
            ),
            "Asia/Shanghai",
        )

        self.assertEqual(len(parsed.rows), 1)
        self.assertEqual(parsed.rows[0].score, Decimal("95"))
        self.assertEqual(parsed.ignored_rows, 1)


class StarRatingMatchTests(unittest.TestCase):
    def setUp(self):
        self.rating = parse_star_rating_workbook(
            workbook_bytes([rating_row()]),
            "Asia/Shanghai",
        ).rows[0]

    def candidate(self, candidate_id: int, visit_at: datetime):
        return VisitCandidate(
            id=candidate_id,
            address_key=self.rating.address_key,
            visit_at=visit_at,
            existing_star_values=None,
            existing_time_difference_seconds=None,
        )

    def test_exactly_twenty_four_hours_is_allowed(self):
        candidate = self.candidate(
            1,
            self.rating.collected_at - timedelta(
                seconds=MATCH_WINDOW_SECONDS
            ),
        )

        matches, issues, unmatched, ambiguous = choose_star_rating_matches(
            [self.rating],
            [candidate],
        )

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].time_difference_seconds, 86_400)
        self.assertEqual((issues, unmatched, ambiguous), ([], 0, 0))

    def test_more_than_twenty_four_hours_is_unmatched(self):
        candidate = self.candidate(
            1,
            self.rating.collected_at - timedelta(
                seconds=MATCH_WINDOW_SECONDS + 1
            ),
        )

        matches, issues, unmatched, ambiguous = choose_star_rating_matches(
            [self.rating],
            [candidate],
        )

        self.assertEqual(matches, [])
        self.assertEqual(unmatched, 1)
        self.assertEqual(ambiguous, 0)
        self.assertEqual(issues[0].code, "star_rating_visit_not_found")

    def test_nearest_visit_is_selected(self):
        farther = self.candidate(
            1,
            self.rating.collected_at - timedelta(hours=3),
        )
        nearer = self.candidate(
            2,
            self.rating.collected_at - timedelta(minutes=20),
        )

        matches, _, _, _ = choose_star_rating_matches(
            [self.rating],
            [farther, nearer],
        )

        self.assertEqual(matches[0].visit.id, 2)

    def test_equal_distance_between_visits_is_ambiguous(self):
        before = self.candidate(
            1,
            self.rating.collected_at - timedelta(hours=1),
        )
        after = self.candidate(
            2,
            self.rating.collected_at + timedelta(hours=1),
        )

        matches, issues, unmatched, ambiguous = choose_star_rating_matches(
            [self.rating],
            [before, after],
        )

        self.assertEqual(matches, [])
        self.assertEqual(unmatched, 0)
        self.assertEqual(ambiguous, 1)
        self.assertEqual(issues[0].code, "star_rating_visit_ambiguous")


class StarImportCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.connection.executed.append((normalized, params))
        if normalized == "SELECT id, name FROM _communities":
            self.rows = [(1, "长板")]
        elif normalized.startswith("SELECT a.alias, c.name"):
            self.rows = []
        elif (
            normalized.startswith("SELECT id, `_address_key`, `入户时间`")
            and "FROM t_visit_details" in normalized
        ):
            self.rows = list(self.connection.visit_candidates)
        elif normalized.startswith(
            "SELECT MIN(`星级采集日期`), MAX(`星级采集日期`)"
        ):
            self.rows = [(None, None)]
        else:
            self.rows = []

    async def executemany(self, sql, params):
        normalized = " ".join(sql.split())
        values = list(params)
        self.connection.executed_many.append((normalized, values))
        if normalized.startswith("UPDATE t_visit_details"):
            self.connection.visit_updates.extend(values)
        elif normalized.startswith("INSERT INTO _visit_import_issues"):
            self.connection.issue_inserts.extend(values)

    async def fetchone(self):
        return self.rows[0] if self.rows else None

    async def fetchall(self):
        return list(self.rows)


class StarImportConnection:
    def __init__(self, visit_candidates):
        self.visit_candidates = list(visit_candidates)
        self.executed = []
        self.executed_many = []
        self.visit_updates = []
        self.issue_inserts = []
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return StarImportCursor(self)

    async def begin(self):
        return None

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


class StarRatingImportFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_match_updates_existing_visit_without_inserting_business_row(self):
        parsed = parse_star_rating_workbook(
            workbook_bytes([rating_row()]),
            "Asia/Shanghai",
        )
        rating = parsed.rows[0]
        connection = StarImportConnection(
            [
                (
                    31,
                    rating.address_key,
                    rating.collected_at - timedelta(minutes=30),
                    *([None] * 11),
                )
            ]
        )

        result = await import_star_rating_workbook(
            connection,
            batch_id=9,
            parsed=parsed,
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["inserted_rows"], 1)
        self.assertEqual(result["matched_rows"], 1)
        self.assertEqual(len(connection.visit_updates), 1)
        self.assertEqual(connection.visit_updates[0][-1], 31)
        self.assertFalse(
            any(
                sql.startswith("INSERT INTO t_visit_details")
                for sql, _ in connection.executed_many
            )
        )
        self.assertTrue(connection.committed)
        self.assertFalse(connection.rolled_back)

    async def test_unmatched_rating_is_reported_without_business_row(self):
        parsed = parse_star_rating_workbook(
            workbook_bytes([rating_row()]),
            "Asia/Shanghai",
        )
        connection = StarImportConnection([])

        result = await import_star_rating_workbook(
            connection,
            batch_id=10,
            parsed=parsed,
        )

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["unmatched_rows"], 1)
        self.assertEqual(connection.visit_updates, [])
        self.assertEqual(connection.issue_inserts[0][2], "star_rating_visit_not_found")


if __name__ == "__main__":
    unittest.main()
