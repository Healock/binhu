"""Candidate Staging database identities; production names are never writable."""
import re
from .codec import SnapshotError

DOMAINS = ('OnlineData', 'OnlineDataArchive', 'daily_report', 'PlatformData',
           'VisitData', 'DispatchData', 'RegistryData', 'WorkflowData')
KEYS = ('ONLINE_DATA', 'ARCHIVE', 'DAILY_REPORT', 'PLATFORM', 'VISIT',
        'DISPATCH', 'REGISTRY', 'WORKFLOW')


def database_names(snapshot_id):
    if not re.fullmatch(r'staging-[a-f0-9]{16}', snapshot_id):
        raise SnapshotError('invalid_snapshot_id')
    return {domain: 'Staging_s' + snapshot_id[8:] + '_' + domain for domain in DOMAINS}


def target_settings(settings, snapshot_id):
    if (settings.APP_ENVIRONMENT != 'staging'
            or settings.SESSION_COOKIE_NAME != 'binhu_staging_session'
            or settings.MYSQL_HOST != 'environment-mysql'
            or settings.MYSQL_PORT != 3306
            or settings.MYSQL_USER != 'environment_app'
            or not settings.MYSQL_DOMAIN_DATABASES_ENABLED
            or not settings.PLATFORM_DOMAIN_ACTIVE
            or not settings.REGISTRY_ADDRESS_DOMAIN_ACTIVE
            or settings.TXDOCS_ENABLED):
        raise SnapshotError('target_environment_mismatch')
    current = {domain: getattr(settings, 'MYSQL_' + key + '_DB') for key, domain in zip(KEYS, DOMAINS)}
    if (len(set(current.values())) != 8
            or any(not re.fullmatch(r'Staging_[A-Za-z0-9_]{1,55}', name) for name in current.values())):
        raise SnapshotError('target_database_identity_mismatch')
    candidate = database_names(snapshot_id)
    if set(candidate.values()) & set(current.values()):
        raise SnapshotError('candidate_is_live_database')
    return current, candidate


def qualified(candidate, logical_table):
    # Logical names are labels from the sanitized artifact, never SQL input.
    try:
        domain, separator, table = logical_table.partition('.')
        if not separator:
            raise ValueError
        database = candidate[domain]
    except (KeyError, ValueError):
        raise SnapshotError('invalid_target_table') from None
    if not re.fullmatch(r'Staging_s[a-f0-9]{16}_[A-Za-z_]+', database):
        raise SnapshotError('invalid_candidate_database')
    # Existing MySQL schemas may contain quoted identifiers outside the usual
    # application naming convention (for example a hyphen or a non-ASCII
    # character).  The value is metadata read from the isolated Staging
    # schema, and is quoted below.  Reject the characters that could escape
    # the quoted identifier or alter the logical domain/table split; allow the
    # remaining non-control characters so legitimate legacy tables can be
    # copied without weakening the database/environment boundary.
    if not table or any(ord(char) < 0x20 or char in '`\\;|&<>' for char in table):
        raise SnapshotError('invalid_target_table', diagnostics={'logical_table': logical_table[:128], 'table_name': table[:128]})
    return '`' + database + '`.`' + table + '`'
