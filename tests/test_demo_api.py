"""Contract tests for the local demo API."""

from fastapi.testclient import TestClient

from assay.demo_api import app

client = TestClient(app)


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
    assert client.post("/orders", json={"product_id": 1, "quantity": 10}).status_code == 201
    assert client.post("/orders", json={"product_id": 1, "quantity": 0}).status_code == 422
    assert client.post(
        "/orders", json={"product_id": 99999, "quantity": 1}
    ).status_code == 404


def test_openapi_document_is_deterministic_and_matches_routes() -> None:
    import json

    first = json.dumps(app.openapi(), sort_keys=True)
    second = json.dumps(app.openapi(), sort_keys=True)
    assert first == second
    assert set(app.openapi()["paths"]) == {
        "/health",
        "/products",
        "/products/{product_id}",
        "/products/{product_id}/stock",
        "/orders",
    }
