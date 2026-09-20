from services.parsers import PARSER_REGISTRY, get_parser


def test_suspect_missing_registration_parser_is_registered_with_business_key():
    parser = get_parser("疑似漏登记")
    assert PARSER_REGISTRY["疑似漏登记"] is type(parser)
    assert parser.table_name == "t_suspect_missing_registration"
    assert parser.get_business_key() == ["身份证号", "下发日期"]
    assert parser.community_value({"社区": " 长板 "}) == "长板"


def test_contact_normalization_keeps_only_mobile_numbers_and_discards_timestamp():
    parser = get_parser("疑似漏登记")
    primary_mobile = "138" + "0" * 8
    backup_mobile = "139" + "0" * 7 + "1"
    values = parser.normalize_source_row({
        "下发日期": "2026-09-20",
        "社区": "虚构社区",
        "身份证号": "SYNTHETIC-ID-001",
        "联系方式": f"2026-09-20 09:30，虚构人员 {primary_mobile}；备用 {backup_mobile}",
        "地址": "虚构路1号",
    })
    assert values["联系方式"] == f"{primary_mobile}、{backup_mobile}"
    assert "2026" not in values["联系方式"]


def test_contact_normalization_does_not_copy_non_mobile_text():
    parser = get_parser("疑似漏登记")
    values = parser.normalize_source_row({"联系方式": "无电话（2026-09-20）"})
    assert values["联系方式"] == ""


def test_source_address_alias_is_normalized_to_canonical_address_column():
    parser = get_parser("疑似漏登记")
    values = parser.normalize_source_row({"地址，小区名": "长板社区一号"})
    assert values["地址"] == "长板社区一号"
