"""Contract tests for the local demo API and committed OpenAPI artifact."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from assay.demo_api import app

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[1]


def test_health_returns_ok() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_product_returns_201() -> None:
    response = client.post(
        "/products",
        json={"name": "Pencil", "price": 2.5, "category": "stationery"},
    )
    assert response.status_code == 201
    assert response.json()["name"] == "Pencil"


def test_product_name_minimum_and_maximum_lengths_are_enforced() -> None:
    valid = {"price": 2.5, "category": "stationery"}
    assert client.post("/products", json={"name": "ab", **valid}).status_code == 422
    assert client.post("/products", json={"name": "x" * 41, **valid}).status_code == 422


def test_product_price_range_and_enum_are_enforced() -> None:
    base = {"name": "Valid", "category": "book"}
    assert client.post("/products", json={**base, "price": 0}).status_code == 422
    assert client.post("/products", json={**base, "price": 10000.01}).status_code == 422
    assert client.post(
        "/products", json={**base, "price": 1, "category": "food"}
    ).status_code == 422


def test_invalid_field_types_are_rejected() -> None:
    assert client.post(
        "/products",
        json={"name": 123, "price": 2.5, "category": "book"},
    ).status_code == 422
    assert client.post(
        "/products",
        json={"name": "Valid", "price": "not-a-number", "category": "book"},
    ).status_code == 422
    assert client.patch(
        "/products/1/stock", json={"quantity": "many"}
    ).status_code == 422


def test_product_requires_name() -> None:
    assert client.post(
        "/products", json={"price": 1, "category": "book"}
    ).status_code == 422


def test_get_existing_and_unknown_product() -> None:
    assert client.get("/products/1").status_code == 200
    assert client.get("/products/99999").status_code == 404


def test_stock_quantity_boundaries_and_invalid_values() -> None:
    assert client.patch("/products/1/stock", json={"quantity": 0}).status_code == 200
    assert client.patch("/products/1/stock", json={"quantity": 1000}).status_code == 200
    assert client.patch("/products/1/stock", json={"quantity": 1001}).status_code == 422


def test_order_quantity_range_and_unknown_product() -> None:
    assert (
        client.post("/orders", json={"product_id": 1, "quantity": 10}).status_code
        == 201
    )
    assert (
        client.post("/orders", json={"product_id": 1, "quantity": 0}).status_code
        == 422
    )
    assert client.post(
        "/orders", json={"product_id": 99999, "quantity": 1}
    ).status_code == 404


def test_committed_openapi_matches_live_app_openapi() -> None:
    committed = json.loads(
        (ROOT / "specs" / "openapi.json").read_text(encoding="utf-8")
    )
    assert committed == app.openapi()


def test_openapi_request_schemas_have_expected_constraints() -> None:
    spec = app.openapi()
    schemas = spec["components"]["schemas"]

    def request_schema(path: str, method: str) -> dict:
        operation = spec["paths"][path][method]
        reference = operation["requestBody"]["content"]["application/json"]["schema"][
            "$ref"
        ]
        schema_name = reference.rsplit("/", 1)[-1]
        return schemas[schema_name]

    def resolve_schema(schema: dict) -> dict:
        while "$ref" in schema:
            schema = schemas[schema["$ref"].rsplit("/", 1)[-1]]
        return schema

    product = request_schema("/products", "post")["properties"]
    assert product["name"]["minLength"] == 3
    assert product["name"]["maxLength"] == 40
    assert product["price"]["exclusiveMinimum"] == 0
    assert product["price"]["maximum"] == 10000
    assert resolve_schema(product["category"])["enum"] == ["book", "game", "stationery"]

    stock = request_schema("/products/{product_id}/stock", "patch")["properties"]
    assert stock["quantity"]["minimum"] == 0
    assert stock["quantity"]["maximum"] == 1000

    order = request_schema("/orders", "post")["properties"]
    assert order["product_id"]["exclusiveMinimum"] == 0
    assert order["quantity"]["minimum"] == 1
    assert order["quantity"]["maximum"] == 10
