"""Products and cart lines."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Product:
    """Something the shop sells; prices are in cents."""

    name: str
    price_cents: int


@dataclass
class CartItem:
    """A product and how many of it."""

    product: Product
    quantity: int
