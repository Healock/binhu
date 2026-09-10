"""Snapshot-scoped pseudonyms. Raw lookup maps never belong in exported files."""
from __future__ import annotations
import hashlib
import hmac
import json
import re
import unicodedata
from datetime import date, datetime


class SnapshotError(ValueError):
    """Fixed reason code plus safe, aggregate diagnostic metadata."""

    def __init__(self, reason, *, diagnostics=None):
        super().__init__(reason)
        self.reason = reason
        self.diagnostics = diagnostics or {}


def normalized(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or ""))).casefold()


class Codec:
    def __init__(self, salt: bytes):
        if len(salt) != 32:
            raise SnapshotError("snapshot_salt_invalid")
        self._salt = salt
        self._ids = {}
        self._sensitive = set()
        self._numbers = {}
        self._number_owners = {}
        self._number_counts = {}
        self.categorical_overlaps = {}

    def digest(self, kind, value):
        payload = (kind + "\0" + str(value)).encode("utf-8")
        return hmac.new(self._salt, payload, hashlib.sha256).hexdigest()

    def allocate(self, kind, identifiers):
        if kind in self._ids:
            raise SnapshotError("identity_domain_already_allocated")
        source = {int(value) for value in identifiers}
        if any(value < 1 for value in source):
            raise SnapshotError("invalid_source_identity")
        result, used = {}, set()
        for value in sorted(source, key=lambda x: self.digest(kind, x)):
            attempt = 0
            while True:
                candidate = 1_000_000_000 + int(self.digest(kind, f"{value}:{attempt}")[:12], 16) % 1_000_000_000
                if candidate not in used and candidate not in source:
                    break
                attempt += 1
            result[value] = candidate
            used.add(candidate)
        self._ids[kind] = result

    def reference(self, kind, value, *, nullable=True):
        if value is None and nullable:
            return None
        try:
            return self._ids[kind][int(value)]
        except (KeyError, ValueError, TypeError):
            raise SnapshotError("unresolved_reference") from None

    def remember(self, value):
        text = str(value or "").strip()
        if text:
            self._sensitive.add(text)
        return text

    def text(self, kind, value, label):
        text = self.remember(value)
        return label + self.digest(kind, normalized(text))[:16] if text else ""

    def address(self, community_key, value):
        text = self.remember(value)
        if not text:
            return ""
        # Community is part of the key: identical text across communities does
        # not establish address identity.
        return "验证路" + self.digest("address", str(community_key) + ":" + normalized(text))[:16] + "号"

    def _number(self, kind, value, digits):
        text = self.remember(value)
        if not text:
            return ""
        key = (kind, normalized(text))
        if key in self._numbers:
            return self._numbers[key]
        if self._number_counts.get(kind, 0) >= 10**digits:
            raise SnapshotError("synthetic_number_space_exhausted")
        attempt = 0
        while True:
            candidate = str(int(self.digest(kind, f"{key[1]}:{attempt}"), 16) % (10**digits)).zfill(digits)
            owner = (kind, candidate)
            if owner not in self._number_owners:
                self._number_owners[owner] = key
                self._number_counts[kind] = self._number_counts.get(kind, 0) + 1
                self._numbers[key] = candidate
                return candidate
            attempt += 1

    def phone(self, value):
        # 199 is syntactically mobile-like. It is never sent to an external
        # platform; collision with any exported source value blocks release.
        number = self._number("phone", value, 8)
        return "199" + number if number else ""

    def identity(self, value):
        number = self._number("identity", value, 5)
        if not number:
            return ""
        # A fictional administrative code avoids manufacturing a usable real
        # identity while preserving length, date and checksum validation.
        body = "99000019" + number[:2] + "0101" + number[2:]
        weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
        return body + "10X98765432"[sum(int(c)*w for c,w in zip(body, weights)) % 11]

    def scan(self, value):
        """Compare every exported string leaf with all selected sensitive values.

        The exporter additionally uses closed output formats; unknown free text
        never crosses the boundary. This scan is an independent exact-value gate,
        not a claim to discover arbitrary PII from an unrestricted database dump.
        """
        if isinstance(value, dict):
            return sum(self.scan(v) for v in value.values())
        if isinstance(value, (list, tuple)):
            return sum(self.scan(v) for v in value)
        if not isinstance(value, str) or not value:
            return 0
        if value in self._sensitive:
            return 1
        # Local source values are JSON encoded inside the envelope. Inspect
        # their leaves as well, including escaped Unicode and nested objects.
        if value.lstrip().startswith(("{", "[")):
            try:
                decoded = json.loads(value)
            except (ValueError, RecursionError):
                raise SnapshotError("invalid_embedded_json") from None
            return self.scan(decoded)
        return 0

    def scan_tables(self, tables):
        """Scan a table envelope with one explicit categorical field contract.

        A public housing category can also occur in source free text. Only the
        direct registry housing_type column is categorical; the same value in
        any other column, nested JSON or generic scan remains sensitive.
        Revalidate here so the final scan works on serialized data too.
        """
        return sum(item['count'] for item in self.scan_table_summary(tables))

    def assert_tables_safe(self, tables):
        fields = self.scan_table_summary(tables)
        if fields:
            raise SnapshotError('source_sensitive_value_detected', diagnostics={
                'match_count': sum(item['count'] for item in fields), 'fields': fields,
            })

    def scan_table_summary(self, tables):
        """Return table/field/count metadata, never a matched source value."""
        from services.registry_import import NORMAL_HOUSING_TYPES
        from services.parsers import get_parser
        from services.task_workflow import TASK_WORKFLOWS
        from .tasks import TASK_TYPES, result_categories
        contracts = {kind: (get_parser(kind), TASK_WORKFLOWS[kind]) for kind in TASK_TYPES}
        by_table = {'OnlineData.' + parser.table_name: (parser, workflow)
                    for parser, workflow in contracts.values()}
        summary = []
        for table, rows in tables.items():
            for column in sorted({key for row in rows for key in row}):
                count = 0
                overlaps = 0
                for row in rows:
                    value = row.get(column)
                    if table == 'RegistryData.registry_properties' and column == 'housing_type':
                        enum(value, NORMAL_HOUSING_TYPES)
                        overlaps += self.scan(value)
                    elif table in by_table and column == by_table[table][1].result_field:
                        # A result is a closed business category, not source prose.
                        # Revalidate even after JSON serialization, retaining the
                        # exact scan for the same bytes in every other field.
                        enum(value, result_categories(by_table[table][1]))
                        overlaps += self.scan(value)
                    elif (table in {'OnlineData._online_source_rows', 'OnlineData._local_source_records'}
                          and column == 'values_json' and row.get('parser_type') in contracts):
                        parser, workflow = contracts[row['parser_type']]
                        try:
                            decoded = json.loads(value) if isinstance(value, str) else value
                        except (ValueError, RecursionError):
                            raise SnapshotError('invalid_embedded_json') from None
                        if (not isinstance(decoded, dict) or set(decoded) != set(parser.COLUMNS)
                                or any(not isinstance(v, (str, type(None))) for v in decoded.values())):
                            raise SnapshotError('task_value_contract_mismatch')
                        result = decoded[workflow.result_field]
                        enum(result, result_categories(workflow))
                        overlaps += self.scan(result)
                        count += self.scan({k: v for k, v in decoded.items() if k != workflow.result_field})
                    else:
                        count += self.scan(value)
                if overlaps:
                    self.categorical_overlaps[(table, column)] = overlaps
                if count:
                    summary.append({'table': table, 'field': column, 'count': count})
        return summary


def enum(value, allowed, *, empty=True):
    if value is None or value == "":
        if empty:
            return ""
        raise SnapshotError("missing_enum")
    if value not in allowed:
        raise SnapshotError("unknown_enum")
    return value


def integer(value, *, minimum=0, maximum=2**63-1):
    if isinstance(value, bool):
        value = int(value)
    if not isinstance(value, int) or not minimum <= value <= maximum:
        raise SnapshotError("integer_out_of_bounds")
    return value


def date_value(value):
    if value is None or value == "":
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            raise SnapshotError("unrecognized_date") from None
    # Keep Python's strict ISO parser for values already emitted by MySQL,
    # including timezone offsets and ``Z``. Then accept the explicit local
    # import formats below; arbitrary prose is still rejected.
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.isoformat(sep=" ")
    except ValueError:
        pass
    # Accept the formats used by current local imports while rejecting free
    # text and impossible calendar dates. Normalized output stays stable.
    formats = (
        "%Y-%m-%d", "%Y/%m/%d", "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S.%f",
    )
    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.isoformat(sep=" ") if "H" in fmt else parsed.date().isoformat()
        except ValueError:
            continue
    raise SnapshotError("unrecognized_date") from None
