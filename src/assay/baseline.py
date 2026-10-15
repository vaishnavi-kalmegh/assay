"""Deterministic schema-only case generation for a small OpenAPI subset.

This module accepts an already-loaded OpenAPI document. It never reads files or
acceptance criteria; callers own all input/output.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class UnsupportedSchemaError(ValueError):
    """Raised when an OpenAPI construct is outside the explicitly supported subset."""


class BaselineCase(BaseModel):
    """One normalized, deterministic API input case."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    name: str
    method: str
    path: str
    kind: Literal[
        "valid",
        "boundary",
        "invalid_type",
        "missing_required",
        "invalid_enum",
    ]
    field: str
    path_parameters: dict[str, Any] = Field(default_factory=dict)
    body: dict[str, Any] | None = None
    expected_valid: bool


_HTTP_METHODS = {"delete", "get", "head", "options", "patch", "post", "put"}
_SCHEMA_KEYS = {
    "$ref",
    "title",
    "description",
    "type",
    "properties",
    "required",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "enum",
}
_ROOT_KEYS = {
    "openapi",
    "info",
    "servers",
    "paths",
    "components",
    "security",
    "tags",
    "externalDocs",
}
_PATH_ITEM_KEYS = {"summary", "description", "servers", "parameters", *_HTTP_METHODS}


def _unsupported(where: str, detail: str) -> UnsupportedSchemaError:
    return UnsupportedSchemaError(f"Unsupported OpenAPI schema at {where}: {detail}")


