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
from .reconciliation import summarize

FLOW_FIELDS = ("id", "parser_type", "row_key", "cycle_no", "source_id", "source_revision", "source_row_hash", "state", "flow_version", "review_due_date", "original_deadline", "previous_deadline", "feedback_submitted", "created_by", "last_actor_id", "last_action_at", "resolved_at", "finalized_at", "archived_at", "created_at", "updated_at")
REGISTRATION_FIELDS = ("parser_type", "row_key", "source_id", "source_revision", "source_row_hash", "identity_hmac", "last_address_hmac", "task_community", "property_id", "property_version", "status", "match_count", "selected_by", "selected_at", "confirmed_by", "manual_confirmed_at", "confirmed_at", "created_at", "updated_at")
FLOW_STATES = {"initial_pending", "initial_extension", "deep_pending", "deep_extension", "final_unverifiable", "resolved", "archived", "source_exception"}
REGISTRATION_STATES = {"awaiting_match", "matched_once", "review_required", "confirmation_pending", "confirmed", "cancelled", "legacy_completed", "pending_establishment"}
SCHEMA_DOMAINS = ("OnlineData", "OnlineDataArchive", "daily_report", "PlatformData",
                  "VisitData", "DispatchData", "RegistryData", "WorkflowData")
SCHEMA_KEYS = ("ONLINE_DATA", "ARCHIVE", "DAILY_REPORT", "PLATFORM", "VISIT",
               "DISPATCH", "REGISTRY", "WORKFLOW")


def source_settings(settings):
    if (settings.APP_ENVIRONMENT != "production" or not settings.MYSQL_DOMAIN_DATABASES_ENABLED
            or not settings.PLATFORM_DOMAIN_ACTIVE or not settings.REGISTRY_ADDRESS_DOMAIN_ACTIVE):
        raise SnapshotError("source_environment_or_domain_mismatch")
    names = {"ONLINE_DATA": "OnlineData", "PLATFORM": "PlatformData", "REGISTRY": "RegistryData",
             "ARCHIVE": "OnlineDataArchive"}
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


