"""Address confirmations and history summaries for current snapshot tasks."""
from .codec import SnapshotError, enum, integer, date_value
from .fences import transform_fence

ADDRESS_FIELDS = ('parser_type','row_key','original_address','suggested_entry_id',
    'suggested_community_id','match_status','confirmed_entry_id','confirmed_by','confirmed_at',
    'manual_unmatched_reason','manual_unmatched_address_hmac','manual_unmatched_by','manual_unmatched_at')
REVIEW_EVENT_FIELDS = ('id','flow_id','stage','action','outcome','actor_user_id','automatic',
    'source_revision','source_row_hash','created_at')
REGISTRATION_EVENT_FIELDS = ('id','parser_type','row_key','source_id','property_id','event_type','actor_user_id','created_at')
MATCH_STATES = {'suggested','confirmed','ambiguous','unmatched','conflict','invalid','review_required','manual_unmatched'}
FLOW_STATES = {'initial_pending','initial_extension','deep_pending','deep_extension',
    'final_unverifiable','resolved','archived','source_exception'}
REVIEW_STATES = FLOW_STATES | {'source_removed'}
REVIEW_ACTIONS = {'legacy_unverifiable_backfill','formal_result_submitted','entered_unverifiable',
    'feedback_recorded','feedback_cleared','automatic_transition_resumed','automatic_transition_paused',
    'review_decision','archive_exported','formal_result_detected','overdue_auto_transition',
    'administrative_bulk_archive','maintenance_archived'}
REGISTRATION_ACTIONS = {'property_selected','registration_cancelled','residence_match','residence_mismatch',
    'registration_confirmation_enqueue_failed','registration_confirmed','registration_writeback_failed',
    'manual_confirmation','manual_registration_confirmed','pending_address_saved','property_linked'}
UNMATCHED_REASONS = {'insufficient_address','outside_existing_communities','community_registry_missing',
    'outside_task_community','other_review_required'}


def address_rows(rows, remapped, source_communities, codec):
    output=[]
    metadata=[]
    for row in rows:
        key=(row['parser_type'],row['row_key'])
        entry=remapped[key]
        community_id=source_communities[key]
        community=codec.reference('community',row['suggested_community_id'])
        safe={'parser_type':row['parser_type'],'row_key':entry['source']['row_key'],
            'original_address':codec.address(community_id,row['original_address']),
            'suggested_entry_id':codec.reference('small_community',row['suggested_entry_id']),
            'suggested_community_id':community,
            'suggested_community_name':'验证社区'+str(community) if community else '',
            'match_status':enum(row['match_status'],MATCH_STATES,empty=False),
            'confirmed_entry_id':codec.reference('small_community',row['confirmed_entry_id']),
            'confirmed_by':codec.reference('actor',row['confirmed_by']),
            'confirmed_at':date_value(row['confirmed_at']),
            'manual_unmatched_reason':enum(row['manual_unmatched_reason'],UNMATCHED_REASONS) or None,
            'manual_unmatched_by':codec.reference('actor',row['manual_unmatched_by']),
            'manual_unmatched_at':date_value(row['manual_unmatched_at']),
            'manual_unmatched_address_hmac':None,
            'match_score':0,'match_method':'staging_snapshot','match_reason':'脱敏副本，候选证据重新生成',
            'candidates_json':'[]','matcher_version':'staging_snapshot'}
        output.append(safe)
        metadata.append({'parser_type':row['parser_type'],'row_key':safe['row_key'],
                         'has_annotation_hash':bool(row['manual_unmatched_address_hmac'])})
    return output, metadata


def history_rows(review, registration, flows, current, remapped, codec):
    output={'OnlineData._unverifiable_review_events':[], 'OnlineData._task_registration_events':[]}
    codec.allocate('review_event',[row['id'] for row in review])
    codec.allocate('registration_event',[row['id'] for row in registration])
    for row in review:
        flow=flows[row['flow_id']]
        key=(flow['parser_type'],flow['row_key'])
        action=enum(row['action'],REVIEW_ACTIONS,empty=False)
        # Export job IDs in archive outcomes are replaced with a safe summary.
        outcome='archived' if action=='archive_exported' else enum(row['outcome'], REVIEW_STATES | {'success','failure'})
        fence=transform_fence(row,current[key],remapped[key]['source'],codec)
        output['OnlineData._unverifiable_review_events'].append({
            'id':codec.reference('review_event',row['id']), 'flow_id':codec.reference('flow',row['flow_id']),
            'stage':enum(row['stage'],REVIEW_STATES),'action':action,'outcome':outcome,
            'actor_user_id':codec.reference('actor',row['actor_user_id']),
            'automatic':integer(row['automatic'],maximum=1),**fence,
            'protected_text':None,'safe_reason_code':'staging_snapshot_summary','created_at':date_value(row['created_at'])})
    for row in registration:
        key=(row['parser_type'],row['row_key'])
        output['OnlineData._task_registration_events'].append({
            'id':codec.reference('registration_event',row['id']), 'parser_type':row['parser_type'],
            'row_key':remapped[key]['source']['row_key'],
            'source_id':codec.reference('source',row['source_id']),
            'property_id':codec.reference('property',row['property_id']),
            'event_type':enum(row['event_type'],REGISTRATION_ACTIONS,empty=False),
            'actor_user_id':codec.reference('actor',row['actor_user_id']),
            'reason_code':'staging_snapshot_summary','created_at':date_value(row['created_at'])})
    return output
