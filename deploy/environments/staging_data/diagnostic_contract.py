"""Reviewed diagnostic identifiers; never infer safe labels from source values.

This module is importable on the host without the application's dependencies.
Parser columns are frozen below and checked against the application in tests.
"""
from .registry import FIELDS as REGISTRY_FIELDS
from .organization import FIELDS as ORGANIZATION_FIELDS
from .relations import ADDRESS_FIELDS, REVIEW_EVENT_FIELDS, REGISTRATION_EVENT_FIELDS
from .build import FLOW_FIELDS, REGISTRATION_FIELDS


def diagnostic_fields():
    fields = {table: set(columns) for table, columns in
              {**REGISTRY_FIELDS, **ORGANIZATION_FIELDS, **TASK_COLUMNS}.items()}
    additions = {
        'PlatformData._communities': 'police_officers qmf_community_code',
        'RegistryData._police_address_entries': 'normalized_name aliases_json source_flags pattern',
        'RegistryData.registry_properties': 'community_name_snapshot normalized_address source_type source_ref',
        'RegistryData.registry_property_small_community_links': 'small_community_name community_name_snapshot match_score match_method match_reason matcher_version match_evidence',
        'PlatformData._grid_members': 'phone notes id_card_number',
        'PlatformData._users': 'username role password_is_temporary permission_group_id group_assignment_mode',
        'OnlineData._online_source_rows': 'id spreadsheet_id parser_type sheet_id physical_row row_key row_hash values_json cell_meta_json revision source_kind source_ref archived_at',
        'OnlineData._local_source_records': 'parser_type local_task_id business_key source_kind source_ref values_json content_hash revision status',
        'OnlineData._unverifiable_review_flows': 'safe_reason_code',
        'OnlineData._task_registration_links': 'last_scan_token reason_code manual_reason manual_note',
        'OnlineData._online_task_address_matches': 'suggested_community_name match_score match_method match_reason candidates_json matcher_version',
        'OnlineData._unverifiable_review_events': 'protected_text safe_reason_code',
        'OnlineData._task_registration_events': 'reason_code',
    }
    for table, columns in {
        'OnlineData._unverifiable_review_flows': FLOW_FIELDS,
        'OnlineData._task_registration_links': REGISTRATION_FIELDS,
        'OnlineData._online_task_address_matches': ADDRESS_FIELDS,
        'OnlineData._unverifiable_review_events': REVIEW_EVENT_FIELDS,
        'OnlineData._task_registration_events': REGISTRATION_EVENT_FIELDS,
    }.items():
        fields[table] = set(columns)
    for table, columns in additions.items():
        fields.setdefault(table, set()).update(columns.split())
    for table in TASK_COLUMNS:
        fields[table].update(('id', '_row_key'))
    return fields


TASK_COLUMNS = {'OnlineData.t_fullchain': ('下发日期',
                            '截止日期',
                            '核查人',
                            '社区',
                            '来源',
                            '姓名',
                            '身份证号',
                            '电话号码',
                            '地址',
                            '登记情况',
                            '创建时间',
                            '现住址',
                            '核查结果',
                            '研判',
                            '二次反馈'),
 'OnlineData.t_rental_check': ('下发时间',
                               '截止时间',
                               '核查人',
                               '社区',
                               '姓名',
                               '身份证号',
                               '手机号码',
                               '房屋地址',
                               '现住址',
                               '核查结果',
                               '入住方式',
                               '研判',
                               '二次反馈'),
 'OnlineData.t_suspect_unrevoked': ('截止时间', '核查人', '姓名', '身份证号', '联系方式', '地址', '下发社区', '核查结果', '备注'),
 'OnlineData.t_suspect_return': ('下发日期',
                                 '截止日期',
                                 '核查人',
                                 '社区',
                                 '姓名',
                                 '身份证号码',
                                 '联系号码',
                                 '高频抓拍小区',
                                 '现住址',
                                 '核查反馈',
                                 '研判',
                                 '二次核查结果'),
 'OnlineData.t_delivery_industry': ('下发时间',
                                    '截止时间',
                                    '核查人',
                                    '姓名',
                                    '身份证号',
                                    '地址1',
                                    '手机号码',
                                    '社区',
                                    '参考姓名',
                                    '参考身份证号码',
                                    '现住址',
                                    '核查结果',
                                    '研判',
                                    '二次反馈'),
 'OnlineData.t_suzhou_police': ('下发日期',
                                '截止日期',
                                '核查人',
                                '社区',
                                '姓名',
                                '身份证号',
                                '联系号码',
                                '疑似现住址',
                                '接警编号',
                                '出警日期',
                                '出警类别',
                                '出警内容',
                                '出警单位',
                                '参考派出所',
                                '现住址',
                                '核查结果',
                                '研判',
                                '二次反馈',
                                '备注'),
 'OnlineData.t_traffic_police': ('下发日期',
                                 '截止日期',
                                 '核查人',
                                 '社区',
                                 '姓名',
                                 '身份证号',
                                 '联系号码',
                                 '地址1',
                                 '现住址',
                                 '核查结果',
                                 '研判',
                                 '二次反馈',
                                 '备注')}
