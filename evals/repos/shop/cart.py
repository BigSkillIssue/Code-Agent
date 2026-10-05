"""The shopping cart."""

from models import CartItem, Product


class Cart:
    """Products the customer wants to buy."""

    def __init__(self) -> None:
        self.items: list[CartItem] = []

    def add(self, product: Product, quantity: int = 1) -> None:
        """Add a product (more of it if it is already in the cart)."""
        for item in self.items:
            if item.product == product:
                item.quantity += quantity
                return
        self.items.append(CartItem(product, quantity))

    def total(self) -> int:
        """Total price in cents."""
        return sum(item.product.price_cents * item.quantity for item in self.items)
