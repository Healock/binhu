"""Redis task read-model key and atomic update contract.

Business snapshots are stored only as authenticated ciphertext. Search and
scope indexes contain environment-scoped HMAC tokens, never source values.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from typing import Any

from services.task_commands import (
    TaskCommandContractError, decrypt_task_snapshot, task_row_group_token,
)


SEARCH_FIELDS = frozenset({
    "name", "address", "phone", "identity", "community", "inspector",
    "task_type", "task_state",
})
_RUN_PATTERNS = {
    "development": re.compile(r"dev-[A-Za-z0-9][A-Za-z0-9_-]{0,63}"),
    "staging": re.compile(r"STG-[0-9]{8}-[0-9]{2}"),
    "production": re.compile(r"PRD-[A-Za-z0-9_-]{1,48}"),
    "shadow": re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?"),
}
_TASK_ID_PATTERN = re.compile(r"(t_[a-z0-9_]{1,79}):([1-9][0-9]{0,18})")
_MAX_QUERY_FIELDS = 32
_MAX_VALUES_PER_FIELD = 50
_MAX_PAGE_SIZE = 100_000
_MAX_OFFSET = 1_000_000
TASK_READ_MODEL_SCAN_BATCH_MAX = 500
_SCAN_TTL_SECONDS = 300
_STATE_INDEX_FIELDS = {
    "address_match.status", "address_match.small_community_name",
    "watch.category_id", "qmf.state", "residence.state",
    "registration.status", "review.state", "task_graph.state",
    "task_graph.blocked",
}

_APPLY_EVENT = r"""
local old_revision = tonumber(redis.call('HGET', KEYS[1], 'revision') or '-1')
local revision = tonumber(ARGV[1])
local event_id = ARGV[2]
local command_id = ARGV[3]
local tombstone = ARGV[4]
local previous_event = redis.call('HGET', KEYS[1], 'event_id')
if previous_event == event_id then return 'duplicate' end
if revision < old_revision then return 'stale' end
if revision == old_revision and old_revision >= 0 then
  if previous_event == event_id then return 'duplicate' end
  return 'revision_conflict'
end
local prefix = ARGV[16]
local old_group = redis.call('HGET', KEYS[1], 'row_group_token') or ''
local old_source_id = redis.call('HGET', KEYS[1], 'source_id') or ''
local old_community = redis.call('HGET', KEYS[1], 'community_token') or ''
local new_group = ARGV[17]
local new_community = ARGV[18]
local function group_keys(token)
  return prefix .. ':group:' .. token .. ':members', prefix .. ':group:' .. token .. ':indexes'
end
local function remove_group_community(token, community, task_id)
  if token == '' or community == '' then return end
  local relation = prefix .. ':group:' .. token .. ':community:' .. community
  redis.call('SREM', relation, task_id)
  if redis.call('SCARD', relation) == 0 then
    redis.call('DEL', relation)
    local community_index = prefix .. ':gidx-community:' .. community
    redis.call('ZREM', community_index, token)
    if redis.call('ZCARD', community_index) == 0 then
      redis.call('SREM', prefix .. ':community-tokens', community)
    end
  end
end
local function add_group_community(token, community, task_id)
  if token == '' or community == '' then return end
  local relation = prefix .. ':group:' .. token .. ':community:' .. community
  local was_empty = redis.call('SCARD', relation) == 0
  redis.call('SADD', relation, task_id)
  if was_empty then redis.call('ZADD', prefix .. ':gidx-community:' .. community, 1, token) end
  redis.call('SADD', prefix .. ':community-tokens', community)
end
local function rebuild_group(token)
  if token == '' then return end
  local members_key, group_indexes_key = group_keys(token)
  local prior_group_indexes = redis.call('SMEMBERS', group_indexes_key)
  for _, key in ipairs(prior_group_indexes) do redis.call('ZREM', key, token) end
  redis.call('DEL', group_indexes_key)
  local canonical = redis.call('ZRANGE', members_key, 0, 0)[1]
  if not canonical then return end
  local canonical_indexes = prefix .. ':indexes:' .. canonical
  local task_indexes = redis.call('SMEMBERS', canonical_indexes)
  for _, key in ipairs(task_indexes) do
    local group_index = string.gsub(key, ':idx:', ':gidx:')
    redis.call('ZADD', group_index, 1, token)
    redis.call('SADD', group_indexes_key, group_index)
  end
end
local prior_indexes = redis.call('SMEMBERS', KEYS[2])
for _, key in ipairs(prior_indexes) do redis.call('ZREM', key, ARGV[5]) end
redis.call('DEL', KEYS[2])
if old_group ~= '' then
  local old_members_key, _ = group_keys(old_group)
  redis.call('ZREM', old_members_key, string.rep('0', 19 - string.len(old_source_id)) .. old_source_id .. '|' .. ARGV[5])
  remove_group_community(old_group, old_community, ARGV[5])
  redis.call('ZREM', prefix .. ':gidx:all', old_group)
  if redis.call('ZCARD', old_members_key) > 0 then redis.call('ZADD', prefix .. ':gidx:all', 1, old_group) end
  rebuild_group(old_group)
end
if tombstone == '1' then
  redis.call('HSET', KEYS[1], 'environment', ARGV[12], 'task_id', ARGV[13], 'source_id', ARGV[14], 'run_id', ARGV[15],
    'revision', ARGV[1], 'event_id', event_id,
    'command_id', command_id, 'tombstone', '1', 'ciphertext', '', 'aad', '', 'nonce', '',
    'ciphertext_sha256', '', 'key_version', '', 'row_group_token', '', 'community_token', '', 'updated_at', ARGV[6])
else
  redis.call('HSET', KEYS[1], 'environment', ARGV[12], 'task_id', ARGV[13], 'source_id', ARGV[14], 'run_id', ARGV[15],
    'revision', ARGV[1], 'event_id', event_id,
    'command_id', command_id, 'tombstone', '0', 'ciphertext', ARGV[7], 'aad', ARGV[8], 'nonce', ARGV[9],
    'ciphertext_sha256', ARGV[10], 'key_version', ARGV[11], 'row_group_token', new_group,
    'community_token', new_community, 'updated_at', ARGV[6])
  local members_key, _ = group_keys(new_group)
  local source_member = string.rep('0', 19 - string.len(ARGV[14])) .. ARGV[14] .. '|' .. ARGV[5]
  redis.call('ZADD', members_key, 0, source_member)
  redis.call('ZADD', prefix .. ':gidx:all', 1, new_group)
  add_group_community(new_group, new_community, ARGV[5])
  for index = 19, #ARGV do
    local separator = string.find(ARGV[index], '|', 1, true)
    local index_key = string.sub(ARGV[index], 1, separator - 1)
    redis.call('ZADD', index_key, revision, ARGV[5])
    redis.call('SADD', KEYS[2], index_key)
  end
  rebuild_group(new_group)
