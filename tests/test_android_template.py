"""The Android app of a full-stack product (S71): Kotlin and Jetpack Compose with a client of the
server's API generated from its snapshot, account deletion in the app, Play Billing for digital
goods, a Gradle wrapper and pinned versions. Built for real under the `fullstack` marker."""

import hashlib
import os
import re
import tomllib
from pathlib import Path
from xml.etree import ElementTree

from forge.android_template import WRAPPER_SHA256, package_of, placed
from forge.app_template import app_files
from forge.cli import main


def android(*features: str) -> dict[str, bytes]:
    """The `android/` folder of a product, by its path in that folder."""
    files = app_files("tally-notes", "Tally Notes", features=["android", *features],
                      apps_domain="apps.forge.test")  # fmt: skip
    return {p.removeprefix("android/"): d for p, d in files.items() if p.startswith("android/")}


def test_every_placeholder_is_filled_and_every_file_parses() -> None:
    files = android()
    for path, data in files.items():
        if path.endswith(".jar"):
            continue
        text = data.decode()
        assert "@@" not in text, path
        if path.endswith(".toml"):
            tomllib.loads(text)
        elif path.endswith(".xml"):
            ElementTree.fromstring(text)
        elif path.endswith((".kt", ".kts")):
            assert text.count("{") == text.count("}"), path
    package = "com.example.tallynotes"
    assert "app/src/main/java/com/example/tallynotes/MainActivity.kt" in files
    assert files["app/src/main/java/com/example/tallynotes/Server.kt"].startswith(
        f"package {package}\n".encode()
    )
    assert (
        b'<string name="app_name">Tally Notes</string>'
        in files["app/src/main/res/values/strings.xml"]
    )
    assert b'rootProject.name = "tally-notes"' in files["settings.gradle.kts"]


def test_the_client_is_generated_from_the_snapshot_and_points_at_staging() -> None:
    build = android()["app/build.gradle.kts"].decode()
    assert 'rootProject.file("../server/openapi.json")' in build
    assert 'generatorName.set("kotlin")' in build and "dependsOn(generateApi)" in build
    assert '"API_STAGING", "\\"https://tally-notes.staging.apps.forge.test\\""' in build
    assert '"API_PRODUCTION", "\\"https://tally-notes.apps.forge.test\\""' in build
    server = android()["app/src/main/java/com/example/tallynotes/Server.kt"].decode()
    assert "CookieJar" in server
    # debug builds talk to staging, the release build (Google Play's) to production
    wanted = "if (BuildConfig.DEBUG) BuildConfig.API_STAGING else BuildConfig.API_PRODUCTION"
    assert wanted in server


def test_signing_in_and_deleting_the_account_work_in_the_app() -> None:
    screen = android()["app/src/main/java/com/example/tallynotes/MainActivity.kt"].decode()
    assert "deleteAccountApiAccountDelete(DeleteAccount(password))" in screen
    assert "signInApiAuthLoginPost(SignIn(email, password))" in screen
    assert 'Text("Delete my account")' in screen
    manifest = android()["app/src/main/AndroidManifest.xml"].decode()
    assert "android.permission.INTERNET" in manifest


def test_every_version_is_pinned_and_the_wrapper_is_gradles_own() -> None:
    files = android()
    catalog = tomllib.loads(files["gradle/libs.versions.toml"].decode())
    for name, version in catalog["versions"].items():
        assert re.fullmatch(r"\d+\.\d+(\.\d+)?", version), f"{name} is not pinned: {version}"
    for name, library in catalog["libraries"].items():
        pinned = "ref" in library.get("version", {})
        assert pinned or name.startswith("compose-"), name  # Compose's come with its BOM
    wrapper = files["gradle/wrapper/gradle-wrapper.properties"].decode()
    assert "gradle-8.11.1-bin.zip" in wrapper and "distributionSha256Sum=" in wrapper
    assert hashlib.sha256(files["gradle/wrapper/gradle-wrapper.jar"]).hexdigest() == WRAPPER_SHA256
    assert files["gradlew"].startswith(b"#!/bin/sh")
    assert b"\r\n" in files["gradlew.bat"]  # Windows needs its line ends, .gitattributes keeps them
    assert b"gradlew.bat text eol=crlf" in files[".gitattributes"]
    assert all(b"\r\n" not in data for path, data in files.items()
               if not path.endswith((".bat", ".jar")))  # fmt: skip


def test_digital_goods_switch_the_android_app_to_play_billing_without_a_web_checkout() -> None:
    plain = android()
    assert not any(path.endswith("Store.kt") for path in plain)
    assert b"billing" not in plain["app/build.gradle.kts"]
    paid = android("payments", "digital_goods")
    store = paid["app/src/main/java/com/example/tallynotes/Store.kt"].decode()
    assert "BillingClient.newBuilder" in store and "acknowledgePurchase" in store
    build = paid["app/build.gradle.kts"].decode()
    assert "implementation(libs.billing)" in build
    assert 'mapOf("FILTER" to "tag:account|auth|moderation")' in build  # no payments operations


def test_names_become_packages() -> None:
    assert package_of("tally-notes") == "com.example.tallynotes"
    assert package_of("9lives") == "com.example.app9lives"
    assert placed("app/src/main/java/app/Store.kt", "com.example.x") == (
        "app/src/main/java/com/example/x/Store.kt"
    )
    assert placed("app/build.gradle.kts", "com.example.x") == "app/build.gradle.kts"


def test_forge_app_new_android(tmp_path: Path) -> None:
    assert main(["-C", str(tmp_path), "app", "new", "shop", "--android"]) == 0
    shop = tmp_path / "shop"
    assert 'clients = ["web", "android"]' in (shop / "forge.app.toml").read_text(encoding="utf-8")
    gradlew = shop / "android" / "gradlew"
    assert gradlew.is_file()
    if os.name != "nt":  # Windows keeps no executable bit
        assert gradlew.stat().st_mode & 0o100
