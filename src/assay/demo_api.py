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


def create_app(*, seeded: bool = True) -> FastAPI:
    """Build a demo API instance that owns its own in-memory product state.

    Each call returns an independent app, so tests never share products. With
    ``seeded=True`` the app starts with the original "Notebook" product (ID 1).
    With ``seeded=False`` it starts empty and the first created product gets ID 1.
    """
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
        return {"status": "ok"}

    @app.post("/products", status_code=201, tags=["products"])
    def create_product(payload: ProductCreate) -> dict[str, object]:
        """Create a product after validating its fields."""
        nonlocal next_product_id
        product_id = next_product_id
        next_product_id += 1
        product = {
            "id": product_id,
            "name": payload.name,
            "price": payload.price,
            "category": payload.category.value,
            "stock": 0,
        }
        products[product_id] = product
        return product

    @app.get("/products/{product_id}", tags=["products"])
    def get_product(product_id: int) -> dict[str, object]:
        """Return a product or a 404 for an unknown ID."""
        if product_id not in products:
            raise HTTPException(status_code=404, detail="Product not found")
        return products[product_id]

    @app.patch("/products/{product_id}/stock", tags=["products"])
    def update_stock(product_id: int, payload: StockUpdate) -> dict[str, object]:
        """Set product stock to a validated quantity."""
        if product_id not in products:
            raise HTTPException(status_code=404, detail="Product not found")
        products[product_id]["stock"] = payload.quantity
        return {"id": product_id, "stock": payload.quantity}

    @app.post("/orders", status_code=201, tags=["orders"])
    def create_order(payload: OrderCreate) -> dict[str, object]:
        """Create an order for an existing product."""
        if payload.product_id not in products:
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
