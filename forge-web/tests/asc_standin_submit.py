"""The submission half of the App Store Connect stand-in (W22d2): versions and their texts, app
information, age rating, content rights, price, availability, the contact for App Review,
review submissions and releases.

Apple's rules that matter here are kept: "What's new" is refused for an app's first version, a
version needs a build, texts, a support URL, a privacy policy, an age rating, content rights,
a price, availability, a review contact and an iPhone screenshot before it can be submitted,
and only an approved version can be released.
"""

from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request, Response

from asc_standin_release import ReleaseState, new_id, resource
from asc_standin_store import StoreState

TERRITORIES = ["USA", "DEU", "FRA", "JPN"]


@dataclass
class SubmitState:
    """What App Store Connect knows about versions and reviews."""

    versions: dict[str, dict[str, Any]] = field(default_factory=dict)
    infos: dict[str, dict[str, Any]] = field(default_factory=dict)  # app id -> app info
    app_terms: dict[str, dict[str, Any]] = field(default_factory=dict)  # app id -> terms
    submissions: dict[str, dict[str, Any]] = field(default_factory=dict)
    released: list[str] = field(default_factory=list)
    review_outcome: str = "PENDING_DEVELOPER_RELEASE"  # what App Review decides


def body_of(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """A request's attributes and relationships."""
    return data.get("attributes", {}) or {}, data.get("relationships", {}) or {}


def version_routes(
    app: FastAPI, state: SubmitState, store: StoreState, release: ReleaseState, error: Any
) -> None:
    """Versions, their builds and texts."""

    @app.get("/v1/apps/{app_id}/appStoreVersions")
    async def versions(app_id: str, request: Request) -> Any:
        platform = request.query_params.get("filter[platform]")
        found = [
            {"type": "appStoreVersions", "id": vid, "attributes": v["attributes"]}
            for vid, v in state.versions.items()
            if v["app"] == app_id and platform in (None, v["attributes"]["platform"])
        ]
        return {"data": found, "links": {}}  # fmt: skip

    @app.post("/v1/appStoreVersions")
    async def new_version(request: Request) -> Any:
        attributes, relations = body_of((await request.json())["data"])
        vid = new_id()
        state.versions[vid] = {
            "app": relations["app"]["data"]["id"],
            "build": None,
            "localizations": {},
            "review": None,
            "attributes": {**attributes, "appVersionState": "PREPARE_FOR_SUBMISSION"},
        }
        return resource("appStoreVersions", vid, state.versions[vid]["attributes"])  # fmt: skip

    @app.get("/v1/appStoreVersions/{vid}")
    async def version(vid: str) -> Any:
        found = state.versions.get(vid)
        if found is None:
            return error(404, "There is no such version.")
        return resource("appStoreVersions", vid, found["attributes"])

    @app.patch("/v1/appStoreVersions/{vid}")
    async def change_version(vid: str, request: Request) -> Any:
        attributes, _ = body_of((await request.json())["data"])
        state.versions[vid]["attributes"].update(attributes)
        return resource("appStoreVersions", vid, state.versions[vid]["attributes"])

    @app.patch("/v1/appStoreVersions/{vid}/relationships/build")
    async def set_build(vid: str, request: Request) -> Any:
        build_id = (await request.json())["data"]["id"]
        if release.builds.get(build_id, {}).get("attributes", {}).get("processingState") != "VALID":
            return error(409, "The build is not valid for this version.")
        state.versions[vid]["build"] = build_id
        return Response(status_code=204)

    @app.get("/v1/appStoreVersions/{vid}/appStoreVersionLocalizations")
    async def localizations(vid: str) -> Any:
        found = [{"type": "appStoreVersionLocalizations", "id": lid,
                  "attributes": store.localizations[lid]["attributes"]}
                 for lid in state.versions[vid]["localizations"]]  # fmt: skip
        return {"data": found, "links": {}}

    @app.post("/v1/appStoreVersionLocalizations")
    async def new_localization(request: Request) -> Any:
        attributes, relations = body_of((await request.json())["data"])
        vid = relations["appStoreVersion"]["data"]["id"]
        if problem := whats_new_problem(state, vid, attributes):
            return error(409, problem)
        lid = new_id()
        store.localizations[lid] = {"version": vid, "attributes": attributes}
        state.versions[vid]["localizations"][lid] = True
        return resource("appStoreVersionLocalizations", lid, attributes)

    @app.patch("/v1/appStoreVersionLocalizations/{lid}")
    async def change_localization(lid: str, request: Request) -> Any:
        attributes, _ = body_of((await request.json())["data"])
        found = store.localizations[lid]
        if problem := whats_new_problem(state, found["version"], attributes):
            return error(409, problem)
        found["attributes"].update(attributes)
        return resource("appStoreVersionLocalizations", lid, found["attributes"])

    @app.get("/v1/appStoreVersionLocalizations/{lid}/placements")
    async def placements(lid: str) -> Any:
        found = [{"type": "appAssetLibraryPlacements", "id": pid, "attributes": {}}
                 for pid, p in store.placements.items() if p["localization"] == lid]  # fmt: skip
        return {"data": found, "links": {}}

    @app.delete("/v1/appAssetLibraryPlacements/{pid}")
    async def unplace(pid: str) -> Any:
        store.placements.pop(pid, None)
        return Response(status_code=204)


def whats_new_problem(state: SubmitState, vid: str, attributes: dict[str, Any]) -> str:
    """Apple's rule: no "What's new" for an app's first version on a platform."""
    if not attributes.get("whatsNew"):
        return ""
    this = state.versions[vid]
    earlier = [v for k, v in state.versions.items() if k != vid and v["app"] == this["app"]
               and v["attributes"]["appVersionState"] == "READY_FOR_DISTRIBUTION"]  # fmt: skip
    return "" if earlier else "whatsNew cannot be edited for the first version of an app."


def app_info_routes(app: FastAPI, state: SubmitState, apps: Any, error: Any) -> None:
    """App information, its texts, categories and the age rating."""

    def info(app_id: str) -> dict[str, Any]:
        return state.infos.setdefault(app_id, {"id": f"info-{app_id}", "localizations": {},
                                               "categories": {}, "age": None})  # fmt: skip

    @app.get("/v1/apps/{app_id}/appInfos")
    async def infos(app_id: str) -> Any:
        found = info(app_id)
        return {"data": [{"type": "appInfos", "id": found["id"],
                          "attributes": {"appStoreState": "PREPARE_FOR_SUBMISSION"}}],
                "links": {}}  # fmt: skip

    def by_id(info_id: str) -> dict[str, Any]:
        return next(i for i in state.infos.values() if i["id"] == info_id)

    @app.patch("/v1/appInfos/{info_id}")
    async def categories(info_id: str, request: Request) -> Any:
        _, relations = body_of((await request.json())["data"])
        by_id(info_id)["categories"] = {k: v["data"]["id"] for k, v in relations.items()}
        return resource("appInfos", info_id, {})

    @app.get("/v1/appInfos/{info_id}/appInfoLocalizations")
    async def info_texts(info_id: str) -> Any:
        found = [{"type": "appInfoLocalizations", "id": lid, "attributes": a}
                 for lid, a in by_id(info_id)["localizations"].items()]  # fmt: skip
        return {"data": found, "links": {}}

    @app.post("/v1/appInfoLocalizations")
    async def new_info_text(request: Request) -> Any:
        attributes, relations = body_of((await request.json())["data"])
        lid = new_id()
        by_id(relations["appInfo"]["data"]["id"])["localizations"][lid] = attributes
        return resource("appInfoLocalizations", lid, attributes)

    @app.patch("/v1/appInfoLocalizations/{lid}")
    async def change_info_text(lid: str, request: Request) -> Any:
        attributes, _ = body_of((await request.json())["data"])
        for found in state.infos.values():
            if lid in found["localizations"]:
                found["localizations"][lid].update(attributes)
        return resource("appInfoLocalizations", lid, attributes)

    @app.get("/v1/appInfos/{info_id}/ageRatingDeclaration")
    async def age(info_id: str) -> Any:
        return resource("ageRatingDeclarations", f"age-{info_id}", {})

    @app.patch("/v1/ageRatingDeclarations/{age_id}")
    async def change_age(age_id: str, request: Request) -> Any:
        attributes, _ = body_of((await request.json())["data"])
        if "alcoholTobaccoOrDrugUseOrReferences" not in attributes:
            return error(409, "The age rating answers are incomplete.")
        by_id(age_id.removeprefix("age-"))["age"] = attributes
        return resource("ageRatingDeclarations", age_id, attributes)


def terms_routes(app: FastAPI, state: SubmitState, error: Any) -> None:
    """Content rights, price and availability."""

    def terms(app_id: str) -> dict[str, Any]:
        return state.app_terms.setdefault(app_id, {})

    @app.patch("/v1/apps/{app_id}")
    async def change_app(app_id: str, request: Request) -> Any:
        attributes, _ = body_of((await request.json())["data"])
        terms(app_id).update(attributes)
        return resource("apps", app_id, attributes)

    @app.get("/v1/apps/{app_id}/appPriceSchedule")
    async def price(app_id: str) -> Any:
        if "price" not in terms(app_id):
            return error(404, "There is no price schedule.")
        return resource("appPriceSchedules", f"price-{app_id}", {})

    @app.get("/v1/apps/{app_id}/appPricePoints")
    async def points(app_id: str) -> Any:
        found = [{"type": "appPricePoints", "id": f"pp-{n}", "attributes": {"customerPrice": p}}
                 for n, p in enumerate(("0.99", "0.0", "1.99"))]  # fmt: skip
        return {"data": found, "links": {}}

    @app.post("/v1/appPriceSchedules")
    async def new_price(request: Request) -> Any:
        sent = await request.json()
        app_id = sent["data"]["relationships"]["app"]["data"]["id"]
        point = sent["included"][0]["relationships"]["appPricePoint"]["data"]["id"]
        terms(app_id)["price"] = point
        return resource("appPriceSchedules", f"price-{app_id}", {})

    @app.get("/v1/apps/{app_id}/appAvailabilityV2")
    async def availability(app_id: str) -> Any:
        if "territories" not in terms(app_id):
            return error(404, "There is no availability.")
        return resource("appAvailabilities", f"av-{app_id}", {})

    @app.get("/v1/territories")
    async def territories() -> Any:
        return {"data": [{"type": "territories", "id": t} for t in TERRITORIES], "links": {}}

    @app.post("/v2/appAvailabilities")
    async def new_availability(request: Request) -> Any:
        sent = await request.json()
        app_id = sent["data"]["relationships"]["app"]["data"]["id"]
        terms(app_id)["territories"] = [i["relationships"]["territory"]["data"]["id"]
                                        for i in sent["included"]]  # fmt: skip
        return resource("appAvailabilities", f"av-{app_id}", {})


def review_routes(app: FastAPI, state: SubmitState, store: StoreState, error: Any) -> None:
    """The contact for App Review, review submissions and releases."""

    @app.get("/v1/appStoreVersions/{vid}/appStoreReviewDetail")
    async def detail(vid: str) -> Any:
        found = state.versions[vid]["review"]
        if found is None:
            return error(404, "There is no review detail.")
        return resource("appStoreReviewDetails", f"rd-{vid}", found)

    @app.post("/v1/appStoreReviewDetails")
    async def new_detail(request: Request) -> Any:
        attributes, relations = body_of((await request.json())["data"])
        vid = relations["appStoreVersion"]["data"]["id"]
        state.versions[vid]["review"] = attributes
        return resource("appStoreReviewDetails", f"rd-{vid}", attributes)

    @app.patch("/v1/appStoreReviewDetails/{rid}")
    async def change_detail(rid: str, request: Request) -> Any:
        attributes, _ = body_of((await request.json())["data"])
        state.versions[rid.removeprefix("rd-")]["review"] = attributes
        return resource("appStoreReviewDetails", rid, attributes)

    @app.post("/v1/reviewSubmissions")
    async def new_submission(request: Request) -> Any:
        attributes, relations = body_of((await request.json())["data"])
        sid = new_id()
        state.submissions[sid] = {
            "app": relations["app"]["data"]["id"],
            "items": [],
            "seen": 0,
            "attributes": {**attributes, "state": "READY_FOR_REVIEW"},
        }
        return resource("reviewSubmissions", sid, state.submissions[sid]["attributes"])  # fmt: skip

    @app.post("/v1/reviewSubmissionItems")
    async def new_item(request: Request) -> Any:
        _, relations = body_of((await request.json())["data"])
        sid = relations["reviewSubmission"]["data"]["id"]
        vid = relations["appStoreVersion"]["data"]["id"]
        if problem := not_ready(state, store, vid):
            return error(409, problem)
        state.submissions[sid]["items"].append(vid)
        return resource("reviewSubmissionItems", new_id(), {})

    @app.patch("/v1/reviewSubmissions/{sid}")
    async def submitted(sid: str, request: Request) -> Any:
        attributes, _ = body_of((await request.json())["data"])
        found = state.submissions[sid]
        if attributes.get("submitted") and found["items"]:
            found["attributes"]["state"] = "WAITING_FOR_REVIEW"
            for vid in found["items"]:
                state.versions[vid]["attributes"]["appVersionState"] = "WAITING_FOR_REVIEW"
        return resource("reviewSubmissions", sid, found["attributes"])

    @app.get("/v1/reviewSubmissions/{sid}")
    async def review(sid: str) -> Any:
        found = state.submissions[sid]
        found["seen"] += 1
        if found["seen"] >= 2 and found["attributes"]["state"] == "WAITING_FOR_REVIEW":
            found["attributes"]["state"] = "COMPLETE"
            for vid in found["items"]:
                state.versions[vid]["attributes"]["appVersionState"] = state.review_outcome
        return resource("reviewSubmissions", sid, found["attributes"])

    @app.post("/v1/appStoreVersionReleaseRequests")
    async def release_it(request: Request) -> Any:
        _, relations = body_of((await request.json())["data"])
        vid = relations["appStoreVersion"]["data"]["id"]
        if state.versions[vid]["attributes"]["appVersionState"] != "PENDING_DEVELOPER_RELEASE":
            return error(409, "Only an approved version can be released.")
        state.versions[vid]["attributes"]["appVersionState"] = "PROCESSING_FOR_DISTRIBUTION"
        state.released.append(vid)
        return resource("appStoreVersionReleaseRequests", new_id(), {})


def not_ready(state: SubmitState, store: StoreState, vid: str) -> str:
    """What a version still lacks for App Review ("" when nothing)."""
    version = state.versions[vid]
    texts = [store.localizations[lid]["attributes"] for lid in version["localizations"]]
    info = state.infos.get(version["app"], {})
    terms = state.app_terms.get(version["app"], {})
    shots = [p for p in store.placements.values() if p["localization"] in version["localizations"]]
    needs = {
        "a build": version["build"] is not None,
        "a description and support URL": any(t.get("description") and t.get("supportUrl")
                                              for t in texts),
        "a privacy policy URL": any(t.get("privacyPolicyUrl")
                                    for t in info.get("localizations", {}).values()),
        "a category": bool(info.get("categories")), "an age rating": bool(info.get("age")),
        "content rights": "contentRightsDeclaration" in terms, "a price": "price" in terms,
        "availability": "territories" in terms, "a review contact": version["review"] is not None,
        "screenshots": bool(shots),
    }  # fmt: skip
    missing = [what for what, there in needs.items() if not there]
    return f"The version still needs {', '.join(missing)}." if missing else ""