end
redis.call('INCR', KEYS[3])
return 'applied'
"""


def _key(secret: str, environment: str) -> bytes:
    if len(secret.encode("utf-8")) < 32:
        raise TaskCommandContractError("dedicated task event key is required")
    if environment not in {"development", "staging", "production", "shadow"}:
        raise TaskCommandContractError("invalid task read-model environment")
    base = hashlib.sha256(b"binhu/task-event/aes-gcm/v1\0" + secret.encode()).digest()
    return hmac.new(base, b"binhu/task-read-model-index/v1\0" + environment.encode(), hashlib.sha256).digest()


def _configured_task_event_secret() -> str:
    from config import settings

    return str(settings.TASK_EVENT_ENCRYPTION_KEY or "")


def search_token(value: Any, *, field: str, environment: str, secret: str) -> str:
    if (not isinstance(field, str) or not field or len(field) > 120
            or any(ord(char) < 32 for char in field)):
        raise TaskCommandContractError("unsupported task search field")
    normalized = re.sub(r"[ \t\n\v\f\r]+", " ", str(value or "").strip(" \t\n\v\f\r")).lower()
    if not normalized or len(normalized) > 512:
        raise TaskCommandContractError("invalid task search value")
    return hmac.new(_key(secret, environment), field.encode() + b"\0" + normalized.encode(), hashlib.sha256).hexdigest()


def task_read_model_keys(environment: str, run_id: str, task_id: str) -> tuple[str, str, str]:
    run_pattern = _RUN_PATTERNS.get(environment) if isinstance(environment, str) else None
    task_match = _TASK_ID_PATTERN.fullmatch(task_id) if isinstance(task_id, str) else None
    if (run_pattern is None or not isinstance(run_id, str)
            or not run_pattern.fullmatch(run_id) or task_match is None):
        raise TaskCommandContractError("invalid task read-model identity")
    if not _registered_task_tables() or task_match.group(1) not in _registered_task_tables():
        raise TaskCommandContractError("invalid task read-model identity")
    prefix = f"binhu:task-rm:v4:{environment}:{run_id}"
    return f"{prefix}:task:{task_id}", f"{prefix}:indexes:{task_id}", prefix


def _registered_task_tables() -> frozenset[str]:
    from services.parsers import PARSER_REGISTRY

    return frozenset(
        str(parser.table_name)
        for parser in PARSER_REGISTRY.values()
        if isinstance(getattr(parser, "table_name", None), str)
    )


def _registered_filter_fields() -> frozenset[str]:
    from services.parsers import PARSER_REGISTRY

    fields = set(SEARCH_FIELDS)
    for parser in PARSER_REGISTRY.values():
        fields.update(
            field for field in getattr(parser, "COLUMNS", ())
            if isinstance(field, str) and field and len(field) <= 120
        )
    return frozenset(fields)


def _validate_parser_type(parser_type: str) -> None:
    if not isinstance(parser_type, str) or parser_type not in _registered_task_tables():
        raise TaskCommandContractError("invalid task read-model parser")


def _parser_key_for_table(table_name: str) -> str:
    from services.parsers import TABLE_NAMES

    for parser_key, registered_table in TABLE_NAMES.items():
        if registered_table == table_name:
            return parser_key
    raise TaskCommandContractError("invalid task read-model parser")


def _validate_filters(filters: dict[str, Any]) -> dict[str, list[str]]:
    if not isinstance(filters, dict) or len(filters) > _MAX_QUERY_FIELDS:
        raise TaskCommandContractError("invalid task read-model filters")
    allowed_fields = _registered_filter_fields()
    normalized: dict[str, list[str]] = {}
    for field, raw_values in filters.items():
        if not isinstance(field, str) or field not in allowed_fields or isinstance(raw_values, bytes):
            raise TaskCommandContractError("invalid task read-model filters")
        values = raw_values if isinstance(raw_values, list) else [raw_values]
        if len(values) > _MAX_VALUES_PER_FIELD:
            raise TaskCommandContractError("invalid task read-model filters")
        if any(not isinstance(value, str) or len(value) > 512 for value in values):
            raise TaskCommandContractError("invalid task read-model filters")
        if field == "task_type" and any(value not in _registered_task_tables() for value in values):
            raise TaskCommandContractError("invalid task read-model filters")
        normalized[field] = [value for value in values if value.strip()]
    return normalized


def _validate_query_page(offset: int, limit: int) -> None:
    if (type(offset) is not int or type(limit) is not int
            or offset < 0 or offset > _MAX_OFFSET
            or limit < 1 or limit > _MAX_PAGE_SIZE):
        raise TaskCommandContractError("invalid task read-model page")


def _validate_sort(sort_by: str | None, sort_order: str) -> None:
    if sort_by is not None and (
        not isinstance(sort_by, str) or sort_by not in _registered_filter_fields()
    ):
        raise TaskCommandContractError("invalid task read-model sort")
    if not isinstance(sort_order, str) or sort_order not in {"asc", "desc"}:
        raise TaskCommandContractError("invalid task read-model sort")


def _validate_search(search: str | None) -> str | None:
    if search is None or search == "":
        return None
    if not isinstance(search, str):
        raise TaskCommandContractError("invalid task read-model search")
    normalized = re.sub(r"[ \t\n\v\f\r]+", " ", search.strip(" \t\n\v\f\r")).lower()
    if len(normalized) < 2 or len(normalized) > 120:
        raise TaskCommandContractError("invalid task read-model search")
    return normalized


def _validate_redis_index_key(key: str, prefix: str) -> bool:
    return isinstance(key, str) and bool(
        re.fullmatch(re.escape(prefix) + r":idx:[0-9a-f]{64}", key)
    )


def build_read_model_update(
    event: dict[str, Any], *, secret: str,
) -> dict[str, Any]:
    """Validate and reduce one v4 task event into an atomic Redis update."""
    schema_version = event.get("schema_version")
    if schema_version not in {4, 5, 6}:
        raise TaskCommandContractError("unsupported task read-model event version")
    environment = event.get("environment")
    task_id = event.get("task_id")
    revision = event.get("revision")
    event_id = event.get("event_id")
    command_id = event.get("command_id")
    source_id = event.get("source_id")
    run_id = event.get("run_id")
    try:
        task_read_model_keys(environment, run_id, task_id)
    except TaskCommandContractError:
        raise TaskCommandContractError("invalid task read-model event") from None
    if (not isinstance(task_id, str) or not task_id
            or type(revision) is not int or revision < 1
            or type(source_id) is not int or source_id < 1
            or not isinstance(event_id, str) or not event_id):
        raise TaskCommandContractError("invalid task read-model event")
    try:
        event_id = str(uuid.UUID(event_id))
        command_id = str(uuid.UUID(command_id))
    except (ValueError, TypeError, AttributeError):
        raise TaskCommandContractError("invalid task read-model event") from None
    task_key, indexes_key, prefix = task_read_model_keys(environment, run_id, task_id)
    deleted = event.get("event_type") in {"task.deleted", "task.archived"}
    if deleted:
        envelope = {}
        index_keys: list[str] = []
        row_group_token = None
        community_token = None
    else:
        row_group_token = event.get("row_group_token")
        if schema_version == 6 and (
            not isinstance(row_group_token, str)
            or re.fullmatch(r"[0-9a-f]{64}", row_group_token) is None
        ):
            raise TaskCommandContractError("invalid task row group token")
        if schema_version in {4, 5} and row_group_token is not None:
            raise TaskCommandContractError("invalid legacy task row group token")
        envelope = event.get("encrypted_payload")
        if not isinstance(envelope, dict):
            raise TaskCommandContractError("encrypted task snapshot is required")
        snapshot = decrypt_task_snapshot(
            envelope, environment=environment, task_id=task_id,
            revision=revision, event_id=event_id, command_id=command_id,
            row_group_token=row_group_token if schema_version == 6 else None,
        )
        if schema_version in {4, 5}:
            parser_key = _parser_key_for_table(task_id.partition(":")[0])
            row_group_token = task_row_group_token(
                snapshot, parser_type=parser_key, environment=environment,
            )
        from services.parsers import get_parser

        parser = get_parser(_parser_key_for_table(task_id.partition(":")[0]))
        community_value = str(parser.community_value(snapshot) or "").strip()
        community_token = search_token(
            community_value if community_value else "__missing_community__",
            field="group-community", environment=environment, secret=secret,
        )
        values: dict[str, list[str]] = {
            field: [value]
            for field, value in snapshot.items()
            if isinstance(field, str) and isinstance(value, str) and value.strip()
        }
        parser_type = task_id.partition(":")[0]
        values["task_type"] = [parser_type]
        index_keys = []
        search_values: set[str] = set()
        for field, items in values.items():
            if not isinstance(items, list):
                raise TaskCommandContractError("invalid task read-model index values")
            for value in items:
                token = search_token(value, field=field, environment=environment, secret=secret)
                index_keys.append(f"{prefix}:idx:{token}")
                search_values.add(value)
                if "社区" in field:
                    token = search_token(value, field="community", environment=environment, secret=secret)
                    index_keys.append(f"{prefix}:idx:{token}")
                if "核查人" in field or "责任人" in field:
                    token = search_token(value, field="inspector", environment=environment, secret=secret)
                    index_keys.append(f"{prefix}:idx:{token}")
        for value in search_values:
            normalized = re.sub(r"[ \t\n\v\f\r]+", " ", value.strip(" \t\n\v\f\r")).lower()
            if len(normalized) > 512:
                continue
            grams = {normalized[index:index + 2] for index in range(max(0, len(normalized) - 1))}
            for gram in grams:
                token = search_token(gram, field="search", environment=environment, secret=secret)
                index_keys.append(f"{prefix}:idx:{token}")
    return {
        "keys": (task_key, indexes_key, f"{prefix}:watermark"),
        "revision": revision,
        "event_id": event_id,
        "command_id": command_id,
        "tombstone": deleted,
        "environment": environment,
        "run_id": run_id,
        "task_id": task_id,
        "source_id": source_id,
        "member": task_id,
        "row_group_token": None if deleted else row_group_token,
        "community_token": None if deleted else community_token,
        "envelope": envelope,
        "index_keys": sorted(set(index_keys)),
        "updated_at": str(event.get("timestamp") or ""),
    }


async def apply_read_model_update(redis_client, update: dict[str, Any]) -> str:
    try:
        expected_keys = task_read_model_keys(
            update["environment"], update["run_id"], update["task_id"],
        )
        prefix = expected_keys[2]
        index_keys = update["index_keys"]
        if (tuple(update["keys"]) != (expected_keys[0], expected_keys[1], f"{prefix}:watermark")
                or not isinstance(index_keys, list)
                or any(not _validate_redis_index_key(key, prefix) for key in index_keys)
                or update["member"] != update["task_id"]
                or type(update["source_id"]) is not int or update["source_id"] < 1
                or type(update["revision"]) is not int or update["revision"] < 1
                or (not update["tombstone"] and (
                    not isinstance(update.get("row_group_token"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", update["row_group_token"]) is None
                ))
                or (not update["tombstone"] and (
                    not isinstance(update.get("community_token"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", update["community_token"]) is None
                ))
                or type(update["tombstone"]) is not bool):
            raise TaskCommandContractError("invalid task read-model update")
    except (KeyError, TypeError, ValueError):
        raise TaskCommandContractError("invalid task read-model update") from None
    envelope = update["envelope"]
    args = [
        update["revision"], update["event_id"], update["command_id"],
        "1" if update["tombstone"] else "0", update["member"], update["updated_at"],
        envelope.get("ciphertext", ""), envelope.get("aad", ""), envelope.get("nonce", ""),
        envelope.get("ciphertext_sha256", ""), envelope.get("key_version", ""),
        update["environment"], update["task_id"], update["source_id"],
    ]
    args.append(update["run_id"])
    args.append(prefix)
    args.append(update.get("row_group_token") or "")
    args.append(update.get("community_token") or "")
    args.extend(key + "|" for key in update["index_keys"])
    return await redis_client.eval(_APPLY_EVENT, 3, *update["keys"], *args)


def index_key(environment: str, run_id: str, field: str, value: Any, *, secret: str) -> str:
    _task_key, _indexes_key, prefix = task_read_model_keys(environment, run_id, "t_fullchain:1")
    if field not in _registered_filter_fields() | {"search"}:
        raise TaskCommandContractError("unsupported task read-model field")
    token = search_token(value, field=field, environment=environment, secret=secret)
    return f"{prefix}:idx:{token}"


async def query_task_ids(redis_client, *, environment: str, run_id: str,
                         filters: dict[str, Any], secret: str,
                         search: str | None = None,
                         offset: int = 0, limit: int = 100,
                         descending: bool = True) -> list[str]:
    _validate_query_page(offset, limit)
    if type(descending) is not bool:
        raise TaskCommandContractError("invalid task read-model sort")
    _task_key, _indexes_key, prefix = task_read_model_keys(
        environment, run_id, "t_fullchain:1",
    )
    filters = _validate_filters(filters)
    normalized_search = _validate_search(search)
    keys: list[str] = []
    temporary_keys: list[str] = []
    try:
        for field, values in filters.items():
            values = [value for value in values if value.strip()]
            if not values:
                return []
            field_keys = [index_key(environment, run_id, field, value, secret=secret) for value in values]
            if len(field_keys) == 1:
                keys.append(field_keys[0])
            else:
                temporary = f"{prefix}:query:{uuid.uuid4()}"
                temporary_keys.append(temporary)
                await redis_client.execute_command(
                    "ZUNIONSTORE", temporary, len(field_keys), *field_keys,
                    "AGGREGATE", "MIN",
                )
                await redis_client.expire(temporary, 30)
                keys.append(temporary)
        if normalized_search:
            for gram in sorted({normalized_search[index:index + 2]
                                for index in range(len(normalized_search) - 1)}):
                keys.append(index_key(environment, run_id, "search", gram, secret=secret))
        if not keys:
            raise TaskCommandContractError("task read-model query requires a bounded index")
        result_key = f"{prefix}:query:{uuid.uuid4()}"
        temporary_keys.append(result_key)
        await redis_client.execute_command(
            "ZINTERSTORE", result_key, len(keys), *keys, "AGGREGATE", "MIN",
        )
        await redis_client.expire(result_key, 30)
        total = await redis_client.zcard(result_key)
        if type(total) is not int or total < 0:
            raise TaskCommandContractError("task read model query failed")
        if total == 0 or offset >= total:
            return []
        page_end = offset + limit - 1
        if descending:
            page = await redis_client.zrevrange(result_key, offset, page_end, withscores=True)
        else:
            page = await redis_client.zrange(result_key, offset, page_end, withscores=True)
        if not isinstance(page, (list, tuple)) or len(page) > limit:
            raise TaskCommandContractError("task read model query failed")
        task_ids = []
        for entry in page:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                raise TaskCommandContractError("task read model query failed")
            member, score = entry
            if not isinstance(member, (str, bytes)):
                raise TaskCommandContractError("task read model query failed")
            try:
                float(score)
            except (TypeError, ValueError):
                raise TaskCommandContractError("task read model query failed") from None
            task_ids.append(member.decode() if isinstance(member, bytes) else member)
        if any(_TASK_ID_PATTERN.fullmatch(task_id) is None for task_id in task_ids):
            raise TaskCommandContractError("task read model query failed")
        return task_ids
    except TaskCommandContractError:
        raise
    except Exception:
        raise TaskCommandContractError("task read model query failed") from None
    finally:
        if temporary_keys:
            try:
                await redis_client.delete(*temporary_keys)
            except Exception:
                raise TaskCommandContractError("task read model query failed") from None


def _task_read_model_scan_keys(environment: str, run_id: str, parser_type: str,
                               scan_id: str) -> tuple[str, str, str]:
    _validate_parser_type(parser_type)
    _task_key, _indexes_key, prefix = task_read_model_keys(
        environment, run_id, f"{parser_type}:1",
    )
    if not isinstance(scan_id, str) or re.fullmatch(r"[0-9a-f]{32}", scan_id) is None:
        raise TaskCommandContractError("invalid task read-model scan")
    identity = f"{parser_type}:{scan_id}"
    return (
        f"{prefix}:scan:{identity}",
        f"{prefix}:scan-meta:{identity}",
        prefix,
    )


async def create_task_read_model_scan(
    redis_client, *, environment: str, run_id: str, parser_type: str,
    communities: list[str] | None, filters: dict[str, Any],
    search: str | None, secret: str, descending: bool = True,
) -> dict[str, Any]:
    """Create a short-lived immutable candidate set for bounded API scans.

    The returned cursor is an offset into a server-created ZSET ordered by
    (revision, task_id). Callers can stream all candidates and apply any
    plaintext-only filters or row-key grouping without exposing Redis keys.
    """
    _validate_parser_type(parser_type)
    if type(descending) is not bool:
        raise TaskCommandContractError("invalid task read-model sort")
    normalized_filters = _validate_filters(filters)
    normalized_search = _validate_search(search)
    requested_communities = normalized_filters.pop("community", None)
    if communities is not None:
        if (not isinstance(communities, list) or len(communities) > _MAX_VALUES_PER_FIELD
                or any(not isinstance(value, str) or len(value) > 512 for value in communities)):
            raise TaskCommandContractError("invalid task read-model filters")
        if requested_communities:
            requested_set = set(requested_communities)
            communities = [
                value for value in communities if value in requested_set
            ]
        if not communities:
            return {"scan_id": None, "total": 0, "cursor": 0}
    elif requested_communities:
        normalized_filters["community"] = requested_communities
    requested_task_types = normalized_filters.pop("task_type", None)
    if requested_task_types and parser_type not in requested_task_types:
        return {"scan_id": None, "total": 0, "cursor": 0}

    _task_key, _indexes_key, prefix = task_read_model_keys(
        environment, run_id, f"{parser_type}:1",
    )
    scan_id = uuid.uuid4().hex
    result_key, metadata_key, _ = _task_read_model_scan_keys(
        environment, run_id, parser_type, scan_id,
    )
    temporary_keys: list[str] = []
    index_keys: list[str] = []
    published = False
    index_filters: dict[str, list[str]] = {"task_type": [parser_type]}
    if communities is not None:
        index_filters["community"] = communities
    index_filters.update({field: values for field, values in normalized_filters.items() if values})
    try:
        for field, values in index_filters.items():
            values = [value for value in values if value.strip()]
            if not values:
                return {"scan_id": None, "total": 0, "cursor": 0}
            field_keys = [
                index_key(environment, run_id, field, value, secret=secret)
                for value in values
            ]
            if len(field_keys) == 1:
                index_keys.append(field_keys[0])
                continue
            union_key = f"{prefix}:scan-work:{parser_type}:{uuid.uuid4().hex}"
            temporary_keys.append(union_key)
            await redis_client.execute_command(
                "ZUNIONSTORE", union_key, len(field_keys), *field_keys,
                "AGGREGATE", "MAX",
            )
            index_keys.append(union_key)
        if normalized_search:
            for gram in sorted({
                normalized_search[index:index + 2]
                for index in range(len(normalized_search) - 1)
            }):
                index_keys.append(index_key(
                    environment, run_id, "search", gram, secret=secret,
                ))
        if not index_keys:
            raise TaskCommandContractError("task read-model scan requires a bounded index")
        await redis_client.execute_command(
            "ZINTERSTORE", result_key, len(index_keys), *index_keys,
            "AGGREGATE", "MIN",
        )
        total = await redis_client.zcard(result_key)
        if type(total) is not int or total < 0:
            raise TaskCommandContractError("task read-model scan failed")
        await redis_client.set(
            metadata_key,
            json.dumps({
                "environment": environment,
                "run_id": run_id,
                "parser_type": parser_type,
                "scan_id": scan_id,
                "total": total,
                "descending": descending,
            }, separators=(",", ":")),
            ex=_SCAN_TTL_SECONDS,
        )
        if total:
            await redis_client.expire(result_key, _SCAN_TTL_SECONDS)
        published = True
        return {"scan_id": scan_id, "total": total, "cursor": 0}
    except TaskCommandContractError:
        raise
    except Exception:
        raise TaskCommandContractError("task read-model scan failed") from None
    finally:
        cleanup_keys = list(temporary_keys)
        if not published:
            cleanup_keys.extend((result_key, metadata_key))
        if cleanup_keys:
            try:
                await redis_client.delete(*cleanup_keys)
            except Exception:
                raise TaskCommandContractError("task read-model scan failed") from None


async def read_task_read_model_scan(
    redis_client, *, environment: str, run_id: str, parser_type: str,
    scan_id: str, cursor: int, limit: int,
) -> dict[str, Any]:
    """Read at most 500 task identities from an immutable candidate scan."""
    if (type(cursor) is not int or cursor < 0 or type(limit) is not int
            or limit < 1 or limit > TASK_READ_MODEL_SCAN_BATCH_MAX):
        raise TaskCommandContractError("invalid task read-model scan page")
    result_key, metadata_key, _prefix = _task_read_model_scan_keys(
        environment, run_id, parser_type, scan_id,
    )
    try:
        raw_metadata = await redis_client.get(metadata_key)
        if raw_metadata is None:
            raise TaskCommandContractError("task read-model scan expired")
        if isinstance(raw_metadata, bytes):
            raw_metadata = raw_metadata.decode("utf-8")
        metadata = json.loads(raw_metadata)
        if (not isinstance(metadata, dict)
                or metadata.get("environment") != environment
                or metadata.get("run_id") != run_id
                or metadata.get("parser_type") != parser_type
                or metadata.get("scan_id") != scan_id
                or type(metadata.get("total")) is not int
                or type(metadata.get("descending")) is not bool):
            raise TaskCommandContractError("task read-model scan is inconsistent")
        total = metadata["total"]
        descending = metadata["descending"]
        if total < 0 or cursor > total:
            raise TaskCommandContractError("invalid task read-model scan cursor")
        if total:
            current_total = await redis_client.zcard(result_key)
            if type(current_total) is not int or current_total != total:
                raise TaskCommandContractError("task read-model scan expired")
            end = min(cursor + limit, total) - 1
            if cursor >= total:
                page = []
            elif descending:
                page = await redis_client.zrevrange(
                    result_key, cursor, end, withscores=True,
                )
            else:
                page = await redis_client.zrange(
                    result_key, cursor, end, withscores=True,
                )
        else:
            page = []
        if not isinstance(page, (list, tuple)) or len(page) > limit:
            raise TaskCommandContractError("task read-model scan failed")
        records = []
        for entry in page:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                raise TaskCommandContractError("task read-model scan failed")
            member, score = entry
            task_id = member.decode() if isinstance(member, bytes) else member
            match = _TASK_ID_PATTERN.fullmatch(task_id) if isinstance(task_id, str) else None
            if match is None or match.group(1) != parser_type:
                raise TaskCommandContractError("task read-model scan identity mismatch")
            try:
                numeric_score = float(score)
                indexed_revision = int(numeric_score)
            except (TypeError, ValueError, OverflowError):
                raise TaskCommandContractError("task read-model scan failed") from None
            if indexed_revision < 1 or numeric_score != indexed_revision:
                raise TaskCommandContractError("task read-model scan failed")
            records.append({"task_id": task_id, "indexed_revision": indexed_revision})
        next_cursor = cursor + len(records)
        await redis_client.expire(metadata_key, _SCAN_TTL_SECONDS)
        if total:
            await redis_client.expire(result_key, _SCAN_TTL_SECONDS)
        return {
            "records": records,
            "next_cursor": next_cursor if next_cursor < total else None,
            "total": total,
        }
    except TaskCommandContractError:
        raise
    except Exception:
        raise TaskCommandContractError("task read-model scan failed") from None


async def close_task_read_model_scan(redis_client, *, environment: str, run_id: str,
                                     parser_type: str, scan_id: str) -> None:
    result_key, metadata_key, _prefix = _task_read_model_scan_keys(
        environment, run_id, parser_type, scan_id,
    )
    try:
        await redis_client.delete(result_key, metadata_key)
    except Exception:
        raise TaskCommandContractError("task read-model scan cleanup failed") from None


def _decode_task_row(row: dict[str, Any], *, environment: str, run_id: str,
                     task_id: str, expected_source_id: int | None = None,
                     expected_revision: int | None = None,
                     event_id: str | None = None,
                     command_id: str | None = None) -> dict[str, Any] | None:
    task_read_model_keys(environment, run_id, task_id)
    if not isinstance(row, dict) or not row:
        raise TaskCommandContractError("task read model is inconsistent")
    try:
        source_id = int(row.get("source_id", 0))
        revision = int(row.get("revision", -1))
    except (TypeError, ValueError):
        raise TaskCommandContractError("task read model is inconsistent") from None
    if (row.get("environment") != environment or row.get("run_id") != run_id
            or row.get("task_id") != task_id or source_id < 1 or revision < 1
            or (expected_source_id is not None and source_id != expected_source_id)
            or (expected_revision is not None and revision != expected_revision)):
        raise TaskCommandContractError("task read-model identity mismatch")
    stored_event_id = str(row.get("event_id", ""))
    stored_command_id = str(row.get("command_id", ""))
    try:
        if str(uuid.UUID(stored_event_id)) != stored_event_id:
            raise ValueError
        if str(uuid.UUID(stored_command_id)) != stored_command_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise TaskCommandContractError("task read-model identity mismatch") from None
    if ((event_id is not None and stored_event_id != event_id)
            or (command_id is not None and stored_command_id != command_id)):
        raise TaskCommandContractError("task read-model identity mismatch")
    tombstone = row.get("tombstone")
    if tombstone not in {"0", "1", 0, 1}:
        raise TaskCommandContractError("task read model is inconsistent")
    if tombstone in {"1", 1}:
        return None
    envelope = {key: row.get(key, "") for key in (
        "algorithm", "key_version", "nonce", "ciphertext", "aad", "ciphertext_sha256",
    )}
    envelope["algorithm"] = "AES-256-GCM"
    stored_group_token = row.get("row_group_token")
    if envelope.get("key_version") == "task-event-v3":
        if (not isinstance(stored_group_token, str)
                or re.fullmatch(r"[0-9a-f]{64}", stored_group_token) is None):
            raise TaskCommandContractError("task read-model identity mismatch")
    else:
        stored_group_token = None
    return decrypt_task_snapshot(
        envelope, environment=environment, task_id=task_id,
        revision=revision, event_id=stored_event_id, command_id=stored_command_id,
        row_group_token=stored_group_token,
    )


async def _read_task_rows(redis_client, task_ids: list[str], *, environment: str,
                          run_id: str, parser_type: str) -> list[dict[str, Any]]:
    for task_id in task_ids:
        match = _TASK_ID_PATTERN.fullmatch(task_id)
        if match is None or match.group(1) != parser_type:
            raise TaskCommandContractError("task read model query failed")
    keys = [task_read_model_keys(environment, run_id, task_id)[0] for task_id in task_ids]
    if not keys:
        return []
    try:
        pipe = redis_client.pipeline(transaction=False)
        for key in keys:
            pipe.hgetall(key)
        rows = await pipe.execute()
    except Exception:
        raise TaskCommandContractError("task read model query failed") from None
    if not isinstance(rows, (list, tuple)) or len(rows) != len(task_ids):
        raise TaskCommandContractError("task read model query failed")
    return list(rows)


async def read_task_read_model_scan_records(
    redis_client, *, environment: str, run_id: str, parser_type: str,
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Batch-read and decrypt one bounded page emitted by a read-model scan."""
    _validate_parser_type(parser_type)
    if (not isinstance(records, list) or len(records) > TASK_READ_MODEL_SCAN_BATCH_MAX
            or any(not isinstance(record, dict) for record in records)):
        raise TaskCommandContractError("invalid task read-model scan records")
    task_ids: list[str] = []
    expected_revisions: list[int] = []
    for record in records:
        if set(record) != {"task_id", "indexed_revision"}:
            raise TaskCommandContractError("invalid task read-model scan records")
        task_id = record.get("task_id")
        revision = record.get("indexed_revision")
        if (not isinstance(task_id, str) or type(revision) is not int or revision < 1):
            raise TaskCommandContractError("invalid task read-model scan records")
        task_ids.append(task_id)
        expected_revisions.append(revision)
    if len(set(task_ids)) != len(task_ids):
        raise TaskCommandContractError("invalid task read-model scan records")
    stored_rows = await _read_task_rows(
        redis_client, task_ids, environment=environment, run_id=run_id,
        parser_type=parser_type,
    )
    decoded = []
    for task_id, revision, row in zip(task_ids, expected_revisions, stored_rows):
        try:
            source_id = int(row.get("source_id", 0))
            event_id = str(row.get("event_id", ""))
            command_id = str(row.get("command_id", ""))
            values = _decode_task_row(
                row, environment=environment, run_id=run_id, task_id=task_id,
                expected_source_id=source_id, expected_revision=revision,
                event_id=event_id, command_id=command_id,
            )
        except TaskCommandContractError:
            raise
        except Exception:
            raise TaskCommandContractError("task read model is inconsistent") from None
        if values is None:
            raise TaskCommandContractError("indexed task is tombstoned")
        decoded.append({
            "task_id": task_id,
            "source_id": source_id,
            "revision": revision,
            "event_id": event_id,
            "updated_at": row.get("updated_at", ""),
            "values": values,
        })
    return decoded


