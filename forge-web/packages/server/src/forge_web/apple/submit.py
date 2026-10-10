"""Getting an App Store version ready for review, and submitting it (W22d2).

Everything goes through App Store Connect's API with the user's key: the version with the build
from a release to TestFlight, the texts the user saved (W22c), app information, categories, age
rating, content rights, a free price, availability in every territory, the contact for App
Review, and the store screenshots (W22d1) placed on the version. Submitting is a separate call,
made only after the user confirmed it; releasing an approved version is a third one.
"""

from typing import Any

from forge.apple_listing import StoreListing

from forge_web.apple.asc_client import AscClient, AscError

PLATFORMS = {"ios": "IOS", "macos": "MAC_OS"}
EDITABLE = ("PREPARE_FOR_SUBMISSION", "DEVELOPER_REJECTED", "REJECTED", "METADATA_REJECTED",
            "INVALID_BINARY")  # fmt: skip
NO_THIRD_PARTY = "DOES_NOT_USE_THIRD_PARTY_CONTENT"
TERRITORY = "USA"  # the base territory of the price schedule


class SubmitProblem(Exception):
    """App Store Connect is not ready for this step; with what the user can do."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


def camel(name: str) -> str:
    """Forge's snake_case answer names as App Store Connect spells them."""
    head, *rest = name.split("_")
    return head + "".join(part.capitalize() for part in rest)


def state_of(item: dict[str, Any]) -> str:
    attributes = item.get("attributes", {})
    return str(attributes.get("appVersionState") or attributes.get("appStoreState") or "")


async def version(asc: AscClient, app_id: str, platform: str, version_string: str) -> str:
    """The app's version in preparation for this platform, made when there is none."""
    found = await asc.all(f"/v1/apps/{app_id}/appStoreVersions",
                          **{"filter[platform]": PLATFORMS[platform]})  # fmt: skip
    for item in found:
        if state_of(item) in EDITABLE:
            if item["attributes"].get("versionString") != version_string:
                await asc.patch(f"/v1/appStoreVersions/{item['id']}", {"data": {
                    "type": "appStoreVersions", "id": item["id"],
                    "attributes": {"versionString": version_string}}})  # fmt: skip
            return str(item["id"])
    body = {
        "data": {
            "type": "appStoreVersions",
            "attributes": {
                "platform": PLATFORMS[platform],
                "versionString": version_string,
                "releaseType": "MANUAL",
            },
            "relationships": {"app": {"data": {"type": "apps", "id": app_id}}},
        }
    }
    return str((await asc.post("/v1/appStoreVersions", body))["data"]["id"])  # fmt: skip


async def first_version(asc: AscClient, app_id: str, platform: str) -> bool:
    """Whether no version of this platform was ever on the App Store (then no "What's new")."""
    found = await asc.all(f"/v1/apps/{app_id}/appStoreVersions",
                          **{"filter[platform]": PLATFORMS[platform]})  # fmt: skip
    return all(state_of(item) in EDITABLE for item in found)


async def set_version(
    asc: AscClient, version_id: str, build_id: str, listing: StoreListing
) -> None:
    """The version's build, copyright and a release by hand (the user's second click)."""
    await asc.patch(
        f"/v1/appStoreVersions/{version_id}",
        {
            "data": {
                "type": "appStoreVersions",
                "id": version_id,
                "attributes": {"copyright": listing.copyright, "releaseType": "MANUAL"},
            }
        },
    )
    await asc.patch(f"/v1/appStoreVersions/{version_id}/relationships/build",
                    {"data": {"type": "builds", "id": build_id}})  # fmt: skip


async def localization(asc: AscClient, version_id: str, listing: StoreListing, first: bool) -> str:
    """The version's texts in the listing's language (made when missing); its id."""
    attributes = {
        "description": listing.description,
        "keywords": listing.keywords,
        "promotionalText": listing.promotional_text or None,
        "supportUrl": listing.support_url,
        "marketingUrl": listing.marketing_url or None,
    }
    if not first and listing.whats_new:  # Apple refuses "What's new" for a first version
        attributes["whatsNew"] = listing.whats_new
    found = await asc.all(f"/v1/appStoreVersions/{version_id}/appStoreVersionLocalizations")
    for item in found:
        if item["attributes"].get("locale") == listing.locale:
            await asc.patch(
                f"/v1/appStoreVersionLocalizations/{item['id']}",
                {
                    "data": {
                        "type": "appStoreVersionLocalizations",
                        "id": item["id"],
                        "attributes": attributes,
                    }
                },
            )
            return str(item["id"])
    body = {
        "data": {
            "type": "appStoreVersionLocalizations",
            "attributes": {"locale": listing.locale, **attributes},
            "relationships": {
                "appStoreVersion": {"data": {"type": "appStoreVersions", "id": version_id}}
            },
        }
    }
    made = await asc.post("/v1/appStoreVersionLocalizations", body)
    return str(made["data"]["id"])


