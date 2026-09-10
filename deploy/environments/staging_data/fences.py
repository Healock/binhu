"""Preserve optimistic-lock equality without exporting production hashes."""
from .codec import SnapshotError, integer


def transform_fence(row, original_source, sanitized_source, codec):
    revision = row["source_revision"]
    if revision is not None:
        revision = integer(revision)
    original_hash = row["source_row_hash"]
    if not isinstance(original_hash, str):
        raise SnapshotError("invalid_source_fence_hash")
    # Empty hashes are meaningful in legacy records; do not fill them in.
    if not original_hash:
        safe_hash = ""
    elif original_hash == original_source["row_hash"]:
        safe_hash = sanitized_source["row_hash"]
    else:
        safe_hash = codec.digest("stale_source_hash", original_hash)
        if safe_hash == sanitized_source["row_hash"]:
            raise SnapshotError("source_fence_collision")
    return {"source_revision": revision, "source_row_hash": safe_hash}
