"""初始化新业务域数据库。

这些表只保存领域数据和稳定引用，不建立跨数据库外键。新库缺失时由
``DatabaseManager`` 记录并让旧业务继续启动，迁移窗口创建数据库后即可启用。
"""


async def _ensure_column(cur, table: str, column: str, definition: str) -> None:
    await cur.execute(f"SHOW COLUMNS FROM `{table}` LIKE %s", (column,))
    if not await cur.fetchone():
        await cur.execute(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}")


async def ensure_registry_schema(cur) -> None:
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS registry_properties (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            street VARCHAR(200) NOT NULL DEFAULT '',
            community_id BIGINT DEFAULT NULL,
            community_name_snapshot VARCHAR(200) NOT NULL DEFAULT '',
            natural_address VARCHAR(500) NOT NULL DEFAULT '',
            building VARCHAR(100) NOT NULL DEFAULT '',
            room VARCHAR(100) NOT NULL DEFAULT '',
            normalized_address VARCHAR(1000) NOT NULL DEFAULT '',
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            current_version INT UNSIGNED NOT NULL DEFAULT 1,
            created_by BIGINT DEFAULT NULL,
            updated_by BIGINT DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_registry_property_community (community_id, status),
            INDEX idx_registry_property_address (normalized_address(255), status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS registry_property_units (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            property_id BIGINT NOT NULL,
            unit_code VARCHAR(100) NOT NULL,
            unit_type VARCHAR(30) NOT NULL DEFAULT 'room',
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_registry_property_unit (property_id, unit_code),
            INDEX idx_registry_unit_status (status, property_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS registry_property_address_versions (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            property_id BIGINT NOT NULL,
            version_no INT UNSIGNED NOT NULL,
            street VARCHAR(200) NOT NULL DEFAULT '',
            natural_address VARCHAR(500) NOT NULL DEFAULT '',
            building VARCHAR(100) NOT NULL DEFAULT '',
            room VARCHAR(100) NOT NULL DEFAULT '',
            normalized_address VARCHAR(1000) NOT NULL DEFAULT '',
            effective_from DATETIME NOT NULL,
            effective_to DATETIME DEFAULT NULL,
            source_type VARCHAR(30) NOT NULL DEFAULT 'manual',
            source_ref VARCHAR(190) NOT NULL DEFAULT '',
            change_reason VARCHAR(500) NOT NULL DEFAULT '',
            changed_by BIGINT DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uk_registry_property_version (property_id, version_no),
            INDEX idx_registry_property_version_time (property_id, effective_from)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS registry_address_aliases (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            property_id BIGINT DEFAULT NULL,
            alias VARCHAR(500) NOT NULL,
            normalized_alias VARCHAR(500) NOT NULL,
            community_id BIGINT DEFAULT NULL,
            enabled TINYINT(1) NOT NULL DEFAULT 1,
            source_type VARCHAR(30) NOT NULL DEFAULT 'manual',
            source_ref VARCHAR(190) NOT NULL DEFAULT '',
            created_by BIGINT DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_registry_alias (normalized_alias, community_id),
            INDEX idx_registry_alias_property (property_id, enabled),
            INDEX idx_registry_alias_community (community_id, enabled)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS registry_housing_people (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            identity_number VARCHAR(50) DEFAULT NULL,
            identity_hmac CHAR(64) DEFAULT NULL,
            identity_hmac_version SMALLINT UNSIGNED DEFAULT NULL,
            is_temporary TINYINT(1) NOT NULL DEFAULT 0,
            verification_status VARCHAR(20) NOT NULL DEFAULT 'unverified',
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            merged_into_id BIGINT DEFAULT NULL,
            source_type VARCHAR(30) NOT NULL DEFAULT 'manual',
            source_ref VARCHAR(190) NOT NULL DEFAULT '',
            created_by BIGINT DEFAULT NULL,
            updated_by BIGINT DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_registry_housing_identity (identity_hmac),
            INDEX idx_registry_housing_name (name),
            INDEX idx_registry_housing_status (status, is_temporary),
            INDEX idx_registry_housing_merged (merged_into_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS registry_person_phones (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            person_id BIGINT NOT NULL,
            phone VARCHAR(200) NOT NULL,
            phone_hmac CHAR(64) NOT NULL,
            hmac_version SMALLINT UNSIGNED NOT NULL DEFAULT 1,
            is_primary TINYINT(1) NOT NULL DEFAULT 0,
            verified TINYINT(1) NOT NULL DEFAULT 0,
            valid_from DATETIME DEFAULT NULL,
            valid_to DATETIME DEFAULT NULL,
            source_type VARCHAR(30) NOT NULL DEFAULT 'manual',
            source_ref VARCHAR(190) NOT NULL DEFAULT '',
            created_by BIGINT DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_registry_person_phone (person_id, valid_to, is_primary),
            INDEX idx_registry_phone_hmac (phone_hmac),
            INDEX idx_registry_phone_valid (phone_hmac, valid_from, valid_to)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS registry_organizations (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(200) NOT NULL,
            organization_type VARCHAR(30) NOT NULL DEFAULT 'other',
            license_number VARCHAR(100) NOT NULL DEFAULT '',
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            notes VARCHAR(1000) NOT NULL DEFAULT '',
            source_type VARCHAR(30) NOT NULL DEFAULT 'manual',
            source_ref VARCHAR(190) NOT NULL DEFAULT '',
            created_by BIGINT DEFAULT NULL,
            updated_by BIGINT DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_registry_org_name (name),
            INDEX idx_registry_org_type_status (organization_type, status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS registry_organization_memberships (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            organization_id BIGINT NOT NULL,
            person_id BIGINT NOT NULL,
            title VARCHAR(100) NOT NULL DEFAULT '',
            valid_from DATETIME DEFAULT NULL,
            valid_to DATETIME DEFAULT NULL,
            verified TINYINT(1) NOT NULL DEFAULT 0,
            source_type VARCHAR(30) NOT NULL DEFAULT 'manual',
            source_ref VARCHAR(190) NOT NULL DEFAULT '',
            created_by BIGINT DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_registry_membership_org (organization_id, valid_to),
            INDEX idx_registry_membership_person (person_id, valid_to)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS registry_role_types (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            code VARCHAR(50) NOT NULL UNIQUE,
            name VARCHAR(100) NOT NULL UNIQUE,
            subject_type VARCHAR(20) NOT NULL DEFAULT 'person',
            is_active TINYINT(1) NOT NULL DEFAULT 1,
            sort_order INT NOT NULL DEFAULT 100,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    roles = [
        ("owner", "业主", "person"),
        ("landlord", "房东", "person"),
        ("sub_landlord", "二房东", "person"),
        ("agent", "中介经办人", "person"),
        ("platform_contact", "租房平台联系人", "person"),
        ("manager", "实际管理人", "person"),
        ("property_manager", "物业管理人", "organization"),
    ]
    for code, name, subject_type in roles:
        await cur.execute(
            "INSERT IGNORE INTO registry_role_types "
            "(code, name, subject_type) VALUES (%s, %s, %s)",
            (code, name, subject_type),
        )
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS registry_property_person_roles (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            property_id BIGINT NOT NULL,
            person_id BIGINT NOT NULL,
            role_type_id BIGINT NOT NULL,
            valid_from DATETIME DEFAULT NULL,
            valid_to DATETIME DEFAULT NULL,
            verified TINYINT(1) NOT NULL DEFAULT 0,
            source_type VARCHAR(30) NOT NULL DEFAULT 'manual',
            source_ref VARCHAR(190) NOT NULL DEFAULT '',
            created_by BIGINT DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_registry_property_person (property_id, role_type_id, valid_to),
            INDEX idx_registry_person_property (person_id, role_type_id, valid_to)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS registry_property_organization_roles (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            property_id BIGINT NOT NULL,
            organization_id BIGINT NOT NULL,
            role_type_id BIGINT NOT NULL,
            valid_from DATETIME DEFAULT NULL,
            valid_to DATETIME DEFAULT NULL,
            verified TINYINT(1) NOT NULL DEFAULT 0,
            source_type VARCHAR(30) NOT NULL DEFAULT 'manual',
            source_ref VARCHAR(190) NOT NULL DEFAULT '',
            created_by BIGINT DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_registry_property_org (property_id, role_type_id, valid_to),
            INDEX idx_registry_org_property (organization_id, role_type_id, valid_to)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS registry_source_batches (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            source_type VARCHAR(30) NOT NULL,
            file_name VARCHAR(255) NOT NULL DEFAULT '',
            file_sha256 CHAR(64) NOT NULL DEFAULT '',
            status VARCHAR(20) NOT NULL DEFAULT 'preview',
            imported_count INT UNSIGNED NOT NULL DEFAULT 0,
            candidate_count INT UNSIGNED NOT NULL DEFAULT 0,
            conflict_count INT UNSIGNED NOT NULL DEFAULT 0,
            created_by BIGINT DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uk_registry_source_hash (source_type, file_sha256),
            INDEX idx_registry_source_status (status, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS registry_source_records (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            batch_id BIGINT NOT NULL,
            source_ref VARCHAR(190) NOT NULL DEFAULT '',
            entity_type VARCHAR(30) NOT NULL,
            entity_id BIGINT DEFAULT NULL,
            payload_json JSON NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_registry_source_record_batch (batch_id, id),
            INDEX idx_registry_source_record_entity (entity_type, entity_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS registry_change_candidates (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            batch_id BIGINT DEFAULT NULL,
            entity_type VARCHAR(30) NOT NULL,
            entity_id BIGINT DEFAULT NULL,
            change_type VARCHAR(30) NOT NULL,
            payload_json JSON NOT NULL,
            reason VARCHAR(500) NOT NULL DEFAULT '',
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            reviewed_by BIGINT DEFAULT NULL,
            reviewed_at DATETIME DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_registry_candidate_status (status, entity_type, created_at),
            INDEX idx_registry_candidate_entity (entity_type, entity_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS registry_conflicts (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            batch_id BIGINT DEFAULT NULL,
            entity_type VARCHAR(30) NOT NULL,
            entity_key VARCHAR(190) NOT NULL,
            conflict_type VARCHAR(50) NOT NULL,
            details_json JSON NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            resolved_by BIGINT DEFAULT NULL,
            resolved_at DATETIME DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_registry_conflict_status (status, created_at),
            INDEX idx_registry_conflict_key (entity_type, entity_key)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS registry_merge_history (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            source_person_id BIGINT NOT NULL,
            target_person_id BIGINT NOT NULL,
            action VARCHAR(20) NOT NULL,
            relation_snapshot JSON NOT NULL,
            reason VARCHAR(500) NOT NULL DEFAULT '',
            changed_by BIGINT DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_registry_merge_source (source_person_id, created_at),
            INDEX idx_registry_merge_target (target_person_id, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS watch_people (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            identity_number VARCHAR(50) DEFAULT NULL,
            identity_hmac CHAR(64) DEFAULT NULL,
            identity_hmac_version SMALLINT UNSIGNED DEFAULT NULL,
            is_temporary TINYINT(1) NOT NULL DEFAULT 0,
            verification_status VARCHAR(20) NOT NULL DEFAULT 'unverified',
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            source_type VARCHAR(30) NOT NULL DEFAULT 'manual',
            source_ref VARCHAR(190) NOT NULL DEFAULT '',
            created_by BIGINT DEFAULT NULL,
            updated_by BIGINT DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_watch_identity (identity_hmac),
            INDEX idx_watch_name (name),
            INDEX idx_watch_status (status, is_temporary)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS watch_person_phones (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            person_id BIGINT NOT NULL,
            phone VARCHAR(200) NOT NULL,
            phone_hmac CHAR(64) NOT NULL,
            hmac_version SMALLINT UNSIGNED NOT NULL DEFAULT 1,
            is_primary TINYINT(1) NOT NULL DEFAULT 0,
            valid_from DATETIME DEFAULT NULL,
            valid_to DATETIME DEFAULT NULL,
            source_type VARCHAR(30) NOT NULL DEFAULT 'manual',
            source_ref VARCHAR(190) NOT NULL DEFAULT '',
            created_by BIGINT DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_watch_phone_hmac (phone_hmac),
            INDEX idx_watch_person_phone (person_id, valid_to, is_primary)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS watch_categories (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            code VARCHAR(60) NOT NULL UNIQUE,
            name VARCHAR(100) NOT NULL,
            parent_id BIGINT DEFAULT NULL,
            color VARCHAR(20) NOT NULL DEFAULT '#1677ff',
            alert_level VARCHAR(20) NOT NULL DEFAULT 'normal',
            description VARCHAR(1000) NOT NULL DEFAULT '',
            is_active TINYINT(1) NOT NULL DEFAULT 1,
            sort_order INT NOT NULL DEFAULT 100,
            created_by BIGINT DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_watch_category_parent (parent_id, is_active)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    for code, name in [("重点人员", "重点人员"), ("五失人员", "五失人员"), ("通勤人员", "通勤人员")]:
        await cur.execute(
            "INSERT IGNORE INTO watch_categories (code, name) VALUES (%s, %s)",
            (code, name),
        )
    await _ensure_column(
        cur,
        "watch_categories",
        "description",
        "VARCHAR(1000) NOT NULL DEFAULT '' AFTER alert_level",
    )
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS watch_assignments (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            person_id BIGINT NOT NULL,
            category_id BIGINT NOT NULL,
            valid_from DATETIME NOT NULL,
            valid_to DATETIME DEFAULT NULL,
            released_at DATETIME DEFAULT NULL,
            source_type VARCHAR(30) NOT NULL DEFAULT 'manual',
            source_ref VARCHAR(190) NOT NULL DEFAULT '',
            basis VARCHAR(1000) NOT NULL DEFAULT '',
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            created_by BIGINT DEFAULT NULL,
            updated_by BIGINT DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_watch_assignment_person (person_id, status, valid_from, valid_to),
            INDEX idx_watch_assignment_category (category_id, status, valid_from, valid_to)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS watch_assignment_versions (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            assignment_id BIGINT NOT NULL,
            version_no INT UNSIGNED NOT NULL,
            snapshot_json JSON NOT NULL,
            changed_by BIGINT DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uk_watch_assignment_version (assignment_id, version_no),
            INDEX idx_watch_assignment_version_time (assignment_id, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS online_task_watch_snapshots (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            parser_type VARCHAR(50) NOT NULL,
            row_key VARCHAR(200) NOT NULL,
            identity_hmac CHAR(64) NOT NULL,
            first_dispatch_at DATETIME NOT NULL,
            assignment_id BIGINT NOT NULL,
            assignment_version INT UNSIGNED NOT NULL,
            snapshot_status VARCHAR(20) NOT NULL DEFAULT 'active',
            snapshot_reason VARCHAR(30) NOT NULL DEFAULT 'initial_match',
            captured_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uk_watch_task_assignment (parser_type, row_key, assignment_id),
            INDEX idx_watch_task (parser_type, row_key),
            INDEX idx_watch_task_identity_date (identity_hmac, first_dispatch_at),
            INDEX idx_watch_task_date (first_dispatch_at, snapshot_status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)


async def ensure_workflow_schema(cur) -> None:
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS workflow_types (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            code VARCHAR(60) NOT NULL UNIQUE,
            name VARCHAR(100) NOT NULL,
            description VARCHAR(1000) NOT NULL DEFAULT '',
            form_schema JSON NOT NULL,
            default_due_hours INT UNSIGNED DEFAULT NULL,
            enabled TINYINT(1) NOT NULL DEFAULT 0,
            created_by BIGINT DEFAULT NULL,
            updated_by BIGINT DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_workflow_type_enabled (enabled, code)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS workflow_type_versions (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            workflow_type_id BIGINT NOT NULL,
            version_no INT UNSIGNED NOT NULL,
            form_schema JSON NOT NULL,
            approval_mode VARCHAR(20) NOT NULL DEFAULT 'sequential',
            status VARCHAR(20) NOT NULL DEFAULT 'draft',
            published_by BIGINT DEFAULT NULL,
            published_at DATETIME DEFAULT NULL,
            created_by BIGINT DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uk_workflow_type_version (workflow_type_id, version_no),
            INDEX idx_workflow_version_status (workflow_type_id, status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS workflow_steps (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            workflow_version_id BIGINT NOT NULL,
            step_order INT UNSIGNED NOT NULL,
            step_group INT UNSIGNED NOT NULL DEFAULT 1,
            name VARCHAR(100) NOT NULL,
            step_type VARCHAR(30) NOT NULL DEFAULT 'approval',
            approval_mode VARCHAR(20) NOT NULL DEFAULT 'sequential',
            default_due_hours INT UNSIGNED DEFAULT NULL,
            config_json JSON NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uk_workflow_step_order (workflow_version_id, step_order),
            INDEX idx_workflow_step_group (workflow_version_id, step_group)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS workflow_step_assignee_rules (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            step_id BIGINT NOT NULL,
            assignee_type VARCHAR(30) NOT NULL,
            assignee_value VARCHAR(190) NOT NULL,
            priority INT NOT NULL DEFAULT 100,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_workflow_assignee_rule (step_id, assignee_type, priority)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS work_orders (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            ticket_no VARCHAR(40) NOT NULL UNIQUE,
            type_code VARCHAR(60) NOT NULL,
            workflow_version_id BIGINT NOT NULL,
            title VARCHAR(200) NOT NULL,
            description TEXT NOT NULL,
            requester_user_id BIGINT NOT NULL,
            current_assignee_user_id BIGINT DEFAULT NULL,
            current_queue VARCHAR(100) NOT NULL DEFAULT '',
            status VARCHAR(30) NOT NULL DEFAULT 'draft',
            priority VARCHAR(20) NOT NULL DEFAULT 'normal',
            due_at DATETIME DEFAULT NULL,
            version_no INT UNSIGNED NOT NULL DEFAULT 1,
            submitted_at DATETIME DEFAULT NULL,
            completed_at DATETIME DEFAULT NULL,
            cancelled_at DATETIME DEFAULT NULL,
            cancel_reason VARCHAR(500) NOT NULL DEFAULT '',
            form_data JSON NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_work_order_queue (current_queue, status, due_at),
            INDEX idx_work_order_requester (requester_user_id, created_at),
            INDEX idx_work_order_assignee (current_assignee_user_id, status, updated_at),
            INDEX idx_work_order_type_status (type_code, status, updated_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS work_order_links (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            work_order_id BIGINT NOT NULL,
            object_type VARCHAR(40) NOT NULL,
            object_id VARCHAR(190) NOT NULL,
            object_ref VARCHAR(190) NOT NULL DEFAULT '',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uk_work_order_link (work_order_id, object_type, object_id),
            INDEX idx_work_order_object (object_type, object_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS work_order_steps (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            work_order_id BIGINT NOT NULL,
            workflow_step_id BIGINT NOT NULL,
            step_order INT UNSIGNED NOT NULL,
            status VARCHAR(30) NOT NULL DEFAULT 'pending',
            assignee_user_id BIGINT DEFAULT NULL,
            queue VARCHAR(100) NOT NULL DEFAULT '',
            due_at DATETIME DEFAULT NULL,
            decision VARCHAR(30) NOT NULL DEFAULT '',
            decision_note VARCHAR(2000) NOT NULL DEFAULT '',
            decided_by BIGINT DEFAULT NULL,
            decided_at DATETIME DEFAULT NULL,
            version_no INT UNSIGNED NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_work_order_step (work_order_id, workflow_step_id),
            INDEX idx_work_order_step_queue (queue, status, due_at),
            INDEX idx_work_order_step_assignee (assignee_user_id, status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS work_order_claims (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            work_order_id BIGINT NOT NULL,
            step_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            claimed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            released_at DATETIME DEFAULT NULL,
            release_reason VARCHAR(500) NOT NULL DEFAULT '',
            INDEX idx_work_order_claim_active (step_id, released_at),
            INDEX idx_work_order_claim_user (user_id, claimed_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS work_order_events (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            work_order_id BIGINT NOT NULL,
            step_id BIGINT DEFAULT NULL,
            event_type VARCHAR(40) NOT NULL,
            actor_user_id BIGINT DEFAULT NULL,
            from_status VARCHAR(30) NOT NULL DEFAULT '',
            to_status VARCHAR(30) NOT NULL DEFAULT '',
            detail_json JSON NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_work_order_event (work_order_id, created_at),
            INDEX idx_work_order_event_actor (actor_user_id, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS work_order_comments (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            work_order_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            content TEXT NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_work_order_comment (work_order_id, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS work_order_attachments (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            work_order_id BIGINT NOT NULL,
            file_id CHAR(36) NOT NULL UNIQUE,
            original_name VARCHAR(255) NOT NULL DEFAULT '',
            mime_type VARCHAR(100) NOT NULL DEFAULT 'application/octet-stream',
            storage_key VARCHAR(500) NOT NULL,
            sha256 CHAR(64) NOT NULL,
            size_bytes BIGINT UNSIGNED NOT NULL DEFAULT 0,
            classification VARCHAR(20) NOT NULL DEFAULT 'sensitive',
            uploaded_by BIGINT NOT NULL,
            retention_until DATETIME DEFAULT NULL,
            deleted_at DATETIME DEFAULT NULL,
            deleted_by BIGINT DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_work_order_attachment (work_order_id, deleted_at),
            INDEX idx_work_order_attachment_retention (retention_until, deleted_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS work_order_reminders (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            work_order_id BIGINT NOT NULL,
            reminder_type VARCHAR(30) NOT NULL,
            reminder_date DATE DEFAULT NULL,
            recipient_user_id BIGINT NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uk_work_order_reminder (
                work_order_id, reminder_type, reminder_date, recipient_user_id
            ),
            INDEX idx_work_order_reminder_time (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await _ensure_column(
        cur,
        "work_order_attachments",
        "mime_type",
        "VARCHAR(100) NOT NULL DEFAULT 'application/octet-stream' AFTER original_name",
    )
    await _ensure_column(
        cur,
        "work_order_attachments",
        "deleted_by",
        "BIGINT DEFAULT NULL AFTER deleted_at",
    )
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS leave_request_details (
            work_order_id BIGINT PRIMARY KEY,
            member_id BIGINT NOT NULL,
            leave_type VARCHAR(50) NOT NULL,
            start_date DATE NOT NULL,
            end_date DATE NOT NULL,
            reason VARCHAR(1000) NOT NULL DEFAULT '',
            affects_weekend_duty TINYINT(1) NOT NULL DEFAULT 0,
            attendance_record_id BIGINT DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_leave_member_dates (member_id, start_date, end_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS photo_request_details (
            work_order_id BIGINT PRIMARY KEY,
            subject_type VARCHAR(40) NOT NULL,
            subject_id VARCHAR(190) NOT NULL,
            requested_from DATETIME DEFAULT NULL,
            requested_to DATETIME DEFAULT NULL,
            request_reason VARCHAR(1000) NOT NULL DEFAULT '',
            result_status VARCHAR(30) NOT NULL DEFAULT 'pending',
            result_note VARCHAR(2000) NOT NULL DEFAULT '',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_photo_subject (subject_type, subject_id),
            INDEX idx_photo_result (result_status, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    photo_form_schema = (
        '{"fields":['
        '{"name":"subject_type","label":"对象类型","type":"select","required":true,"options":["task","person","other"]},'
        '{"name":"subject_id","label":"对象编号","type":"text","required":false},'
        '{"name":"request_reason","label":"申请理由","type":"textarea","required":true},'
        '{"name":"requested_from","label":"开始时间","type":"datetime","required":false},'
        '{"name":"requested_to","label":"结束时间","type":"datetime","required":false}'
        ']}'
    )
    leave_form_schema = (
        '{"fields":['
        '{"name":"leave_type","label":"请假类型","type":"select","required":true,"options":["temporary_leave","sick_leave","annual_leave"]},'
        '{"name":"start_date","label":"开始日期","type":"date","required":true},'
        '{"name":"end_date","label":"结束日期","type":"date","required":true},'
        '{"name":"reason","label":"原因","type":"textarea","required":false},'
        '{"name":"affects_weekend_duty","label":"影响双休日备勤","type":"boolean","required":false}'
        ']}'
    )
    # Photo requests are usable out of the box. Leave requests intentionally
    # remain disabled until a super administrator publishes the local process.
    await cur.execute(
        "INSERT IGNORE INTO workflow_types "
        "(code, name, description, form_schema, default_due_hours, enabled) "
        "VALUES (%s,%s,%s,%s,%s,1)",
        (
            "photo_request",
            "照片调取申请",
            "业务人员向基础管控申请调取照片",
            photo_form_schema,
            24,
        ),
    )
    await cur.execute(
        "INSERT IGNORE INTO workflow_types "
        "(code, name, description, form_schema, default_due_hours, enabled) "
        "VALUES (%s,%s,%s,%s,%s,0)",
        (
            "leave_request",
            "请假申请",
            "网格员提交请假并按配置流程审批",
            leave_form_schema,
            24,
        ),
    )
    await cur.execute(
        "INSERT IGNORE INTO workflow_type_versions "
        "(workflow_type_id, version_no, form_schema, status, published_at) "
        "SELECT id, 1, form_schema, 'published', UTC_TIMESTAMP() "
        "FROM workflow_types WHERE code=%s",
        ("photo_request",),
    )
    await cur.execute(
        "INSERT IGNORE INTO workflow_steps "
        "(workflow_version_id, step_order, step_group, name, step_type, config_json) "
        "SELECT version.id, 1, 1, '基础管控处理', 'handling', %s "
        "FROM workflow_type_versions version "
        "JOIN workflow_types type ON type.id=version.workflow_type_id "
        "WHERE type.code=%s AND version.version_no=1",
        ('{"queue":"基础管控","claim_required":true}', "photo_request"),
    )
