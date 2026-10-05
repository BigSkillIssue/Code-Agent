"""Inventory items."""

from dataclasses import dataclass


@dataclass
class Item:
    name: str
    price: float
    quantity: int
