import json
from typing import Optional, Dict, Any


def extract_json_block(text: str) -> Optional[str]:
    """从混合文本中提取 JSON 块"""
    stripped = text.strip()
    if not stripped:
        return None

    # 代码块
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[-1].startswith("```"):
            return "\n".join(lines[1:-1]).strip()
        if stripped.startswith("```json"):
            return "\n".join(lines[1:-1]).strip()
        return None

    # 纯 JSON
    if stripped[0] in "[{" and stripped[-1] in "]}":
        return stripped

    # 从文本中搜索第一个 JSON 对象/数组
    decoder = json.JSONDecoder()
    for i, ch in enumerate(stripped):
        if ch not in "[{":
            continue
        try:
            _, end = decoder.raw_decode(stripped[i:])
            return stripped[i : i + end]
        except Exception:
            continue
    return None


def safe_json_loads(text: str) -> Optional[Dict[str, Any]]:
    """安全解析 JSON，支持修复和从混合文本中提取"""
    json_text = extract_json_block(text)
    if not json_text:
        return None

    # 先尝试标准解析
    try:
        return json.loads(json_text)
    except json.JSONDecodeError:
        pass

    # 尝试 json_repair
    try:
        import json_repair

        return json_repair.loads(json_text)
    except Exception:
        pass

    return None
