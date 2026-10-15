# Offline schema baseline

The baseline reads an OpenAPI document already loaded into memory and returns
normalized BaselineCase records. It does not open files, read criteria.yaml,
or use acceptance criteria.

## Supported input subset

- JSON request bodies whose schema is an object.
- Required request bodies and required object properties.
- Required path parameters (including operation-level and path-item-level declarations).
- Primitive string, integer, and number fields.
- String minLength / maxLength and numeric inclusive/exclusive minimum/maximum.
- String enums and local references of the form #/components/schemas/Name.

Unknown keywords in request/input schemas, unresolved or external references,
non-path parameters, non-JSON request bodies, unsupported field types, and
unsupported body shapes raise UnsupportedSchemaError. The parser intentionally
does not attempt to guess how unsupported constructs should behave.

## Generated case categories

- valid: one representative valid request for each operation.
- boundary: each declared minimum/maximum and a representable value just
  outside it. For strings, this means the exact length and one character below
  or above; for numbers, the bound and a small step outside it.
- invalid_type: a wrong primitive type for each supported input field.
- missing_required: each required body property omitted and each required
  JSON body omitted.
- invalid_enum: an unsupported value for each enum field.

Every case has a stable SHA-256-derived case_id, a name containing method,
path, field, and test intent, and explicit expected_valid metadata. Output is
sorted by method, path, field, kind, name, and ID. normalize_cases provides a
canonical JSON-compatible representation for deterministic comparisons.

## Run

    pytest tests/test_baseline.py
    pytest

This is the schema-only baseline stage. Criteria mapping and generated pytest
files are deliberately out of scope here.