async def query_task_records(redis_client, *, environment: str, run_id: str, parser_type: str,
                             communities: list[str] | None, keyword: str | None,
                             filters: dict[str, list[str]], secret: str,
                             sort_by: str | None, sort_order: str,
                             offset: int, limit: int) -> tuple[list[dict[str, Any]], int]:
    _validate_parser_type(parser_type)
    _validate_query_page(offset, limit)
    _validate_sort(sort_by, sort_order)
    normalized_filters = _validate_filters(filters)
    normalized_keyword = _validate_search(keyword)
    if communities is not None:
        if (not isinstance(communities, list) or len(communities) > _MAX_VALUES_PER_FIELD
                or any(not isinstance(value, str) or len(value) > 512 for value in communities)):
            raise TaskCommandContractError("invalid task read-model filters")
        if not communities:
            return [], 0
    index_filters: dict[str, Any] = {"task_type": parser_type}
    if communities is not None:
        index_filters["community"] = communities
    index_filters.update({field: values for field, values in normalized_filters.items() if values})
    try:
        task_ids = await query_task_ids(
            redis_client, environment=environment, run_id=run_id, filters=index_filters,
            secret=secret, search=normalized_keyword, offset=0, limit=_MAX_PAGE_SIZE,
        )
        if len(task_ids) >= _MAX_PAGE_SIZE:
            raise TaskCommandContractError("task read-model result exceeds the query limit")
        stored_rows = await _read_task_rows(
            redis_client, task_ids, environment=environment, run_id=run_id,
            parser_type=parser_type,
        )
    except TaskCommandContractError:
        raise
    except Exception:
        raise TaskCommandContractError("task read model query failed") from None
    rows = []
    for task_id, row in zip(task_ids, stored_rows):
        task_key = task_read_model_keys(environment, run_id, task_id)[0]
        try:
            revision = int(row.get("revision", -1))
            event_id = str(row.get("event_id", ""))
            command_id = str(row.get("command_id", ""))
            values = _decode_task_row(
                row, environment=environment, run_id=run_id, task_id=task_id,
                expected_source_id=int(row.get("source_id", 0)),
                expected_revision=revision, event_id=event_id, command_id=command_id,
            )
        except TaskCommandContractError:
            raise
        except Exception:
            raise TaskCommandContractError("task read model is inconsistent") from None
        if values is None:
            continue
        if normalized_keyword and not any(
            isinstance(value, str) and normalized_keyword in value.lower()
            for value in values.values()
        ):
            continue
        if any(str(values.get(field, "") or "") not in allowed
               for field, allowed in normalized_filters.items() if allowed):
            continue
        if communities is not None and str(values.get("社区", "") or "") not in communities:
            continue
        rows.append({
            "task_id": task_id,
            "source_id": int(row.get("source_id", 0)),
            "revision": revision,
            "event_id": event_id,
            "updated_at": row.get("updated_at", ""),
            "values": values,
        })
    if sort_by:
        rows.sort(key=lambda row: (str(row["values"].get(sort_by, "") or "").lower(), row["task_id"]),
                  reverse=sort_order == "desc")
    else:
        rows.sort(key=lambda row: row["task_id"], reverse=sort_order == "desc")
    total = len(rows)
    return rows[offset:offset + limit], total


