"""A store holding items."""

from items import Item


class Store:
    def __init__(self) -> None:
        self.items: dict[str, Item] = {}

    def add_item(self, item: Item) -> None:
        self.items[item.name] = item

    def total_value(self) -> float:
        return sum(item.price for item in self.items.values())

    def is_low(self, name: str) -> bool:
        return self.items[name].quantity < 5