def _check_keys(value: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise _unsupported(where, f"unsupported key(s): {', '.join(unknown)}")


def _resolve_schema(
    schema: dict[str, Any], document: dict[str, Any], where: str
) -> dict[str, Any]:
    if not isinstance(schema, dict):
        raise _unsupported(where, "schema must be an object")
    _check_keys(schema, _SCHEMA_KEYS, where)
    if "$ref" not in schema:
        return schema
    if len(schema) != 1:
        raise _unsupported(where, "$ref cannot be combined with sibling keywords")
    reference = schema["$ref"]
    prefix = "#/components/schemas/"
    if not isinstance(reference, str) or not reference.startswith(prefix):
        raise _unsupported(
            where, f"only local schema references are supported: {reference!r}"
        )
    name = reference[len(prefix) :]
    schemas = document.get("components", {}).get("schemas", {})
    if not name or name not in schemas:
        raise _unsupported(where, f"unresolved schema reference: {reference}")
    target = schemas[name]
    if not isinstance(target, dict):
        raise _unsupported(where, f"referenced schema {name!r} must be an object")
    return _resolve_schema(target, document, f"components.schemas.{name}")


def _validate_field_schema(
    schema: dict[str, Any], document: dict[str, Any], where: str
) -> dict[str, Any]:
    resolved = _resolve_schema(schema, document, where)
    schema_type = resolved.get("type")
    if "enum" in resolved:
        if not isinstance(resolved["enum"], list) or not resolved["enum"]:
            raise _unsupported(where, "enum must be a non-empty array")
        if schema_type not in {"string", None}:
            raise _unsupported(where, "enum is supported only for strings")
    elif schema_type not in {"string", "integer", "number"}:
        raise _unsupported(where, f"field type {schema_type!r} is not supported")

    if schema_type == "string":
        for key in ("minLength", "maxLength"):
            if key in resolved and (
                not isinstance(resolved[key], int) or resolved[key] < 0
            ):
                raise _unsupported(where, f"{key} must be a non-negative integer")
        if (
            "minLength" in resolved
            and "maxLength" in resolved
            and resolved["minLength"] > resolved["maxLength"]
        ):
            raise _unsupported(where, "minLength cannot exceed maxLength")
        if any(key in resolved for key in (
            "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"
        )):
            raise _unsupported(where, "numeric bounds cannot be applied to strings")
    if schema_type in {"integer", "number"}:
        if any(key in resolved for key in ("minLength", "maxLength")):
            raise _unsupported(where, "string length cannot be applied to numbers")
        for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
            if key in resolved and not isinstance(resolved[key], (int, float)):
                raise _unsupported(where, f"{key} must be numeric")
        low = resolved.get("exclusiveMinimum", resolved.get("minimum"))
        high = resolved.get("exclusiveMaximum", resolved.get("maximum"))
        if low is not None and high is not None and low > high:
            raise _unsupported(where, "minimum bound cannot exceed maximum bound")
    return resolved


def _parse_body_schema(
    operation: dict[str, Any], document: dict[str, Any], method: str, path: str
) -> tuple[dict[str, dict[str, Any]] | None, set[str], bool]:
    request_body = operation.get("requestBody")
    if request_body is None:
        return None, set(), False
    if not isinstance(request_body, dict):
        raise _unsupported(f"{method.upper()} {path}.requestBody", "must be an object")
    _check_keys(
        request_body,
        {"description", "required", "content"},
        f"{method.upper()} {path}.requestBody",
    )
    content = request_body.get("content")
    if not isinstance(content, dict) or set(content) != {"application/json"}:
        raise _unsupported(
            f"{method.upper()} {path}.requestBody.content",
            "only application/json request bodies are supported",
        )
    media = content["application/json"]
    if not isinstance(media, dict):
        raise _unsupported(
            f"{method.upper()} {path}.requestBody", "media type must be an object"
        )
    _check_keys(media, {"schema", "example", "examples", "encoding"},
                f"{method.upper()} {path}.requestBody.application/json")
    if set(media) - {"schema"}:
        raise _unsupported(
            f"{method.upper()} {path}.requestBody.application/json",
            "examples and encoding are not supported",
        )
    body_schema = media.get("schema")
    if not isinstance(body_schema, dict):
        raise _unsupported(f"{method.upper()} {path}.requestBody.schema", "is required")
    body_schema = _resolve_schema(
        body_schema, document, f"{method.upper()} {path}.requestBody.schema"
    )
    if body_schema.get("type") != "object":
        raise _unsupported(
            f"{method.upper()} {path}.requestBody.schema", "must be an object"
        )
    _check_keys(
        body_schema,
        {"title", "description", "type", "properties", "required"},
        f"{method.upper()} {path}.requestBody.schema",
    )
    properties = body_schema.get("properties")
    if not isinstance(properties, dict):
        raise _unsupported(
            f"{method.upper()} {path}.requestBody.schema.properties",
            "must be an object",
        )
    parsed = {
        name: _validate_field_schema(
            schema, document, f"{method.upper()} {path}.body.{name}"
        )
        for name, schema in sorted(properties.items())
    }
    required = body_schema.get("required", [])
    if not isinstance(required, list) or any(
        not isinstance(item, str) for item in required
    ):
        raise _unsupported(
            f"{method.upper()} {path}.requestBody.schema.required",
            "must be an array of strings",
        )
    unknown_required = sorted(set(required) - set(parsed))
    if unknown_required:
        raise _unsupported(
            f"{method.upper()} {path}.requestBody.schema.required",
            f"required field(s) missing from properties: {', '.join(unknown_required)}",
        )
    if not isinstance(request_body.get("required", False), bool):
        raise _unsupported(
            f"{method.upper()} {path}.requestBody.required", "must be boolean"
        )
    return parsed, set(required), request_body.get("required", False)


def _parse_path_parameters(
    path_item: dict[str, Any], operation: dict[str, Any], document: dict[str, Any],
    method: str, path: str,
) -> dict[str, dict[str, Any]]:
    combined: dict[str, dict[str, Any]] = {}
    for source in (path_item.get("parameters", []), operation.get("parameters", [])):
        if not isinstance(source, list):
            raise _unsupported(
                f"{method.upper()} {path}.parameters", "must be an array"
            )
        for parameter in source:
            if not isinstance(parameter, dict):
                raise _unsupported(
                    f"{method.upper()} {path}.parameters", "parameter must be an object"
                )
            _check_keys(
                parameter,
                {"name", "in", "required", "description", "schema", "example"},
                f"{method.upper()} {path}.parameters",
            )
            if parameter.get("in") != "path":
                raise _unsupported(
                    f"{method.upper()} {path}.parameters",
                    "only path parameters are supported",
                )
            name = parameter.get("name")
            if not isinstance(name, str) or not name:
                raise _unsupported(
                    f"{method.upper()} {path}.parameters", "parameter name is required"
                )
            if parameter.get("required") is not True:
                raise _unsupported(
                    f"{method.upper()} {path}.parameters.{name}",
                    "path parameters must be required",
                )
            schema = parameter.get("schema")
            if not isinstance(schema, dict):
                raise _unsupported(
                    f"{method.upper()} {path}.parameters.{name}", "schema is required"
                )
            combined[name] = _validate_field_schema(
                schema, document, f"{method.upper()} {path}.path.{name}"
            )
    return dict(sorted(combined.items()))


def _valid_value(schema: dict[str, Any]) -> Any:
    if "enum" in schema:
        return sorted(
            schema["enum"], key=lambda value: json.dumps(value, sort_keys=True)
        )[0]
    schema_type = schema.get("type")
    if schema_type == "string":
        minimum = schema.get("minLength", 0)
        length = max(minimum, 1)
        if "maxLength" in schema:
            length = min(length, schema["maxLength"])
        return "x" * length
    if schema_type == "integer":
        lower = schema.get("exclusiveMinimum")
        if lower is not None:
            return int(lower) + 1
        lower = schema.get("minimum")
        if lower is not None:
            return int(lower)
        return 1
    if schema_type == "number":
        lower = schema.get("exclusiveMinimum")
        if lower is not None:
            return float(lower) + 0.01
        lower = schema.get("minimum")
        if lower is not None:
            return float(lower)
        return 1.0
    raise _unsupported("field", f"cannot make a valid value for type {schema_type!r}")


def _type_invalid(schema: dict[str, Any]) -> Any:
    return {"string": 123, "integer": "not-an-integer", "number": "not-a-number"}.get(
        schema.get("type"), 123
    )


def _add_case(
    cases: list[BaselineCase], method: str, path: str, kind: str, field: str,
    path_parameters: dict[str, Any], body: dict[str, Any] | None,
    expected_valid: bool, description: str,
) -> None:
    name = f"{method.upper()} {path} — {field} — {description}"
    identity = {
        "method": method.upper(),
        "path": path,
        "kind": kind,
        "field": field,
        "path_parameters": path_parameters,
        "body": body,
        "expected_valid": expected_valid,
        "name": name,
    }
    canonical = json.dumps(
        identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    case_id = (
        "BASE-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12].upper()
    )
    cases.append(BaselineCase(
        case_id=case_id, name=name, method=method.upper(), path=path, kind=kind,
        field=field, path_parameters=path_parameters, body=body,
        expected_valid=expected_valid,
    ))


def _numeric_step(schema: dict[str, Any]) -> int | float:
    return 1 if schema.get("type") == "integer" else 0.01


def _boundary_cases(
    cases: list[BaselineCase], method: str, path: str, field: str,
    schema: dict[str, Any], base_path: dict[str, Any], base_body: dict[str, Any] | None,
    is_path: bool,
) -> None:
    def emit(value: Any, description: str, valid: bool) -> None:
        params = dict(base_path)
        body = None if base_body is None else dict(base_body)
        if is_path:
            params[field.removeprefix("path.")] = value
        elif body is not None:
            body[field.removeprefix("body.")] = value
        _add_case(
            cases, method, path, "boundary", field, params, body, valid, description
        )

    schema_type = schema.get("type")
    if schema_type == "string":
        if "minLength" in schema:
            bound = schema["minLength"]
            emit("x" * bound, f"minimum length {bound}", True)
            if bound > 0:
                emit("x" * (bound - 1), f"just below minimum length {bound}", False)
        if "maxLength" in schema:
            bound = schema["maxLength"]
            emit("x" * bound, f"maximum length {bound}", True)
            emit("x" * (bound + 1), f"just above maximum length {bound}", False)
    elif schema_type in {"integer", "number"}:
        step = _numeric_step(schema)
        for key, label, direction in (
            ("minimum", "minimum", -1),
            ("exclusiveMinimum", "exclusive minimum", -1),
            ("maximum", "maximum", 1),
            ("exclusiveMaximum", "exclusive maximum", 1),
        ):
            if key not in schema:
                continue
            bound = schema[key]
            valid_at_bound = key in {"minimum", "maximum"}
            emit(bound, f"{label} boundary {bound}", valid_at_bound)
            emit(bound + direction * step, f"just outside {label} {bound}", False)


def generate_cases(document: dict[str, Any]) -> list[BaselineCase]:
    """Parse the supported OpenAPI subset and return sorted baseline cases.

    Supported: JSON object request bodies, required body/property fields, path
    parameters, string min/max length, numeric min/max and exclusive bounds, enum,
    and primitive string/integer/number types. Unsupported input-schema constructs
    raise UnsupportedSchemaError rather than being ignored.
    """
    if not isinstance(document, dict):
        raise _unsupported("document", "must be a JSON object")
    _check_keys(document, _ROOT_KEYS, "document")
    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise _unsupported("document.paths", "must be an object")
    cases: list[BaselineCase] = []
    for path in sorted(paths):
        path_item = paths[path]
        if not isinstance(path_item, dict):
            raise _unsupported(f"paths.{path}", "path item must be an object")
        _check_keys(path_item, _PATH_ITEM_KEYS, f"paths.{path}")
        for method in sorted(set(path_item) & _HTTP_METHODS):
            operation = path_item[method]
            if not isinstance(operation, dict):
                raise _unsupported(
                    f"{method.upper()} {path}", "operation must be an object"
                )
            _check_keys(
                operation,
                {
                    "operationId", "summary", "description", "tags", "parameters",
                    "requestBody", "responses", "deprecated", "security", "servers",
                    "externalDocs",
                },
                f"{method.upper()} {path}",
            )
            path_schemas = _parse_path_parameters(
                path_item, operation, document, method, path
            )
            body_schemas, required_fields, body_required = _parse_body_schema(
                operation, document, method, path
            )
            base_path = {
                name: _valid_value(schema) for name, schema in path_schemas.items()
            }
            base_body = (
                {name: _valid_value(schema) for name, schema in body_schemas.items()}
                if body_schemas is not None else None
            )
            if body_schemas is None and not base_path:
                _add_case(
                    cases, method, path, "valid", "request", {}, None, True,
                    "valid request",
                )
            else:
                valid_field = "request body" if base_body is not None else (
                    "path." + next(iter(base_path))
                )
                _add_case(cases, method, path, "valid", valid_field, base_path,
                          base_body, True, "valid input")
            if body_required:
                _add_case(cases, method, path, "missing_required", "body", base_path,
                          None, False, "missing required JSON body")
            if body_schemas is not None and base_body is not None:
                for field, schema in body_schemas.items():
                    field_name = f"body.{field}"
                    _add_case(
                        cases, method, path, "invalid_type", field_name, base_path,
                              {**base_body, field: _type_invalid(schema)}, False,
                              "invalid field type")
                    if field in required_fields:
                        missing = dict(base_body)
                        del missing[field]
                        _add_case(cases, method, path, "missing_required", field_name,
                                  base_path, missing, False, "missing required field")
                    if "enum" in schema:
                        invalid = dict(base_body)
                        invalid[field] = "__invalid_enum_value__"
                        _add_case(cases, method, path, "invalid_enum", field_name,
                                  base_path, invalid, False, "invalid enum value")
                    _boundary_cases(cases, method, path, field_name, schema,
                                    base_path, base_body, False)
            for field, schema in path_schemas.items():
                field_name = f"path.{field}"
                _add_case(cases, method, path, "invalid_type", field_name,
                          {**base_path, field: _type_invalid(schema)}, base_body,
                          False, "invalid path parameter type")
                _boundary_cases(cases, method, path, field_name, schema,
                                base_path, base_body, True)
    cases.sort(key=lambda case: (
        case.method, case.path, case.field, case.kind, case.name, case.case_id
    ))
    ids = [case.case_id for case in cases]
    if len(ids) != len(set(ids)):
        raise RuntimeError("stable baseline case ID collision detected")
    return cases


def normalize_cases(cases: list[BaselineCase]) -> list[dict[str, Any]]:
    """Return canonical JSON-compatible cases for repeatability checks."""
    return [case.model_dump(mode="json") for case in cases]
