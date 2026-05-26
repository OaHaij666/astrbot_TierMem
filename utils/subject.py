def extract_subject_id(event, memory_mode: str) -> str:
    uid = event.unified_msg_origin
    parts = uid.split(":")
    user_id = parts[-1] if parts else "unknown"
    msg_type = parts[-2] if len(parts) >= 2 else "PrivateMessage"

    if memory_mode == "shared":
        return f"{user_id}#shared"

    if msg_type == "GroupMessage":
        group_id = parts[-1] if parts else "unknown"
        sender_id = event.get_sender_id() or user_id
        return f"{sender_id}#{group_id}"
    else:
        return f"{user_id}#private"


def build_cross_subject_id(user_id: str, current_subject_id: str, memory_mode: str) -> str:
    if memory_mode == "shared":
        return f"{user_id}#shared"
    parts = current_subject_id.split("#")
    group_id = parts[1] if len(parts) > 1 and parts[1] not in ("shared", "private") else "unknown"
    return f"{user_id}#{group_id}"


def detect_scene(event) -> str:
    uid = event.unified_msg_origin
    parts = uid.split(":")
    msg_type = parts[-2] if len(parts) >= 2 else "PrivateMessage"
    return "group" if msg_type == "GroupMessage" else "private"
