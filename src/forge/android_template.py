"""The Android app of a full-stack product (S71): Kotlin and Jetpack Compose from
`templates/android/`, with a client of the server's API that openapi-generator writes from its
snapshot on every build, sign-in and account deletion in the app, Google Play Billing when the app
sells digital goods, a Gradle wrapper and pinned versions. It builds on Linux.

The template's text files are rendered like the full-stack template's; the wrapper's jar is
copied as it is (Gradle's own, its checksum in WRAPPER_SHA256).
"""

import hashlib
import re
from importlib import resources

KIND = "android"
SOURCES = ("app/src/main/java/app/", "app/src/test/java/app/")  # moved to the package's folder
WRAPPER = "gradle/wrapper/gradle-wrapper.jar"
WRAPPER_SHA256 = "7d3a4ac4de1c32b59bc6a4eb8ecb8e612ccd0cf1ae1e99f66902da64df296172"  # 8.14.3
WEB_TAGS = ("payments",)  # sold through Google Play instead when the app sells digital goods


def package_of(name: str) -> str:
    """The app's package and application id: tally-notes → com.example.tallynotes."""
    part = re.sub(r"[^a-z0-9]", "", name.lower())
    return f"com.example.{part if part[:1].isalpha() else 'app' + part}"


def placed(path: str, package: str) -> str:
    """A template path with the Kotlin sources in their package's folder."""
    for prefix in SOURCES:
        if path.startswith(prefix):
            return f"{prefix.removesuffix('app/')}{package.replace('.', '/')}/{path[len(prefix) :]}"
    return path


def app_tags(api_tags: list[str]) -> str:
    """The API's tags the app's client keeps when it sells digital goods (`a|b|c`)."""
    return "|".join(sorted(t for t in api_tags if t not in WEB_TAGS))


def wrapper_jar() -> bytes:
    """Gradle's wrapper jar, checked against its published checksum."""
    data = resources.files("forge").joinpath("templates", KIND, WRAPPER).read_bytes()
    if hashlib.sha256(data).hexdigest() != WRAPPER_SHA256:
        raise ValueError("the Gradle wrapper in Forge's templates is not Gradle's own")
    return data
