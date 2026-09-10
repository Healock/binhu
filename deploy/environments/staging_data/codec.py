"""Snapshot-scoped pseudonyms. Raw lookup maps never belong in exported files."""
from __future__ import annotations
import hashlib
import hmac
import json
import re
import unicodedata
from datetime import date, datetime


class SnapshotError(ValueError):
    """Only fixed, non-sensitive reason codes may cross the tool boundary."""


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
        from services.registry_import import NORMAL_HOUSING_TYPES
        matches = 0
        for table, rows in tables.items():
            for row in rows:
                for column, value in row.items():
                    if table == 'RegistryData.registry_properties' and column == 'housing_type':
                        enum(value, NORMAL_HOUSING_TYPES)
                    else:
                        matches += self.scan(value)
        return matches


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
    try:
        datetime.fromisoformat(text)
    except ValueError:
        raise SnapshotError("unrecognized_date") from None
    return text
