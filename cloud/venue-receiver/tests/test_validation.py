import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.validation import normalize_person_name, normalize_address, validate_identity_number, validate_phone_number, validate_public_submission_fields, ValidationError


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
