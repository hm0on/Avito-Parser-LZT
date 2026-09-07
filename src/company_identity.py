from __future__ import annotations

import uuid

from src.database.models import CompanyRaw

MERGE_IDENTITY_PREFIX = "merge-group:"


def build_identity_key(raw: CompanyRaw) -> str:
    source = (raw.source or "unknown").strip().lower()
    if raw.source_id:
        return f"{source}:id:{raw.source_id.strip()}"
    if raw.source_link:
        return f"{source}:url:{raw.source_link.strip()}"
    return f"{source}:raw:{raw.id}"


def build_merge_identity_key(group_id: uuid.UUID | str) -> str:
    return f"{MERGE_IDENTITY_PREFIX}{group_id}"


def clean_uuid_for_identity(identity_key: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"clean:{identity_key}")
