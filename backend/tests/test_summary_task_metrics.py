from services.summary_task_metrics import aggregate_summary_task_facts


def test_shared_summary_metrics_cover_carryover_and_state_counts():
    community_totals, checker_totals = aggregate_summary_task_facts([
        {
            "community": "社区甲", "checker": "核查员甲",
            "state": "unchecked", "count": 2,
            "carryover": True, "new": False, "changed": False,
            "unable_to_verify": False,
        },
        {
            "community": "社区甲", "checker": "核查员乙",
            "state": "completed", "count": 1,
            "carryover": False, "new": True, "changed": False,
            "unable_to_verify": False,
        },
        {
            "community": "社区甲", "checker": "核查员乙",
            "state": "checked", "count": 1,
            "carryover": False, "new": False, "changed": True,
            "unable_to_verify": True,
        },
    ])

    assert community_totals["社区甲"] == {
        "total": 4,
        "carryover": 2,
        "new": 1,
        "changed": 1,
        "pending": 3,
        "unchecked": 2,
        "checked": 1,
        "completed": 1,
        "unable_to_verify": 1,
    }
    assert checker_totals["社区甲"]["核查员乙"]["completed"] == 1
    assert checker_totals["社区甲"]["核查员乙"]["changed"] == 1


def test_completed_carryover_is_not_counted_as_pending_carryover():
    community_totals, _ = aggregate_summary_task_facts([
        {
            "community": "社区甲", "checker": "核查员甲",
            "state": "completed", "count": 1,
            "carryover": True, "new": False, "changed": True,
            "unable_to_verify": False,
        },
    ])

    assert community_totals["社区甲"]["total"] == 1
    assert community_totals["社区甲"]["carryover"] == 0
    assert community_totals["社区甲"]["completed"] == 1
