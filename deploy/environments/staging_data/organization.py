"""Organization relations with generated, initially unusable account identities."""
from .codec import SnapshotError, enum, integer, normalized

FIELDS = {
    "PlatformData._departments": ("id", "name", "department_type", "community_id", "is_active"),
    "PlatformData._grid_members": ("id", "name", "community", "department_id", "status", "position"),
    "PlatformData._grid_member_department_links": ("member_id", "department_id", "sort_order"),
    # Never SELECT a password hash, session, avatar, permission group or token.
    "PlatformData._users": ("id", "member_id", "display_name"),
}


def transform(rows, codec, communities):
    from services.personnel_positions import POSITION_OPTIONS
    if set(rows) != set(FIELDS):
        raise SnapshotError("organization_table_scope_mismatch")
    for table, items in rows.items():
        if any(set(row) != set(FIELDS[table]) for row in items):
            raise SnapshotError("organization_column_scope_mismatch")
        keys = [(row['member_id'], row['department_id']) if table.endswith('_links') else row['id'] for row in items]
        if len(keys) != len(set(keys)):
            raise SnapshotError("duplicate_organization_key")
    codec.allocate('department', [r['id'] for r in rows['PlatformData._departments']])
    codec.allocate('member', [r['id'] for r in rows['PlatformData._grid_members']])
    output = {table: [] for table in rows}
    for row in rows['PlatformData._departments']:
        community = codec.reference('community', row['community_id'])
        codec.remember(row['name'])
        output['PlatformData._departments'].append({
            'id': codec.reference('department', row['id']),
            'name': '验证社区' + str(community) if community else codec.text('department', row['name'], '验证部门'),
            'community_id': community, 'department_type': enum(row['department_type'], {'community', 'internal'}, empty=False),
            'is_active': integer(row['is_active'], maximum=1)})
    members = {}
    for row in rows['PlatformData._grid_members']:
        community_name = normalized(row['community'])
        if community_name and community_name not in communities:
            raise SnapshotError('member_community_unresolved')
        codec.remember(row['community'])
        safe = {'id': codec.reference('member', row['id']), 'name': codec.text('staff', row['name'], '验证核查员'),
            'community': '验证社区' + str(codec.reference('community', communities[community_name])) if community_name else '',
            'department_id': codec.reference('department', row['department_id']),
            'status': enum(row['status'], {'在岗', '离岗'}, empty=False),
            'position': enum(row['position'], set(POSITION_OPTIONS), empty=False),
            'phone': '', 'notes': '', 'id_card_number': None}
        output['PlatformData._grid_members'].append(safe)
        members[row['id']] = safe
    for row in rows['PlatformData._grid_member_department_links']:
        output['PlatformData._grid_member_department_links'].append({
            'member_id': codec.reference('member', row['member_id'], nullable=False),
            'department_id': codec.reference('department', row['department_id'], nullable=False),
            'sort_order': integer(row['sort_order'])})
    linked = set()
    for row in rows['PlatformData._users']:
        actor = codec.reference('actor', row['id'], nullable=False)
        member = codec.reference('member', row['member_id'])
        if member is not None:
            if member in linked:
                raise SnapshotError('duplicate_member_account')
            linked.add(member)
        codec.remember(row['display_name'])
        output['PlatformData._users'].append({'id': actor,
            'username': f'snapshot-{actor}@staging',
            'display_name': members[row['member_id']]['name'] if member is not None else f'验证历史操作者{actor}',
            'member_id': member, 'role': 'member', 'password_is_temporary': 1,
            'permission_group_id': None, 'group_assignment_mode': 'inherited'})
    if linked != {row['id'] for row in members.values()}:
        raise SnapshotError('member_account_reference_missing')
    if codec.scan(output):
        raise SnapshotError('source_sensitive_value_detected')
    return output
