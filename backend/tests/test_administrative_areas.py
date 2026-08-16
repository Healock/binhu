import csv
import tempfile
import unittest
from pathlib import Path

from services.administrative_areas import (
    AREA_DATA_PATH,
    AdministrativeArea,
    choose_administrative_area,
    load_administrative_areas,
)


def area(**overrides) -> AdministrativeArea:
    values = {
        "source_row": 2,
        "code": "510904",
        "name": "安居区",
        "level": "county",
        "province": "四川省",
        "city": "遂宁市",
        "parent_code": "510900",
        "path": "四川省/遂宁市/安居区",
        "full_name": "四川省遂宁市安居区",
        "status": "active",
        "start_year": 2003,
        "end_year": None,
        "new_code": "",
        "source": "fixture",
    }
    values.update(overrides)
    return AdministrativeArea(**values)


class AdministrativeAreaTests(unittest.TestCase):
    def test_bundled_csv_keeps_six_digit_code_and_chinese_full_name_separate(self):
        digest, rows = load_administrative_areas(AREA_DATA_PATH)
        matches = [item for item in rows if item.code == "510904"]
        self.assertEqual(len(digest), 64)
        self.assertTrue(matches)
        selected = choose_administrative_area(matches, birth_year=2001)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.code, "510904")
        self.assertEqual(selected.full_name, "四川省遂宁市安居区")

    def test_birth_year_record_wins_before_active_fallback(self):
        historical = area(
            source_row=2, status="retired", start_year=1981, end_year=2002,
            full_name="四川省遂宁市原安居区",
        )
        current = area(source_row=3, status="active", start_year=2003)
        self.assertEqual(
            choose_administrative_area([current, historical], birth_year=1999),
            historical,
        )
        self.assertEqual(
            choose_administrative_area([historical, current], birth_year=2010),
            current,
        )

    def test_loader_accepts_utf8_bom_and_rejects_changed_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            valid_path = Path(directory) / "valid.csv"
            with valid_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow((
                    "code", "name", "level", "province", "city", "parent_code",
                    "path", "status", "start_year", "end_year", "new_code", "source",
                ))
                writer.writerow((
                    "510904", "安居区", "county", "四川省", "遂宁市", "510900",
                    "四川省/遂宁市/安居区", "active", "2003", "", "", "fixture",
                ))
            _digest, rows = load_administrative_areas(valid_path)
            self.assertEqual(rows[0].full_name, "四川省遂宁市安居区")

            invalid_path = Path(directory) / "invalid.csv"
            invalid_path.write_text("code,name\n510904,安居区\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_administrative_areas(invalid_path)


if __name__ == "__main__":
    unittest.main()