async def app_info(asc: AscClient, app_id: str, listing: StoreListing) -> str:
    """Name, subtitle, privacy policy, categories and age rating of the app; the app info's id."""
    infos = await asc.all(f"/v1/apps/{app_id}/appInfos")
    editable = [i for i in infos if state_of(i) not in ("READY_FOR_DISTRIBUTION", "READY_FOR_SALE")]
    if not editable:
        raise SubmitProblem("App Store Connect has no app information that can be changed")
    info_id = str(editable[0]["id"])
    categories = {
        "primaryCategory": {"data": {"type": "appCategories", "id": listing.primary_category}}
    }
    if listing.secondary_category:
        categories["secondaryCategory"] = {
            "data": {"type": "appCategories", "id": listing.secondary_category}
        }
    await asc.patch(
        f"/v1/appInfos/{info_id}",
        {"data": {"type": "appInfos", "id": info_id, "relationships": categories}},
    )
    texts = {
        "name": listing.name,
        "subtitle": listing.subtitle or None,
        "privacyPolicyUrl": listing.privacy_policy_url,
    }
    found = await asc.all(f"/v1/appInfos/{info_id}/appInfoLocalizations")
    match = [i for i in found if i["attributes"].get("locale") == listing.locale]
    if match:
        await asc.patch(
            f"/v1/appInfoLocalizations/{match[0]['id']}",
            {"data": {"type": "appInfoLocalizations", "id": match[0]["id"], "attributes": texts}},
        )
    else:
        await asc.post(
            "/v1/appInfoLocalizations",
            {
                "data": {
                    "type": "appInfoLocalizations",
                    "attributes": {"locale": listing.locale, **texts},
                    "relationships": {"appInfo": {"data": {"type": "appInfos", "id": info_id}}},
                }
            },
        )
    await age_rating(asc, info_id, listing)
    return info_id  # fmt: skip


async def age_rating(asc: AscClient, info_id: str, listing: StoreListing) -> None:
    """The answers to Apple's age rating questions, as the listing gives them."""
    declaration = (await asc.get(f"/v1/appInfos/{info_id}/ageRatingDeclaration"))["data"]
    answers = {camel(k): v for k, v in listing.age_rating.model_dump().items()}
    await asc.patch(
        f"/v1/ageRatingDeclarations/{declaration['id']}",
        {"data": {"type": "ageRatingDeclarations", "id": declaration["id"], "attributes": answers}},
    )


async def app_terms(asc: AscClient, app_id: str) -> None:
    """Content rights (no third-party content), a free price and every territory, where the
    app has none of these yet."""
    await asc.patch(
        f"/v1/apps/{app_id}",
        {
            "data": {
                "type": "apps",
                "id": app_id,
                "attributes": {"contentRightsDeclaration": NO_THIRD_PARTY},
            }
        },
    )
    if not await exists(asc, f"/v1/apps/{app_id}/appPriceSchedule"):
        await free_price(asc, app_id)
    if not await exists(asc, f"/v1/apps/{app_id}/appAvailabilityV2"):
        await everywhere(asc, app_id)  # fmt: skip


async def exists(asc: AscClient, path: str) -> bool:
    try:
        return bool((await asc.get(path)).get("data"))
    except AscError as err:
        if err.status == 404:
            return False
        raise


async def free_price(asc: AscClient, app_id: str) -> None:
    """A price schedule with the free price point."""
    points = await asc.all(f"/v1/apps/{app_id}/appPricePoints",
                           **{"filter[territory]": TERRITORY})  # fmt: skip
    free = [p for p in points if float(p.get("attributes", {}).get("customerPrice") or 1) == 0]
    if not free:
        raise SubmitProblem("App Store Connect offers no free price for this app")
    body = {
        "data": {
            "type": "appPriceSchedules",
            "relationships": {
                "app": {"data": {"type": "apps", "id": app_id}},
                "baseTerritory": {"data": {"type": "territories", "id": TERRITORY}},
                "manualPrices": {"data": [{"type": "appPrices", "id": "${free}"}]},
            },
        },
        "included": [
            {
                "type": "appPrices",
                "id": "${free}",
                "attributes": {"startDate": None},
                "relationships": {
                    "appPricePoint": {"data": {"type": "appPricePoints", "id": free[0]["id"]}}
                },
            }
        ],
    }
    await asc.post("/v1/appPriceSchedules", body)  # fmt: skip


