"""Read a reviewed production scope in one consistent, read-only transaction.

No connection is constructed here. The audited server entry point must prove
the production source identity; the import path never receives its credentials.
Only the sanitized result may be serialized by the caller.
"""
from __future__ import annotations
import json
import re
from .codec import Codec, SnapshotError, normalized, integer, date_value, enum
from .registry import FIELDS, transform as transform_registry
from .tasks import TASK_TYPES, transform_values, source_record
from .fences import transform_fence
from .organization import FIELDS as ORGANIZATION_FIELDS, transform as transform_organization
from .relations import ADDRESS_FIELDS, REVIEW_EVENT_FIELDS, REGISTRATION_EVENT_FIELDS, address_rows, history_rows
from .digests import identity_digest, address_digest, annotation_digest, matching_state

FLOW_FIELDS = ("id", "parser_type", "row_key", "cycle_no", "source_id", "source_revision", "source_row_hash", "state", "flow_version", "review_due_date", "original_deadline", "previous_deadline", "feedback_submitted", "created_by", "last_actor_id", "last_action_at", "resolved_at", "finalized_at", "archived_at", "created_at", "updated_at")
REGISTRATION_FIELDS = ("parser_type", "row_key", "source_id", "source_revision", "source_row_hash", "identity_hmac", "last_address_hmac", "task_community", "property_id", "property_version", "status", "match_count", "selected_by", "selected_at", "confirmed_by", "manual_confirmed_at", "confirmed_at", "created_at", "updated_at")
FLOW_STATES = {"initial_pending", "initial_extension", "deep_pending", "deep_extension", "final_unverifiable", "resolved", "archived", "source_exception"}
REGISTRATION_STATES = {"awaiting_match", "matched_once", "review_required", "confirmation_pending", "confirmed", "cancelled", "legacy_completed", "pending_establishment"}


def source_settings(settings):
    if (settings.APP_ENVIRONMENT != "production" or not settings.MYSQL_DOMAIN_DATABASES_ENABLED
            or not settings.PLATFORM_DOMAIN_ACTIVE or not settings.REGISTRY_ADDRESS_DOMAIN_ACTIVE):
        raise SnapshotError("source_environment_or_domain_mismatch")
    names = {"ONLINE_DATA": "OnlineData", "PLATFORM": "PlatformData", "REGISTRY": "RegistryData"}
    if any(getattr(settings, "MYSQL_" + key + "_DB") != value for key,value in names.items()):
        raise SnapshotError("source_database_name_mismatch")


async def select(cur, qualified, columns, where="", params=()):
    # All identifiers originate in this reviewed module/parser registry.
    if not re.fullmatch(r"[A-Za-z_]+\.[A-Za-z_]+", qualified):
        raise SnapshotError("invalid_table_identifier")
    database, table = qualified.split(".")
    if any("`" in column for column in columns):
        raise SnapshotError("invalid_column_identifier")
    sql = "SELECT " + ",".join("`" + column + "`" for column in columns)
    sql += " FROM `" + database + "`.`" + table + "`" + where
    await cur.execute(sql, params)
    return [dict(zip(columns,row)) for row in await cur.fetchall()]