async def query_task_read_model_group_ids(
    redis_client, environment: str, run_id: str, parser_type: str,
    filters: dict[str, Any], communities: list[str] | None, search: str | None,
    offset: int, limit: int, *, state_filters: dict[str, list[Any]] | None = None,
) -> dict[str, Any]:
    """Return a bounded page of groups matching canonical-source indexes.

    Authorization communities are stricter: every active member in a group
    must belong to the supplied set. Missing community is always forbidden.
    """
    _validate_parser_type(parser_type)
    _validate_query_page(offset, limit)
    normalized_filters = _validate_filters(filters)
    normalized_search = _validate_search(search)
    normalized_state_filters: dict[str, list[str]] = {}
    if state_filters is not None:
        if not isinstance(state_filters, dict) or len(state_filters) > 16:
            raise TaskCommandContractError("invalid task state filters")
        for field, values in state_filters.items():
            if (field not in _STATE_INDEX_FIELDS or not isinstance(values, list)
                    or not values or len(values) > _MAX_VALUES_PER_FIELD):
                raise TaskCommandContractError("invalid task state filters")
            normalized_values = []
            for value in values:
                text = str(value).strip()
                if not text or len(text) > 512 or any(ord(char) < 32 for char in text):
                    raise TaskCommandContractError("invalid task state filters")
                if field == "watch.category_id" and not text.isdigit():
                    raise TaskCommandContractError("invalid task state filters")
                if field == "task_graph.blocked" and text not in {"true", "false"}:
                    raise TaskCommandContractError("invalid task state filters")
                normalized_values.append(text)
            normalized_state_filters[field] = list(dict.fromkeys(normalized_values))
    if communities is not None and (
        not isinstance(communities, list) or len(communities) > 500
        or any(not isinstance(value, str) or len(value) > 512 for value in communities)
    ):
        raise TaskCommandContractError("invalid task read-model filters")
    if communities is not None and not communities:
        return {"total": 0, "group_tokens": []}
    _task_key, _indexes_key, prefix = task_read_model_keys(
        environment, run_id, f"{parser_type}:1",
    )
    secret = _configured_task_event_secret()
    if normalized_state_filters:
        if await redis_client.get(f"{prefix}:state-index-version") != "v1":
            raise TaskCommandContractError("task state indexes are not ready")
    temporary: list[str] = []
    try:
        indexes = [f"{prefix}:gidx:all"]
        canonical_filters: dict[str, list[str]] = {"task_type": [parser_type]}
        canonical_filters.update({field: values for field, values in normalized_filters.items() if values})
        for field, values in canonical_filters.items():
            if not values:
                return {"total": 0, "group_tokens": []}
            keys = [
                f"{prefix}:gidx:{search_token(value, field=field, environment=environment, secret=secret)}"
                for value in values
            ]
            if len(keys) == 1:
                indexes.append(keys[0])
            else:
                union = f"{prefix}:group-query:{uuid.uuid4().hex}"
                temporary.append(union)
                await redis_client.execute_command("ZUNIONSTORE", union, len(keys), *keys, "AGGREGATE", "MAX")
                indexes.append(union)
        for field, values in normalized_state_filters.items():
            domain, state_field = field.split(".", 1)
            keys = [
                f"{prefix}:gidx-state:v1:{search_token(value, field=f'state:{domain}:{state_field}', environment=environment, secret=secret)}"
                for value in values
            ]
            if len(keys) == 1:
                indexes.append(keys[0])
            else:
                union = f"{prefix}:group-query:{uuid.uuid4().hex}"
                temporary.append(union)
                await redis_client.execute_command(
                    "ZUNIONSTORE", union, len(keys), *keys, "AGGREGATE", "MAX",
                )
                indexes.append(union)
        if normalized_search:
            grams = sorted({normalized_search[i:i + 2] for i in range(len(normalized_search) - 1)})
            indexes.extend(
                f"{prefix}:gidx:{search_token(gram, field='search', environment=environment, secret=secret)}"
                for gram in grams
            )
        candidates = f"{prefix}:group-query:{uuid.uuid4().hex}"
        temporary.append(candidates)
        await redis_client.execute_command(
            "ZINTERSTORE", candidates, len(indexes), *indexes, "AGGREGATE", "MIN",
        )
        if normalized_search:
            candidate_count = await redis_client.zcard(candidates)
            if type(candidate_count) is not int or candidate_count > _MAX_PAGE_SIZE:
                raise TaskCommandContractError("task read-model search exceeds query limit")
            exact_key = f"{prefix}:group-query:{uuid.uuid4().hex}"
            temporary.append(exact_key)
            for start in range(0, candidate_count, TASK_READ_MODEL_SCAN_BATCH_MAX):
                page = await redis_client.zrange(
                    candidates, start,
                    min(start + TASK_READ_MODEL_SCAN_BATCH_MAX, candidate_count) - 1,
                )
                tokens = [item.decode() if isinstance(item, bytes) else item for item in page]
                matched = []
                for token in tokens:
                    if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{64}", token) is None:
                        raise TaskCommandContractError("task read-model group query failed")
                    members = await redis_client.zrange(f"{prefix}:group:{token}:members", 0, 0)
                    if not members:
                        raise TaskCommandContractError("task read-model group is inconsistent")
                    encoded_member = members[0].decode() if isinstance(members[0], bytes) else members[0]
                    _source_id, separator, task_id = encoded_member.partition("|")
                    if not separator:
                        raise TaskCommandContractError("task read-model group is inconsistent")
                    rows = await _read_task_rows(
                        redis_client, [task_id], environment=environment,
                        run_id=run_id, parser_type=parser_type,
                    )
                    row = rows[0]
                    source_id = int(row.get("source_id", 0))
                    revision = int(row.get("revision", 0))
                    values = _decode_task_row(
                        row, environment=environment, run_id=run_id,
                        task_id=task_id, expected_source_id=source_id,
                        expected_revision=revision, event_id=str(row.get("event_id", "")),
                        command_id=str(row.get("command_id", "")),
                    )
                    if values is None or row.get("row_group_token") != token:
                        raise TaskCommandContractError("task read-model group identity mismatch")
                    if any(normalized_search in value.lower() for value in values.values()
                           if isinstance(value, str)):
                        matched.append(token)
                if matched:
                    await redis_client.execute_command("ZADD", exact_key, *[
                        item for token in matched for item in (1, token)
                    ])
            candidates = exact_key
        result_key = candidates
        if communities is not None:
            allowed = {
                search_token(value.strip(), field="group-community",
                             environment=environment, secret=secret)
                for value in communities if value.strip()
            }
            raw_tokens = await redis_client.smembers(f"{prefix}:community-tokens")
            tokens = {item.decode() if isinstance(item, bytes) else str(item) for item in raw_tokens}
            if len(tokens) > 500:
                raise TaskCommandContractError("task read-model community scope exceeds limit")
            forbidden_keys = [f"{prefix}:gidx-community:{token}" for token in sorted(tokens - allowed)]
            if forbidden_keys:
                forbidden = f"{prefix}:group-query:{uuid.uuid4().hex}"
                result_key = f"{prefix}:group-query:{uuid.uuid4().hex}"
                temporary.extend((forbidden, result_key))
                await redis_client.execute_command(
                    "ZUNIONSTORE", forbidden, len(forbidden_keys), *forbidden_keys, "AGGREGATE", "MAX",
                )
                await redis_client.execute_command(
                    "ZDIFFSTORE", result_key, 2, candidates, forbidden,
                )
        total = await redis_client.zcard(result_key)
        if type(total) is not int or total < 0:
            raise TaskCommandContractError("task read-model group query failed")
        if offset >= total:
            return {"total": total, "group_tokens": []}
        page = await redis_client.zrange(result_key, offset, offset + limit - 1)
        group_tokens = [item.decode() if isinstance(item, bytes) else item for item in page]
        if (len(group_tokens) > limit
                or any(not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{64}", token) is None
                       for token in group_tokens)):
            raise TaskCommandContractError("task read-model group query failed")
        return {"total": total, "group_tokens": group_tokens}
    except TaskCommandContractError:
        raise
    except Exception:
        raise TaskCommandContractError("task read-model group query failed") from None
    finally:
        if temporary:
            try:
                await redis_client.delete(*temporary)
            except Exception:
                raise TaskCommandContractError("task read-model group query failed") from None


