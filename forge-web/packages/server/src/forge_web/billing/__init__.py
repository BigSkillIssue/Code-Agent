"""Billing (Phase E): customers pay the admin through the admin's own Stripe account.

`stripe_client` calls Stripe, `webhooks` takes Stripe's signed events, `events` stores each once
and handles them in order, and `setup` keeps the admin's Stripe keys in the vault.
"""
