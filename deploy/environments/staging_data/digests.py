"""Environment-specific digest reconstruction with preserved match semantics."""
import hashlib
import hmac
import json
import re


def identity_digest(values, fields, key):
    value = next((re.sub(r'\s+', '', str(values.get(field) or '')).upper()
                  for field in fields if re.sub(r'\s+', '', str(values.get(field) or ''))), '')
    return hmac.new(key.encode(), ('registry:identity:v1:'+value).encode(), hashlib.sha256).hexdigest() if value else ''


def address_digest(value, key):
    from services.registry_import import normalize_address
    address = normalize_address(value)
    return hmac.new(key.encode(), ('registration-address:'+address).encode(), hashlib.sha256).hexdigest() if address else ''


def annotation_digest(values, workflow, community, key):
    from services.address_matching import normalize_address_text
    fields = workflow.address_fields
    original = next((str(values.get(field) or '').strip() for field in fields
        if field != '现住址' and str(values.get(field) or '').strip()), '')
    current = str(values.get('现住址') or '').strip() if '现住址' in fields else ''
    payload = json.dumps(['address-annotation-v2', *[normalize_address_text(value)
        for value in (original,current,community)]],ensure_ascii=False,separators=(',',':'))
    return hmac.new(key.encode(),payload.encode(),hashlib.sha256).hexdigest()


def matching_state(original, current):
    return 'empty' if not original else ('current' if original == current else 'stale')
