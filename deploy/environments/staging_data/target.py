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
        domain, table = logical_table.split('.')
        database = candidate[domain]
    except (KeyError, ValueError):
        raise SnapshotError('invalid_target_table') from None
    if not re.fullmatch(r'Staging_s[a-f0-9]{16}_[A-Za-z_]+', database):
        raise SnapshotError('invalid_candidate_database')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', table):
        raise SnapshotError('invalid_target_table')
    return '`' + database + '`.`' + table + '`'
