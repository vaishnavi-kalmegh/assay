"""Small deterministic FastAPI service used as assay's test-generation target."""

from enum import Enum

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(
    title="assay Demo API",
    version="1.0.0",
    description="A local demo API with explicit validation rules for API testing.",
)


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


_PRODUCTS = {
    1: {"id": 1, "name": "Notebook", "price": 5.5, "category": "stationery", "stock": 10},
}
_NEXT_PRODUCT_ID = 2


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    """Return a simple service health response."""
    return {"status": "ok"}


@app.post("/products", status_code=201, tags=["products"])
def create_product(payload: ProductCreate) -> dict[str, object]:
    """Create a product after validating its fields."""
    global _NEXT_PRODUCT_ID
    product_id = _NEXT_PRODUCT_ID
    _NEXT_PRODUCT_ID += 1
    product = {
        "id": product_id,
        "name": payload.name,
        "price": payload.price,
        "category": payload.category.value,
        "stock": 0,
    }
    _PRODUCTS[product_id] = product
    return product


@app.get("/products/{product_id}", tags=["products"])
def get_product(product_id: int) -> dict[str, object]:
    """Return a product or a 404 for an unknown ID."""
    if product_id not in _PRODUCTS:
        raise HTTPException(status_code=404, detail="Product not found")
    return _PRODUCTS[product_id]


@app.patch("/products/{product_id}/stock", tags=["products"])
def update_stock(product_id: int, payload: StockUpdate) -> dict[str, object]:
    """Set product stock to a validated quantity."""
    if product_id not in _PRODUCTS:
        raise HTTPException(status_code=404, detail="Product not found")
    _PRODUCTS[product_id]["stock"] = payload.quantity
    return {"id": product_id, "stock": payload.quantity}


@app.post("/orders", status_code=201, tags=["orders"])
def create_order(payload: OrderCreate) -> dict[str, object]:
    """Create an order for an existing product."""
    if payload.product_id not in _PRODUCTS:
        raise HTTPException(status_code=404, detail="Product not found")
    return {
        "id": 1,
        "product_id": payload.product_id,
        "quantity": payload.quantity,
        "status": "created",
    }