async def count_task_read_model_groups(
    redis_client, environment: str, run_id: str, parser_type: str,
    filters: dict[str, Any], communities: list[str] | None, search: str | None,
    *, state_filters: dict[str, list[Any]] | None = None,
) -> int:
    """Count canonical groups from Redis indexes without fetching task payloads."""
    result = await query_task_read_model_group_ids(
        redis_client, environment, run_id, parser_type, filters, communities,
        search, 0, 1, state_filters=state_filters,
    )
    return int(result["total"])


async def read_task_read_model_group_records(
    redis_client, environment: str, run_id: str, parser_type: str,
    group_tokens: list[str],
) -> list[dict[str, Any]]:
    """Read complete source groups; group tokens never leave this service."""
    _validate_parser_type(parser_type)
    if (not isinstance(group_tokens, list) or len(group_tokens) > _MAX_PAGE_SIZE
            or any(not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{64}", token) is None
                   for token in group_tokens)
            or len(set(group_tokens)) != len(group_tokens)):
        raise TaskCommandContractError("invalid task read-model groups")
    _task_key, _indexes_key, prefix = task_read_model_keys(
        environment, run_id, f"{parser_type}:1",
    )
    try:
        groups = []
        read_count = 0
        from services.parsers import get_parser

        parser = get_parser(_parser_key_for_table(parser_type))
        for token in group_tokens:
            members_key = f"{prefix}:group:{token}:members"
            encoded_members = await redis_client.zrange(members_key, 0, -1)
            if not isinstance(encoded_members, (list, tuple)):
                raise TaskCommandContractError("task read-model group query failed")
            read_count += len(encoded_members)
            if read_count > 5_000:
                raise TaskCommandContractError("task read-model group exceeds limit")
            task_ids = []
            for entry in encoded_members:
                member = entry.decode() if isinstance(entry, bytes) else entry
                source_part, separator, task_id = member.partition("|")
                match = _TASK_ID_PATTERN.fullmatch(task_id) if separator else None
                if (not source_part.isdigit() or match is None or match.group(1) != parser_type):
                    raise TaskCommandContractError("task read-model group identity mismatch")
                task_ids.append((int(source_part), task_id))
            task_ids.sort()
            rows = await _read_task_rows(
                redis_client, [task_id for _source_id, task_id in task_ids],
                environment=environment, run_id=run_id, parser_type=parser_type,
            )
            sources = []
            for (member_source_id, task_id), row in zip(task_ids, rows):
                source_id = int(row.get("source_id", 0))
                revision = int(row.get("revision", 0))
                event_id = str(row.get("event_id", ""))
                command_id = str(row.get("command_id", ""))
                if (source_id != member_source_id or row.get("row_group_token") != token
                        or row.get("tombstone") not in {"0", 0}):
                    raise TaskCommandContractError("task read-model group identity mismatch")
                values = _decode_task_row(
                    row, environment=environment, run_id=run_id, task_id=task_id,
                    expected_source_id=source_id, expected_revision=revision,
                    event_id=event_id, command_id=command_id,
                )
                if values is None:
                    raise TaskCommandContractError("task read-model group is inconsistent")
                sources.append({
                    "task_id": task_id, "source_id": source_id, "revision": revision,
                    "physical_row": int(task_id.rpartition(":")[2]),
                    "event_id": event_id, "updated_at": row.get("updated_at", ""),
                    "values": values,
                })
            if not sources:
                raise TaskCommandContractError("task read-model group is inconsistent")
            canonical = dict(sources[0])
            canonical_values = canonical["values"]
            stable = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            groups.append({
                **canonical,
                "sources": sources,
                "source_count": len(sources),
                "conflict": any(stable(source["values"]) != stable(canonical_values) for source in sources[1:]),
                "row_key": parser.make_row_key(canonical_values),
                "parser_type": parser_type,
            })
        return groups
    except TaskCommandContractError:
        raise
    except Exception:
        raise TaskCommandContractError("task read-model group query failed") from None


