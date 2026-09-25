"""Request body and query parameter validation.

The console takes JSON from the line network, so every field is read through
one of these helpers.  A missing required field, a wrong type and a value
outside its allowed range all raise the same :class:`ValidationError`, which
the router maps to HTTP 400 with the offending field named.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from kilnline.errors import ValidationError


def as_body(body: Any) -> dict[str, Any]:
    if body is None:
        return {}
    if not isinstance(body, Mapping):
        raise ValidationError("request body must be a JSON object")
    return {str(key): value for key, value in body.items()}


def field(body: Mapping[str, Any], name: str, *, required: bool = True, default: Any = None) -> Any:
    if name in body and body[name] is not None:
        return body[name]
    if required:
        raise ValidationError("required field is missing", field=name)
    return default


def text(
    body: Mapping[str, Any],
    name: str,
    *,
    required: bool = True,
    default: str = "",
    minimum_length: int = 1,
) -> str:
    value = field(body, name, required=required, default=default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValidationError("field must be a string", field=name)
    result = str(value).strip()
    if required and len(result) < int(minimum_length):
        raise ValidationError("field must not be blank", field=name)
    return result


def optional_text(body: Mapping[str, Any], name: str, *, default: str = "") -> str:
    return text(body, name, required=False, default=default)


def number(
    body: Mapping[str, Any],
    name: str,
    *,
    required: bool = True,
    default: float | None = None,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    value = field(body, name, required=required, default=default)
    if value is None:
        if not required:
            return None
        if default is None:
            raise ValidationError("required numeric field is missing", field=name)
        value = default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError("field must be a number", field=name)
    result = float(value)
    if result != result:
        raise ValidationError("field must be a finite number", field=name)
    if minimum is not None and result < float(minimum):
        raise ValidationError("field is below its minimum", field=name, minimum=minimum, value=result)
    if maximum is not None and result > float(maximum):
        raise ValidationError("field is above its maximum", field=name, maximum=maximum, value=result)
    return result


def integer(
    body: Mapping[str, Any],
    name: str,
    *,
    required: bool = True,
    default: int | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int | None:
    value = field(body, name, required=required, default=default)
    if value is None:
        if not required:
            return None
        if default is None:
            raise ValidationError("required integer field is missing", field=name)
        value = default
    if isinstance(value, bool):
        raise ValidationError("field must be an integer", field=name)
    if isinstance(value, str) and value.strip().lstrip("+-").isdigit():
        value = int(value.strip())
    if not isinstance(value, int):
        raise ValidationError("field must be an integer", field=name)
    result = int(value)
    if minimum is not None and result < int(minimum):
        raise ValidationError("field is below its minimum", field=name, minimum=minimum, value=result)
    if maximum is not None and result > int(maximum):
        raise ValidationError("field is above its maximum", field=name, maximum=maximum, value=result)
    return result


def boolean(body: Mapping[str, Any], name: str, *, default: bool = False) -> bool:
    if name not in body or body[name] is None:
        return bool(default)
    value = body[name]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    raise ValidationError("field must be a boolean", field=name)


def text_list(
    body: Mapping[str, Any],
    name: str,
    *,
    required: bool = True,
    default: Sequence[str] = (),
) -> list[str]:
    value = field(body, name, required=required, default=list(default))
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, Sequence):
        parts = [str(part).strip() for part in value]
    else:
        raise ValidationError("field must be a list of strings", field=name)
    cleaned = [part for part in parts if part]
    if required and not cleaned:
        raise ValidationError("field must contain at least one entry", field=name)
    return cleaned


def as_int_string(mapping: Mapping[str, Any], name: str, *, default: int) -> int:
    """Read an integer from a query mapping, falling back to ``default``."""

    raw = mapping.get(name)
    if raw in (None, ""):
        return int(default)
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError("query parameter must be an integer", parameter=name, value=raw) from exc


def as_float_string(mapping: Mapping[str, Any], name: str, *, default: float) -> float:
    """Read a number from a query mapping, falling back to ``default``."""

    raw = mapping.get(name)
    if raw in (None, ""):
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError("query parameter must be a number", parameter=name, value=raw) from exc