async def build(conn, snapshot_id, salt, *, settings, exclude_orphan_property_links=False):
    # Required at the public boundary, before obtaining a cursor or reading data.
    source_settings(settings)
    from services.parsers import get_parser
    from services.task_workflow import TASK_WORKFLOWS
    if not re.fullmatch(r"staging-[a-z0-9]{16}", snapshot_id):
        raise SnapshotError("invalid_snapshot_id")
    codec = Codec(salt)
    try:
        async with conn.cursor() as cur:
            await cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            await cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
            raw_organization = {table: await select(cur, table, columns) for table, columns in ORGANIZATION_FIELDS.items()}
            raw_registry = {table: await select(cur, table, columns) for table, columns in FIELDS.items()}
            sources = await select(cur, "OnlineData._online_source_rows",
                ("id", "parser_type", "physical_row", "revision", "row_key", "row_hash", "values_json", "source_kind"),
                " WHERE archived_at IS NULL AND spreadsheet_id=0 ORDER BY id")
            allowed_source_kinds = {'local_table', 'local_dispatch', 'one_time_continuation_import'}
            unsupported = {}
            for row in sources:
                if row["parser_type"] not in TASK_TYPES or row["source_kind"] not in allowed_source_kinds:
                    parser_label = row["parser_type"] if row["parser_type"] in TASK_TYPES else "unknown_parser"
                    source_label = row["source_kind"] if row["source_kind"] in allowed_source_kinds else "unknown_source_kind"
                    key = f"{parser_label}|{source_label}"
                    unsupported[key] = unsupported.get(key, 0) + 1
            if unsupported:
                raise SnapshotError("unsupported_current_source", diagnostics={
                    "by_parser_and_source_kind": unsupported,
                    "total": sum(unsupported.values()),
                })
            if len({(row["parser_type"], row["physical_row"]) for row in sources}) != len(sources):
                raise SnapshotError("ambiguous_current_source")
            flows = await select(cur,"OnlineData._unverifiable_review_flows",FLOW_FIELDS)
            registrations = await select(cur,"OnlineData._task_registration_links",REGISTRATION_FIELDS)
            current = {(row["parser_type"], row["row_key"]): row for row in sources}
            if len(current) != len(sources):
                raise SnapshotError("duplicate_current_business_key")
            retained_flows = [row for row in flows if (row["parser_type"],row["row_key"]) in current]
            retained_registrations = [row for row in registrations if (row["parser_type"],row["row_key"]) in current]
            addresses = await select(cur,"OnlineData._online_task_address_matches",ADDRESS_FIELDS)
            addresses = [row for row in addresses if (row['parser_type'],row['row_key']) in current]
            flow_map = {row['id']:row for row in retained_flows}
            review_events = await select(cur,"OnlineData._unverifiable_review_events",REVIEW_EVENT_FIELDS)
            review_events = [row for row in review_events if row['flow_id'] in flow_map]
            registration_events = await select(cur,"OnlineData._task_registration_events",REGISTRATION_EVENT_FIELDS)
            registration_events = [row for row in registration_events if (row['parser_type'],row['row_key']) in current]
            actor_ids = {row[field] for rows, fields in ((retained_flows,("created_by","last_actor_id")),(retained_registrations,("selected_by","confirmed_by"))) for row in rows for field in fields if row[field] is not None}
            actor_ids |= {row[field] for rows,fields in ((addresses,('confirmed_by','manual_unmatched_by')),
                (review_events,('actor_user_id',)),(registration_events,('actor_user_id',)))
                for row in rows for field in fields if row[field] is not None}
            known_actors = {row["id"] for row in raw_organization["PlatformData._users"]}
            actor_ids |= {row["confirmed_by"] for row in raw_registry["RegistryData.registry_property_small_community_links"] if row["confirmed_by"] is not None}
            if not actor_ids <= known_actors:
                raise SnapshotError("historical_actor_missing")
            result = transform_registry(raw_registry,codec,actor_ids=known_actors,
                exclude_orphan_property_links=exclude_orphan_property_links)
            tables = result["tables"]
            aliases = await select(cur,"PlatformData._community_aliases",("community_id","alias"))
            communities = {}
            for row in raw_registry["PlatformData._communities"]:
                communities[normalized(row["name"])]=row["id"]
            for row in aliases:
                key=normalized(row["alias"])
                codec.remember(row["alias"])
                if key in communities and communities[key] != row["community_id"]:
                    raise SnapshotError("ambiguous_community_alias")
                codec.reference("community",row["community_id"],nullable=False)
                communities[key]=row["community_id"]
            tables.update(transform_organization(raw_organization,codec,communities))
            codec.allocate("source",[row["id"] for row in sources])
            codec.allocate("flow",[row["id"] for row in retained_flows])
            tables["OnlineData._online_source_rows"]=[]
            tables["OnlineData._local_source_records"]=[]
            remapped={}
            raw_values={}
            source_communities={}
            for parser_type in TASK_TYPES:
                parser=get_parser(parser_type)
                business=await select(cur,"OnlineData."+parser.table_name,("id","_row_key"))
                selected=[row for row in sources if row["parser_type"]==parser_type]
                business_keys = {(row['id'], row['_row_key']) for row in business}
                source_keys = {(row['physical_row'], row['row_key']) for row in selected}
                if business_keys != source_keys:
                    raise SnapshotError("business_source_count_or_key_mismatch", diagnostics={
                        "parser_type": parser_type,
                        "source_count": len(source_keys),
                        "business_count": len(business_keys),
                        "source_only_count": len(source_keys - business_keys),
                        "business_only_count": len(business_keys - source_keys),
                    })
                codec.allocate(parser.table_name,[row["id"] for row in business])
                tables["OnlineData."+parser.table_name]=[]
                new_keys=set()
                for row in selected:
                    values=json.loads(row["values_json"]) if isinstance(row["values_json"],str) else row["values_json"]
                    raw_values[(parser_type,row['row_key'])]=values
                    source_communities[(parser_type,row['row_key'])]=communities[normalized(values[parser.COMMUNITY_COLUMN])]
                    safe=transform_values(parser,TASK_WORKFLOWS[parser_type],values,communities,codec)
                    entry=source_record(parser,{key:row[key] for key in ("id","physical_row","revision","row_key")},safe,codec)
                    new_key=entry["source"]["row_key"]
                    if new_key in new_keys:
                        raise SnapshotError("sanitized_business_key_collision")
                    new_keys.add(new_key)
                    tables["OnlineData."+parser.table_name].append(entry["task"])
                    tables["OnlineData._online_source_rows"].append(entry["source"])
                    tables["OnlineData._local_source_records"].append(entry["local_record"])
                    remapped[(parser_type,row["row_key"])]=entry
            tables["OnlineData._unverifiable_review_flows"]=[]
            for row in retained_flows:
                entry=remapped[(row["parser_type"],row["row_key"])]; source=entry["source"]
                if row["source_id"] is not None and codec.reference("source",row["source_id"])!=source["id"]:
                    raise SnapshotError("flow_source_identity_mismatch")
                safe={"id":codec.reference("flow",row["id"]),"parser_type":row["parser_type"],
                    "row_key":source["row_key"],"source_id":source["id"] if row['source_id'] is not None else None,
                    **transform_fence(row,current[(row["parser_type"],row["row_key"])],source,codec),
                    "state":enum(row["state"],FLOW_STATES,empty=False),
                    "cycle_no":integer(row["cycle_no"],minimum=1),"flow_version":integer(row["flow_version"],minimum=1),
                    "feedback_submitted":integer(row["feedback_submitted"],maximum=1),
                    "created_by":codec.reference("actor",row["created_by"]),"last_actor_id":codec.reference("actor",row["last_actor_id"]),
                    "safe_reason_code":"staging_snapshot"}
                from .tasks import business_date
                for field in ("original_deadline", "previous_deadline"):
                    safe[field] = business_date(row[field])
                for field in ("review_due_date","last_action_at","resolved_at","finalized_at","archived_at","created_at","updated_at"):
                    safe[field]=date_value(row[field])
                tables["OnlineData._unverifiable_review_flows"].append(safe)
            # Formal property IDs survive only through the remapped property
            # graph. No guessed house or cross-community fallback is generated.
            tables["OnlineData._task_registration_links"]=[]
            result['registration_digest_states']=[]
            properties={row['id']:row for row in raw_registry['RegistryData.registry_properties']}
            for row in retained_registrations:
                entry=remapped[(row["parser_type"],row["row_key"])]; source=entry["source"]
                if row["source_id"] is not None and codec.reference("source",row["source_id"])!=source["id"]:
                    raise SnapshotError("registration_source_identity_mismatch")
                parser=get_parser(row["parser_type"])
                context_key=(row['parser_type'],row['row_key'])
                raw_community=normalized(row['task_community'])
                if raw_community and raw_community not in communities:
                    raise SnapshotError('registration_community_unresolved')
                community='验证社区'+str(codec.reference('community',communities[raw_community])) if raw_community else ''
                codec.remember(row['task_community'])
                current_identity=identity_digest(raw_values[context_key],TASK_WORKFLOWS[row['parser_type']].identity_fields,settings.registry_hmac_key)
                current_address=address_digest(properties[row['property_id']]['natural_address'],settings.registry_hmac_key) if row['property_id'] in properties else ''
                result['registration_digest_states'].append({'parser_type':row['parser_type'],'row_key':source['row_key'],
                    'identity':matching_state(row['identity_hmac'],current_identity),
                    'address':matching_state(row['last_address_hmac'],current_address)})
                safe={"parser_type":row["parser_type"],"row_key":source["row_key"],"source_id":source["id"] if row['source_id'] is not None else None,
                    **transform_fence(row,current[(row["parser_type"],row["row_key"])],source,codec),
                    "task_community":community,
                    "property_id":codec.reference("property",row["property_id"]),"property_version":row["property_version"],
                    "status":enum(row["status"],REGISTRATION_STATES,empty=False),"match_count":integer(row["match_count"],maximum=255),
                    "selected_by":codec.reference("actor",row["selected_by"]),"confirmed_by":codec.reference("actor",row["confirmed_by"]),
                    "identity_hmac":codec.digest('registration_identity',row['identity_hmac']) if row['identity_hmac'] else '',
                    "last_address_hmac":codec.digest('registration_address',row['last_address_hmac']) if row['last_address_hmac'] else '',
                    "last_scan_token":"","reason_code":"staging_snapshot","manual_reason":"","manual_note":""}
                for field in ("selected_at","manual_confirmed_at","confirmed_at","created_at","updated_at"):
                    safe[field]=date_value(row[field])
                tables["OnlineData._task_registration_links"].append(safe)
            tables['OnlineData._online_task_address_matches'], annotation_states = address_rows(addresses,remapped,source_communities,codec)
            result['annotation_digest_states']=[]
            for row,metadata in zip(addresses,annotation_states):
                key=(row['parser_type'],row['row_key'])
                values=raw_values[key]
                parser=get_parser(row['parser_type'])
                expected=annotation_digest(values,TASK_WORKFLOWS[row['parser_type']],values[parser.COMMUNITY_COLUMN],settings.registry_hmac_key)
                result['annotation_digest_states'].append({**metadata,
                    'state':matching_state(row['manual_unmatched_address_hmac'],expected)})
            tables.update(history_rows(review_events,registration_events,flow_map,current,remapped,codec))
            matches = codec.scan_tables(tables)
            if matches:
                raise SnapshotError("source_sensitive_value_detected", diagnostics={
                    'match_count': matches,
                    'fields': codec.scan_table_summary(tables),
                })
            result["report"].update({"snapshot_id":snapshot_id,"current_task_count":len(sources),
                "flow_count":len(retained_flows),"registration_count":len(retained_registrations),
                "excluded_noncurrent_flows":len(flows)-len(retained_flows),
                "excluded_noncurrent_registrations":len(registrations)-len(retained_registrations),
                "output_counts":{key:len(value) for key,value in tables.items()},
                "selected_source_counts":{**{key:len(value) for key,value in raw_registry.items()},
                    **{key:len(value) for key,value in raw_organization.items()},
                    "OnlineData._online_source_rows":len(sources),
                    "OnlineData._unverifiable_review_flows":len(flows),
                    "OnlineData._task_registration_links":len(registrations)},
                "pending_gates":["registration_hmac_rebuild","candidate_database_import","target_verification"],
                "scope":"current_tasks_organization_and_registry_graph","ready_for_application_switch":False})
            return result
    finally:
        await conn.rollback()
