"""Small deterministic FastAPI service used as assay's test-generation target."""

from enum import Enum

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


class ProductCategory(str, Enum):
    book = "book"
    game = "game"
    stationery = "stationery"


class ProductCreate(BaseModel):
    name: str = Field(min_length=3, max_length=40)
    price: float = Field(gt=0, le=10000)
    category: ProductCategory


class StockUpdate(BaseModel):
    quantity: int = Field(ge=0, le=1000)


class OrderCreate(BaseModel):
    product_id: int = Field(gt=0)
    quantity: int = Field(ge=1, le=10)


# Hand-written mutants: each is one deliberate behaviour change to the demo API,
# used only to measure whether a test suite notices a change (mutation score).
# They are off by default; ``create_app()`` without ``mutant`` is the real API.
# The OpenAPI spec is always exported from the unmutated app, so a mutant is a
# silent implementation drift the generated tests must catch on their own.
MUTANTS: dict[str, str] = {
    "name_min_length_2": "product name minimum length relaxed from 3 to 2",
    "name_max_length_41": "product name maximum length relaxed from 40 to 41",
    "price_allows_zero": "product price may be zero (gt=0 becomes ge=0)",
    "price_max_exclusive": "product price 10000 is rejected (le=10000 becomes lt)",
    "category_accepts_toy": "product category also accepts the value 'toy'",
    "stock_allows_negative_one": "stock quantity may be -1 (ge=0 becomes ge=-1)",
    "order_quantity_max_11": "order quantity maximum relaxed from 10 to 11",
    "get_unknown_product_200": "GET of an unknown product returns 200, not 404",
    "order_unknown_product_201": "an order for an unknown product is accepted",
    "health_body_wrong": "GET /health returns status 'up' instead of 'ok'",
}


class _ProductCategoryWithToy(str, Enum):
    book = "book"
    game = "game"
    stationery = "stationery"
    toy = "toy"


class _NameMin2(ProductCreate):
    name: str = Field(min_length=2, max_length=40)


class _NameMax41(ProductCreate):
    name: str = Field(min_length=3, max_length=41)


class _PriceZero(ProductCreate):
    price: float = Field(ge=0, le=10000)


class _PriceMaxExclusive(ProductCreate):
    price: float = Field(gt=0, lt=10000)


class _CategoryToy(ProductCreate):
    category: _ProductCategoryWithToy


class _StockMinusOne(StockUpdate):
    quantity: int = Field(ge=-1, le=1000)


class _OrderMax11(OrderCreate):
    quantity: int = Field(ge=1, le=11)


def create_app(*, seeded: bool = True, mutant: str | None = None) -> FastAPI:
    """Build a demo API instance that owns its own in-memory product state.

    Each call returns an independent app, so tests never share products. With
    ``seeded=True`` the app starts with the original "Notebook" product (ID 1).
    With ``seeded=False`` it starts empty and the first created product gets ID 1.
    ``mutant`` names one entry of ``MUTANTS``; None (the default) is the real API.
    """
    if mutant is not None and mutant not in MUTANTS:
        known = ", ".join(sorted(MUTANTS))
        raise ValueError(f"unknown mutant {mutant!r}; known mutants: {known}")
    product_model: type[ProductCreate] = {
        "name_min_length_2": _NameMin2,
        "name_max_length_41": _NameMax41,
        "price_allows_zero": _PriceZero,
        "price_max_exclusive": _PriceMaxExclusive,
        "category_accepts_toy": _CategoryToy,
    }.get(mutant or "", ProductCreate)
    stock_model: type[StockUpdate] = (
        _StockMinusOne if mutant == "stock_allows_negative_one" else StockUpdate
    )
    order_model: type[OrderCreate] = (
        _OrderMax11 if mutant == "order_quantity_max_11" else OrderCreate
    )
    app = FastAPI(
        title="assay Demo API",
        version="1.0.0",
        description="A local demo API with explicit validation rules for API testing.",
    )
    products: dict[int, dict[str, object]] = {}
    next_product_id = 1
    if seeded:
        products[1] = {
            "id": 1,
            "name": "Notebook",
            "price": 5.5,
            "category": "stationery",
            "stock": 10,
        }
        next_product_id = 2

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        """Return a simple service health response."""
        return {"status": "up" if mutant == "health_body_wrong" else "ok"}

    @app.post("/products", status_code=201, tags=["products"])
    def create_product(payload: product_model) -> dict[str, object]:
        """Create a product after validating its fields."""
        nonlocal next_product_id
        product_id = next_product_id
        next_product_id += 1
        product = {
            "id": product_id,
            "name": payload.name,
            "price": payload.price,
            "category": str(getattr(payload.category, "value", payload.category)),
            "stock": 0,
        }
        products[product_id] = product
        return product

    @app.get("/products/{product_id}", tags=["products"])
    def get_product(product_id: int) -> dict[str, object]:
        """Return a product or a 404 for an unknown ID."""
        if product_id not in products:
            if mutant == "get_unknown_product_200":
                return {"id": product_id}
            raise HTTPException(status_code=404, detail="Product not found")
        return products[product_id]

    @app.patch("/products/{product_id}/stock", tags=["products"])
    def update_stock(product_id: int, payload: stock_model) -> dict[str, object]:
        """Set product stock to a validated quantity."""
        if product_id not in products:
            raise HTTPException(status_code=404, detail="Product not found")
        products[product_id]["stock"] = payload.quantity
        return {"id": product_id, "stock": payload.quantity}

    @app.post("/orders", status_code=201, tags=["orders"])
    def create_order(payload: order_model) -> dict[str, object]:
        """Create an order for an existing product."""
        if payload.product_id not in products:
            if mutant != "order_unknown_product_201":
                raise HTTPException(status_code=404, detail="Product not found")
        return {
            "id": 1,
            "product_id": payload.product_id,
            "quantity": payload.quantity,
            "status": "created",
        }

    return app


# The normal seeded demo instance. Existing imports and the OpenAPI export use it.
app = create_app()
