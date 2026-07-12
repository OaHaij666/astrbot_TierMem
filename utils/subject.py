def extract_user_id(event) -> str:
    sender_id = event.get_sender_id() if hasattr(event, "get_sender_id") else ""
    if sender_id:
        return str(sender_id)
    origin = getattr(event, "unified_msg_origin", "") or ""
    parts = origin.split(":")
    return parts[-1] if parts else "unknown"


def detect_scene(event) -> str:
    origin = getattr(event, "unified_msg_origin", "") or ""
    parts = origin.split(":")
    msg_type = parts[-2] if len(parts) >= 2 else "PrivateMessage"
    return "group" if msg_type == "GroupMessage" else "private"


def extract_context_id(event) -> str:
    if detect_scene(event) == "group":
        return f"group:{extract_group_id(event) or 'unknown'}"
    return f"private:{extract_user_id(event)}"


def extract_group_id(event):
    if detect_scene(event) != "group":
        return None
    if hasattr(event, "get_group_id"):
        try:
            group_id = event.get_group_id()
            if group_id:
                return str(group_id)
        except Exception:
            pass
    origin = getattr(event, "unified_msg_origin", "") or ""
    parts = origin.split(":")
    return parts[-1] if parts else None


# Backward-compatible name for external callers. The new model is user-centric.
def extract_subject_id(event, memory_mode: str = "user") -> str:
    return extract_user_id(event)


def build_cross_subject_id(
    user_id: str, current_subject_id: str = "", memory_mode: str = "user"
) -> str:
    return user_id