async def everywhere(asc: AscClient, app_id: str) -> None:
    """The app available in every territory, and in new ones as they come."""
    territories = await asc.all("/v1/territories", limit=200)
    refs = [f"${{t{n}}}" for n in range(len(territories))]
    body = {
        "data": {
            "type": "appAvailabilities",
            "attributes": {"availableInNewTerritories": True},
            "relationships": {
                "app": {"data": {"type": "apps", "id": app_id}},
                "territoryAvailabilities": {
                    "data": [{"type": "territoryAvailabilities", "id": r} for r in refs]
                },
            },
        },
        "included": [
            {
                "type": "territoryAvailabilities",
                "id": ref,
                "attributes": {"available": True},
                "relationships": {"territory": {"data": {"type": "territories", "id": t["id"]}}},
            }
            for ref, t in zip(refs, territories, strict=True)
        ],
    }
    await asc.post("/v2/appAvailabilities", body)  # fmt: skip


async def review_contact(
    asc: AscClient, version_id: str, contact: dict[str, str], notes: str
) -> None:
    """Who App Review may contact, and notes for the reviewer."""
    attributes = {
        "contactFirstName": contact["first_name"],
        "contactLastName": contact["last_name"],
        "contactPhone": contact["phone"],
        "contactEmail": contact["email"],
        "demoAccountRequired": False,
        "notes": notes or None,
    }
    try:
        found = (await asc.get(f"/v1/appStoreVersions/{version_id}/appStoreReviewDetail"))["data"]
    except AscError as err:
        if err.status != 404:
            raise
        found = None
    if found:
        await asc.patch(
            f"/v1/appStoreReviewDetails/{found['id']}",
            {
                "data": {
                    "type": "appStoreReviewDetails",
                    "id": found["id"],
                    "attributes": attributes,
                }
            },
        )
        return
    await asc.post("/v1/appStoreReviewDetails", {"data": {
        "type": "appStoreReviewDetails", "attributes": attributes,
        "relationships": {"appStoreVersion": {"data": {"type": "appStoreVersions",
                                                       "id": version_id}}}}})  # fmt: skip


async def clear_screenshots(asc: AscClient, localization_id: str) -> None:
    """Remove the screenshots placed on a localization before (Forge places them anew)."""
    placed = await asc.all(f"/v1/appStoreVersionLocalizations/{localization_id}/placements",
                           **{"filter[placementType]": "APP_SCREENSHOT"})  # fmt: skip
    for placement in placed:
        await asc.delete(f"/v1/appAssetLibraryPlacements/{placement['id']}")


async def submit(asc: AscClient, app_id: str, platform: str, version_id: str) -> str:
    """Submit the version for App Review; the review submission's id."""
    made = await asc.post(
        "/v1/reviewSubmissions",
        {
            "data": {
                "type": "reviewSubmissions",
                "attributes": {"platform": PLATFORMS[platform]},
                "relationships": {"app": {"data": {"type": "apps", "id": app_id}}},
            }
        },
    )
    submission = str(made["data"]["id"])
    await asc.post(
        "/v1/reviewSubmissionItems",
        {
            "data": {
                "type": "reviewSubmissionItems",
                "relationships": {
                    "reviewSubmission": {"data": {"type": "reviewSubmissions", "id": submission}},
                    "appStoreVersion": {"data": {"type": "appStoreVersions", "id": version_id}},
                },
            }
        },
    )
    await asc.patch(
        f"/v1/reviewSubmissions/{submission}",
        {
            "data": {
                "type": "reviewSubmissions",
                "id": submission,
                "attributes": {"submitted": True},
            }
        },
    )
    return submission  # fmt: skip


async def review_state(asc: AscClient, submission_id: str, version_id: str) -> tuple[str, str]:
    """Where App Review is: the submission's state and the version's."""
    submission = (await asc.get(f"/v1/reviewSubmissions/{submission_id}"))["data"]
    found = (await asc.get(f"/v1/appStoreVersions/{version_id}"))["data"]
    return str(submission.get("attributes", {}).get("state", "")), state_of(found)


async def release(asc: AscClient, version_id: str) -> None:
    """Put an approved version on the App Store."""
    await asc.post("/v1/appStoreVersionReleaseRequests", {"data": {
        "type": "appStoreVersionReleaseRequests",
        "relationships": {"appStoreVersion": {"data": {"type": "appStoreVersions",
                                                       "id": version_id}}}}})  # fmt: skip