async def task_read_model_watermark(redis_client, environment: str, run_id: str) -> int:
    if environment not in {"development", "staging", "production", "shadow"}:
        raise TaskCommandContractError("invalid task read-model environment")
    _task_key, _indexes_key, prefix = task_read_model_keys(environment, run_id, "t_fullchain:1")
    value = await redis_client.get(f"{prefix}:watermark")
    return int(value or 0)


async def require_task_read_model_ready(redis_client, environment: str, run_id: str) -> None:
    """Fail closed unless this exact environment/run completed read-model verify."""
    _task_key, _indexes_key, prefix = task_read_model_keys(environment, run_id, "t_fullchain:1")
    state = await redis_client.get(f"{prefix}:state")
    if state != f"ready:{run_id}":
        raise TaskCommandContractError("task read model is not ready")


async def read_task_snapshot(redis_client, task_key: str, *, environment: str,
                             run_id: str, task_id: str, event_id: str,
                             revision: int, command_id: str,
                             source_id: int | None = None) -> dict[str, Any] | None:
    expected_key = task_read_model_keys(environment, run_id, task_id)[0]
    if task_key != expected_key:
        raise TaskCommandContractError("task read-model identity mismatch")
    try:
        row = await redis_client.hgetall(expected_key)
    except Exception:
        raise TaskCommandContractError("task read model is unavailable") from None
    return _decode_task_row(
        row, environment=environment, run_id=run_id, task_id=task_id,
        expected_source_id=source_id, expected_revision=revision,
        event_id=event_id, command_id=command_id,
    )


__all__ = [
    "SEARCH_FIELDS", "TASK_READ_MODEL_SCAN_BATCH_MAX",
    "apply_read_model_update", "build_read_model_update",
    "close_task_read_model_scan", "create_task_read_model_scan", "index_key",
    "query_task_ids", "query_task_records", "read_task_read_model_scan",
    "query_task_read_model_group_ids", "read_task_read_model_group_records",
    "count_task_read_model_groups",
    "read_task_read_model_scan_records", "read_task_snapshot",
    "require_task_read_model_ready",
    "search_token", "task_read_model_watermark",
    "task_read_model_keys",
]
