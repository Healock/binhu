from datetime import date
import unittest

from services.inferred_attendance import build_suggestions


DAY = date(2026, 10, 25)


def member(member_id=1, name="张三", position="组员"):
    return {
        "id": member_id,
        "name": name,
        "position": position,
        "communities": {"江城"},
    }


def result(*, members, daily=None, periods=None, evidence=None, online=None):
    return build_suggestions(
        start_date=DAY,
        end_date=DAY,
        members=members,
        daily=daily or {},
        periods=periods or {},
        duty_positions={"组员"},
        evidence=evidence or {},
        online_unusable_by_date=online or {},
    )


class InferredAttendanceTests(unittest.TestCase):
    def test_explicit_work_is_official_even_without_evidence(self):
        output = result(
            members=[member()],
            daily={(1, DAY): "work"},
        )
        row = output["rows"][0]
        self.assertEqual(row["official_status"], "work")
        self.assertEqual(row["inferred_status"], "work")
        self.assertEqual(row["suggestion_state"], "official_work")
        self.assertEqual(output["summary"]["official_person_days"], 1)
        self.assertEqual(output["summary"]["suggested_person_days"], 0)

    def test_rest_with_stable_visit_evidence_produces_proposal(self):
        output = result(
            members=[member()],
            daily={(1, DAY): "rest"},
            evidence={(DAY, 1): {"visit_count": 2, "communities": ["江城"]}},
        )
        row = output["rows"][0]
        self.assertEqual(row["official_status"], "rest")
        self.assertEqual(row["inferred_status"], "work")
        self.assertEqual(row["suggestion_state"], "suggested_work")
        self.assertEqual(output["summary"]["suggested_person_days"], 1)

    def test_unassigned_without_evidence_remains_pending(self):
        output = result(members=[member()])
        row = output["rows"][0]
        self.assertEqual(row["official_status"], "unassigned")
        self.assertIsNone(row["inferred_status"])
        self.assertEqual(row["suggestion_state"], "needs_confirmation")
        self.assertEqual(output["summary"]["needs_confirmation"], 1)

    def test_leave_has_priority_and_visit_is_a_conflict(self):
        output = result(
            members=[member()],
            periods={
                1: [
                    {
                        "absence_type": "temporary_leave",
                        "start_date": DAY,
                        "end_date": DAY,
                        "is_active": True,
                    }
                ]
            },
            evidence={(DAY, 1): {"visit_count": 1, "communities": ["江城"]}},
        )
        row = output["rows"][0]
        self.assertEqual(row["official_status"], "leave")
        self.assertEqual(row["inferred_status"], "leave")
        self.assertEqual(row["suggestion_state"], "leave_conflict")
        self.assertEqual(output["summary"]["leave_conflicts"], 1)
        self.assertEqual(output["summary"]["official_person_days"], 0)

    def test_name_only_online_rows_are_never_personal_evidence(self):
        output = result(
            members=[member()],
            online={DAY: 4},
        )
        row = output["rows"][0]
        self.assertEqual(row["suggestion_state"], "needs_confirmation")
        self.assertEqual(row["evidence_summary"]["visit_count"], 0)
        self.assertEqual(row["evidence_summary"]["online_rows_not_personalized"], 4)
        self.assertEqual(output["summary"]["online_rows_not_personalized"], 4)

    def test_same_person_day_is_emitted_once(self):
        output = result(
            members=[member()],
            evidence={(DAY, 1): {"visit_count": 3, "communities": ["江城", "水秀"]}},
        )
        self.assertEqual(len(output["rows"]), 1)
        self.assertEqual(output["rows"][0]["evidence_summary"]["visit_count"], 3)


if __name__ == "__main__":
    unittest.main()
