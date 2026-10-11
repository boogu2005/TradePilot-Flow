"""Redact common credential fields before evidence is sent to a model."""
import re

SECRET_KEYS = re.compile(r"(?i)(api[_-]?key|secret|password|authorization|access[_-]?token|session_string)")
SECRET_TEXT = re.compile(r"(?i)((?:api[_-]?key|secret|password|authorization|access[_-]?token)\s*[=:]\s*)[^\s,;]+")


def redact(value):
    if isinstance(value, dict):
        return {str(key): "[REDACTED]" if SECRET_KEYS.search(str(key)) else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return SECRET_TEXT.sub(r"\1[REDACTED]", value)
    return value
