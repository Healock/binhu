from services.registry_property_identity import current_household_snapshot


def snapshot(number, status, when, **changes):
    return dict(community_id=8, address="合成路8号301室", house_no=number,
                household_status=status, updated_at=when, **changes)


def test_registration_number_changes_do_not_change_physical_house():
    old = snapshot("OLD", "已注销", "2020-01-01")
    current = snapshot("NEW", "未注销", "2026-01-01")
    assert current_household_snapshot([old, current]) is current
    assert current_household_snapshot([current, old]) is current


def test_ambiguous_shared_addresses_never_guess_an_identity():
    old = snapshot("OLD", "已注销", "2020-01-01")
    current = snapshot("NEW", "未注销", "2026-01-01")
    for change in ({"community_id": 9}, {"address": "合成路8号302室"},
                   {"updated_at": None}, {"updated_at": "2026-01-01"},
                   {"updated_at": "2027-01-01"}, {"household_status": "未注销"},
                   {"household_status": ""}, {"house_no": ""}):
        assert current_household_snapshot([{**old, **change}, current]) is None
