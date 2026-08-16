from __future__ import annotations

import warnings

from database import suppress_expected_bootstrap_warnings


def test_bootstrap_warning_filter_only_hides_known_idempotent_messages():
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        with suppress_expected_bootstrap_warnings():
            warnings.warn("Table 'PlatformData._users' already exists", Warning)
            warnings.warn(
                "Duplicate entry 'synthetic' for key '_system_config.PRIMARY'",
                Warning,
            )
            warnings.warn("Unknown table 'daily_report.synthetic_snapshot'", Warning)

    assert [str(item.message) for item in captured] == [
        "Unknown table 'daily_report.synthetic_snapshot'"
    ]