async def source_schema_contract(cur, settings):
    """Return structural metadata only; no row value or credential is read."""
    contract = {}
    for key, domain in zip(SCHEMA_KEYS, SCHEMA_DOMAINS):
        database = getattr(settings, "MYSQL_" + key + "_DB")
        if database != domain:
            raise SnapshotError("source_database_name_mismatch")
        await cur.execute("SELECT table_name FROM information_schema.tables "
                          "WHERE table_schema=%s AND table_type='BASE TABLE' ORDER BY table_name", (database,))
        tables = [row[0] for row in await cur.fetchall()]
        if not tables:
            raise SnapshotError("source_schema_empty")
        contract[domain] = {}
        for table in tables:
            await cur.execute("SELECT column_name,column_type,is_nullable,column_default,extra,generation_expression "
                              "FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                              (database, table))
            columns = [list(row) for row in await cur.fetchall()]
            await cur.execute("SELECT index_name,non_unique,seq_in_index,column_name,sub_part,index_type "
                              "FROM information_schema.statistics WHERE table_schema=%s AND table_name=%s "
                              "ORDER BY index_name,seq_in_index", (database, table))
            indexes = [list(row) for row in await cur.fetchall()]
            await cur.execute("SELECT constraint_name,constraint_type FROM information_schema.table_constraints "
                              "WHERE table_schema=%s AND table_name=%s ORDER BY constraint_name", (database, table))
            constraints = [list(row) for row in await cur.fetchall()]
            contract[domain][table] = {"columns": columns, "indexes": indexes, "constraints": constraints}
    return contract


async def build(conn, snapshot_id, salt, *, settings, exclude_orphan_property_links=False,
                recover_model_three_sources=False, staging_sample_mode=False,
                staging_sample_limit=150):
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
            schema_contract = await source_schema_contract(cur, settings)
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
            observed_source_count = len(sources)
            recovered_source_count = 0
            excluded_stale_model_three_source_count = 0
            recovery_scope = None
            if recover_model_three_sources:
                from .recovery import PARSER, LEDGER_FIELDS, reconstruct, recovery_scope as select_recovery_scope
                parser = get_parser(PARSER)
                business = await select(cur, 'OnlineData.' + parser.table_name,
                    ('id', '_row_key', *parser.COLUMNS))
                # Historical model-three source rows can outlive their active
                # business rows. They are not recovery candidates: in sample
                # mode, only source references backed by a current business
                # row may enter the Staging snapshot.
                if staging_sample_mode:
                    active_business_keys = {(row['id'], row['_row_key']) for row in business}
                    retained_sources = []
                    for source in sources:
                        if source['parser_type'] != PARSER:
                            retained_sources.append(source)
                            continue
                        source_key = (source['physical_row'], source['row_key'])
                        if source_key in active_business_keys:
                            retained_sources.append(source)
                        else:
                            excluded_stale_model_three_source_count += 1
                    sources = retained_sources
                ledgers = await select(cur, 'OnlineData._local_source_records',
                    LEDGER_FIELDS, ' WHERE parser_type=%s', (PARSER,))
                # A nonlocal current source must not be silently replaced.
                other_sources = await select(cur, 'OnlineData._online_source_rows', ('id',),
                    ' WHERE archived_at IS NULL AND spreadsheet_id<>0 AND parser_type=%s', (PARSER,))
                if other_sources:
                    raise SnapshotError('source_recovery_nonlocal_source_present')
                all_ids = await select(cur, 'OnlineData._online_source_rows', ('id',))
                reserved = [r['id'] for r in all_ids]
                reserved.extend(r['source_id'] for r in [*flows, *registrations] if r['source_id'] is not None)
                selected_business, recovery_scope = select_recovery_scope(
                    parser, business, sources,
                    community_digest=lambda value: codec.digest('recovery_community', normalized(value))[:16],
                    ledgers=ledgers,
                    sample_mode=staging_sample_mode,
                    sample_limit=staging_sample_limit,
                )
                recovered, recovery_exclusions = reconstruct(parser, selected_business, sources, ledgers, reserved,
                    community_digest=lambda value: codec.digest('recovery_community', normalized(value)),
                    exclude_approved_conflicts=True, return_diagnostics=True,
                    sample_mode=staging_sample_mode, sample_limit=staging_sample_limit)
                recovered_source_count = len(recovered)
                recovery_scope['excluded_ledger_conflicts'] = recovery_exclusions
                recovery_scope['approved_ledger_conflict_exclusion'] = True
                sources = [*sources, *recovered]
            current = {(row["parser_type"], row["row_key"]): row for row in sources}
            if len(current) != len(sources):
                # Keep the failure code stable, but expose only bounded counts so
                # the staging gateway can distinguish an existing source-key
                # collision from a recovered row colliding with a current row.
                source_key_counts = {}
                for row in sources:
                    key = (row["parser_type"], row["row_key"])
                    source_key_counts[key] = source_key_counts.get(key, 0) + 1
                duplicate_source_key_count = sum(count - 1 for count in source_key_counts.values()
                                                  if count > 1)
                recovered_keys = {
                    (row["parser_type"], row["row_key"])
                    for row in recovered if row["parser_type"] == PARSER
                } if recover_model_three_sources else set()
                pre_recovery_keys = {
                    (row["parser_type"], row["row_key"])
                    for row in sources[:-recovered_source_count]
                } if recovered_source_count else set(source_key_counts)
                pre_recovery_key_counts = {}
                for row in sources[:-recovered_source_count] if recovered_source_count else sources:
                    key = (row["parser_type"], row["row_key"])
                    pre_recovery_key_counts[key] = pre_recovery_key_counts.get(key, 0) + 1
                pre_recovery_duplicate_source_key_count = sum(
                    count - 1 for count in pre_recovery_key_counts.values() if count > 1
                )
                recovered_source_collision_count = sum(
                    1 for key in recovered_keys if key in pre_recovery_keys
                )
                collision_by_type = {}
                collision_communities = {}
                collision_dates = []
                collision_pairs = []
                existing_by_key = {}
                existing_by_physical = {}
                if recovered_source_count:
                    pre_recovery_sources = sources[:-recovered_source_count]
                    for row in pre_recovery_sources:
                        key = (row["parser_type"], row["row_key"])
                        existing_by_key.setdefault(key, []).append(row)
                        physical = (row["parser_type"], row["physical_row"])
                        existing_by_physical.setdefault(physical, []).append(row)
                    parser = get_parser(PARSER)
                    for row in recovered:
                        key = (row["parser_type"], row["row_key"])
                        physical = (row["parser_type"], row["physical_row"])
                        business_matches = existing_by_key.get(key, [])
                        source_matches = existing_by_physical.get(physical, [])
                        has_business_key = bool(business_matches)
                        has_source_reference = bool(source_matches)
                        if has_business_key and has_source_reference:
                            kind = "business_key_and_source_reference"
                        elif has_business_key:
                            kind = "business_key"
                        elif has_source_reference:
                            kind = "source_reference"
                        else:
                            continue
                        collision_by_type[kind] = collision_by_type.get(kind, 0) + 1
                        try:
                            values = json.loads(row["values_json"])
                        except (TypeError, ValueError):
                            values = {}
                        community = normalized(values.get(parser.COMMUNITY_COLUMN))
                        if community:
                            digest = codec.digest("recovery_collision_community", community)[:16]
                            collision_communities[digest] = collision_communities.get(digest, 0) + 1
                        related = business_matches or source_matches
                        for existing in related[:2]:
                            collision_pairs.append({
                                "candidate_task_key": codec.digest("recovery_collision_task", row["physical_row"]),
                                "existing_source_key": codec.digest("recovery_collision_source", existing["id"]),
                                "business_key": codec.digest("recovery_collision_business", row["row_key"]),
                                "relation": "business_key" if existing in business_matches else "source_reference",
                            })
                        for field in ("下发日期", "下发时间", "截止日期", "截止时间"):
                            value = str(values.get(field) or "").strip()
                            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                                collision_dates.append(value)
                            elif re.fullmatch(r"\d{1,2}[-.]\d{1,2}", value):
                                collision_dates.append(value)
                            if collision_dates and collision_dates[-1] == value:
                                break
                raise SnapshotError("duplicate_current_business_key", diagnostics={
                    "source_count": len(sources),
                    "observed_source_count": len(sources) - recovered_source_count,
                    "recovered_candidate_count": recovered_source_count,
                    "duplicate_source_key_count": duplicate_source_key_count,
                    "pre_recovery_duplicate_source_key_count": pre_recovery_duplicate_source_key_count,
                    "recovered_source_collision_count": recovered_source_collision_count,
                    "collision_by_type": collision_by_type,
                    "collision_by_community": [
                        {"community_key": key, "count": collision_communities[key]}
                        for key in sorted(collision_communities)
                    ],
                    "collision_pairs": collision_pairs[:256],
                    "collision_date_min": min(collision_dates) if collision_dates else None,
                    "collision_date_max": max(collision_dates) if collision_dates else None,
                })
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
                if recover_model_three_sources and parser_type == '疑似未注销模型三':
                    # Keep all already-current rows and append only the
                    # deliberately selected normal sample. The recovery
                    # sample must never make existing current data disappear.
                    selected_business_keys = {(row['physical_row'], row['row_key']) for row in selected}
                    selected_business_keys.update(
                        (row['physical_row'], row['row_key'])
                        for row in sources if row['parser_type'] == parser_type
                    )
                    business = [row for row in business if (row['id'], row['_row_key']) in selected_business_keys]
                business_keys = {(row['id'], row['_row_key']) for row in business}
                source_keys = {(row['physical_row'], row['row_key']) for row in selected}
                if business_keys != source_keys:
                    ledgers = await select(cur, 'OnlineData._local_source_records',
                        ('local_task_id', 'business_key', 'status'), ' WHERE parser_type=%s', (parser_type,))
                    archive = await select(cur, 'OnlineDataArchive.' + parser.table_name + '_archive', ('_row_key',))
                    raise SnapshotError("business_source_count_or_key_mismatch", diagnostics={
                        "parser_type": parser_type,
                        **summarize(business, selected, ledgers, {r['_row_key'] for r in archive}),
                    })
                if (len({r['_row_key'] for r in business}) != len(business)
                        or len({r['row_key'] for r in selected}) != len(selected)):
                    raise SnapshotError('duplicate_current_business_key', diagnostics={
                        'parser_type': parser_type, **summarize(business, selected, [], set())})
                codec.allocate(parser.table_name,[row["id"] for row in business])
                tables["OnlineData."+parser.table_name]=[]
                new_keys=set()
                for row in selected:
                    values=json.loads(row["values_json"]) if isinstance(row["values_json"],str) else row["values_json"]
                    raw_values[(parser_type,row['row_key'])]=values
                    try:
                        source_communities[(parser_type,row['row_key'])]=communities[normalized(values[parser.COMMUNITY_COLUMN])]
                    except KeyError:
                        raise SnapshotError('task_community_unresolved') from None
                    safe=transform_values(parser,TASK_WORKFLOWS[parser_type],values,communities,codec)
                    entry=source_record(parser,{key:row[key] for key in ("id","physical_row","revision","row_key","source_kind")},safe,codec)
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
            codec.assert_tables_safe(tables)
            result["report"].update({"snapshot_id":snapshot_id,"current_task_count":len(sources),
                "categorical_value_overlaps":[{'table':table,'field':field,'count':count}
                    for (table,field),count in sorted(codec.categorical_overlaps.items())],
                "flow_count":len(retained_flows),"registration_count":len(retained_registrations),
                "excluded_noncurrent_flows":len(flows)-len(retained_flows),
                "excluded_noncurrent_registrations":len(registrations)-len(retained_registrations),
                "output_counts":{key:len(value) for key,value in tables.items()},
                "selected_source_counts":{**{key:len(value) for key,value in raw_registry.items()},
                    **{key:len(value) for key,value in raw_organization.items()},
                    "OnlineData._online_source_rows":observed_source_count,
                    "OnlineData._unverifiable_review_flows":len(flows),
                    "OnlineData._task_registration_links":len(registrations)},
                "pending_gates":["registration_hmac_rebuild","candidate_database_import","target_verification"],
                "recovered_model_three_source_count": recovered_source_count,
                "excluded_stale_model_three_source_count": excluded_stale_model_three_source_count,
                "recovery_scope": recovery_scope,
                "scope":"current_tasks_organization_and_registry_graph","ready_for_application_switch":False})
            result["schema_contract"] = schema_contract
            return result
    finally:
        await conn.rollback()
