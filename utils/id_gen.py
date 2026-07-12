import uuid
from datetime import datetime, timezone


def generate_memory_id() -> str:
    return f"mem-{int(datetime.now(timezone.utc).timestamp())}-{uuid.uuid4().hex[:6]}"


def generate_turn_id() -> str:
    return f"turn-{int(datetime.now(timezone.utc).timestamp())}-{uuid.uuid4().hex[:6]}"


def generate_relation_id() -> str:
    return f"rel-{int(datetime.now(timezone.utc).timestamp())}-{uuid.uuid4().hex[:8]}"


def generate_evidence_id() -> str:
    return f"ev-{int(datetime.now(timezone.utc).timestamp())}-{uuid.uuid4().hex[:8]}"
