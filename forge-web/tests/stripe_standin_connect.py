"""Stripe Connect for the payments tests (W35): the Stripe stand-in, plus Express accounts with
their onboarding and dashboard links, and calls made as a connected account (`Stripe-Account`),
whose objects that account alone can see, as at Stripe."""

from typing import Any
from urllib.parse import parse_qsl

import httpx

from stripe_standin import StripeStandIn, error

PERIOD_END = 1_900_000_000  # when a new subscription's first period ends


class ConnectStandIn(StripeStandIn):
    """Stripe with connected accounts."""

    def __init__(self) -> None:
        super().__init__()
        self.repeat_key = False  # Stripe's errors for connected accounts name the key used

    def handle(self, request: httpx.Request) -> httpx.Response:
        params = dict(parse_qsl(request.content.decode())) if request.content else {}
        path, account = request.url.path, request.headers.get("stripe-account", "")
        parts = path.strip("/").split("/")
        if path == "/v1/accounts" and request.method == "POST":
            return self.new_account(params)
        if path == "/v1/account_links" and request.method == "POST":
            return self.link(params.get("account", ""), "https://connect.stripe.com/setup/e/")
        if len(parts) == 4 and parts[:2] == ["v1", "accounts"] and parts[3] == "login_links":
            return self.link(parts[2], "https://connect.stripe.com/express/")
        if len(parts) == 3 and parts[:2] == ["v1", "accounts"] and request.method == "GET":
            found = self.objects.get(parts[2])
            return httpx.Response(200, json=found) if found else error(404, "No such account")
        if account:
            return self.as_account(request, account, params)
        return super().handle(request)

    def new_account(self, params: dict[str, str]) -> httpx.Response:
        made = self.make("/v1/accounts", params)
        found = self.objects[made.json()["id"]]
        found.update(charges_enabled=False, payouts_enabled=False, details_submitted=False)
        return httpx.Response(200, json=found)

    def link(self, account: str, base: str) -> httpx.Response:
        if account not in self.objects:
            return error(404, f"No such account: '{account}'")
        self.effects.append(("POST", "link", {"account": account}))
        return httpx.Response(200, json={"object": "link", "url": f"{base}{account}"})

    def as_account(
        self, request: httpx.Request, account: str, params: dict[str, str]
    ) -> httpx.Response:
        """A call made for a connected account: its own objects only."""
        if self.repeat_key:
            return error(400, f"Invalid request with the key {self.secret_key} for {account}")
        if self.objects.get(account, {}).get("object") != "acct":
            return error(403, "The provided key does not have access to account")
        path = request.url.path
        if request.method == "POST" and path == "/v1/checkout/sessions":
            made = self.make(path, params)
            found = self.objects[made.json()["id"]]
            found.update(account=account, status="open", payment_status="unpaid")
            return httpx.Response(200, json=found)
        object_id = path.rpartition("/")[2]
        found = self.objects.get(object_id)
        if found is None or found.get("account") != account:
            return error(404, f"No such object: '{object_id}'")
        if request.method == "POST":
            found.update(params)
            self.effects.append(("POST", path, params))
        return httpx.Response(200, json=found)

    def onboarded(self, account: str) -> None:
        """The maker finished Stripe's onboarding."""
        self.objects[account].update(charges_enabled=True, payouts_enabled=True,
                                     details_submitted=True)  # fmt: skip

    def paid(self, session_id: str) -> dict[str, Any]:
        """The customer paid on Stripe's page; a subscription begins if one was bought."""
        session = self.objects[session_id]
        session.update(status="complete", payment_status="paid",
                       customer_details={"email": "kim@example.org"})  # fmt: skip
        if session.get("mode") == "subscription":
            sub_id = f"sub_{next(self.ids)}"
            period = {"data": [{"current_period_end": PERIOD_END}]}
            self.objects[sub_id] = {"object": "sub", "id": sub_id, "account": session["account"],
                                    "status": "active", "cancel_at_period_end": False,
                                    "items": period}  # fmt: skip
            session["subscription"] = sub_id
        return session

    def sessions(self) -> list[dict[str, Any]]:
        """Every checkout opened for a connected account."""
        return [o for o in self.of("cs") if o.get("account")]
