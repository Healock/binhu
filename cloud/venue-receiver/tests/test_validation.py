import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.validation import (
    ValidationError,
    normalize_address,
    normalize_person_name,
    validate_drinking_report_fields,
    validate_identity_number,
    validate_phone_number,
    validate_public_submission_fields,
    validate_signature_strokes,
)


def test_identity_number_requires_valid_checksum():
    assert validate_identity_number("11010519491231002x") == "11010519491231002X"
    try:
        validate_identity_number("110105194912310020")
    except ValidationError as exc:
        assert exc.field == "identity_number"
    else:
        raise AssertionError("invalid checksum accepted")


def test_phone_requires_mainland_mobile_segment():
    assert validate_phone_number(" 138 0000 0000 ") == "13800000000"
    for value in ("12800000000", "1380000000", "13800000000x", "１３８００００００００"):
        try:
            validate_phone_number(value)
        except ValidationError as exc:
            assert exc.field == "phone"
        else:
            raise AssertionError(f"invalid phone accepted: {value}")


def test_name_and_address_are_normalized_without_rewriting_content():
    assert normalize_person_name("  张  三  ") == "张 三"
    assert normalize_address("  江苏 省  滨湖区  ") == "江苏 省 滨湖区"


def test_control_characters_and_empty_values_rejected():
    for fn, field in ((normalize_person_name, "name"), (normalize_address, "address")):
        try:
            fn("张\x00三")
        except ValidationError as exc:
            assert exc.field == field
        else:
            raise AssertionError("control character accepted")
        for value in ("张\n三", "张\t三"):
            try:
                fn(value)
            except ValidationError as exc:
                assert exc.field == field
            else:
                raise AssertionError("whitespace control character accepted")


def test_public_submission_validation_returns_normalized_values():
    result = validate_public_submission_fields(
        name=" 测试 人员 ",
        identity_number="11010519491231002x",
        phone="138-0000-0000",
        address=" 测试  地址 ",
    )
    assert result == {
        "name": "测试 人员",
        "identity_number": "11010519491231002X",
        "phone": "13800000000",
        "address": "测试 地址",
    }


def test_drinking_report_fields_are_normalized_and_time_becomes_utc():
    result = validate_drinking_report_fields(
        {
            "name": " 测试 人员 ",
            "unit_position": " 某单位  民警 ",
            "drinking_at": "2026-09-16T20:30",
            "drinking_place": " 测试 地点 ",
            "reason": " 家庭 聚会 ",
            "inviter": " 测试 邀约人 ",
            "travel_method": " 公共交通 ",
            "responsible_leader_name": " 测试 领导 ",
            "notes": " 无 ",
        },
        timezone_name="Asia/Shanghai",
    )
    assert result["drinking_at"] == "2026-09-16T12:30:00Z"
    assert result["name"] == "测试 人员"


def test_signature_rejects_taps_and_accepts_real_movement():
    for value in ([[{"x": 0.1, "y": 0.1}]], [[{"x": 0.1, "y": 0.1}, {"x": 0.1, "y": 0.1}]]):
        try:
            validate_signature_strokes(value, field="reporter_signature", label="报备人签名")
        except ValidationError as exc:
            assert exc.field == "reporter_signature"
        else:
            raise AssertionError("tap-only signature accepted")
    assert validate_signature_strokes(
        [[{"x": 0.1, "y": 0.1}, {"x": 0.5, "y": 0.5}]],
        field="reporter_signature",
        label="报备人签名",
    )[0][1] == {"x": 0.5, "y": 0.5}
