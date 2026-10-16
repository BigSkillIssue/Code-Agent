"""The Windows app of a full-stack product (S72): WinUI 3 on .NET 10, packaged as MSIX, with a
client of the server's API that Kiota generates from its snapshot on every build, sign-in and
account deletion in the app, and Microsoft Store purchases for digital goods. Built for real on
Windows under the `fullstack` marker."""

import json
import os
import re
import struct
from pathlib import Path
from xml.etree import ElementTree

from forge.app_template import app_files
from forge.cli import main
from forge.windows_template import LOGOS, namespace_of

XML = (".csproj", ".slnx", ".props", ".xaml", ".appxmanifest", ".manifest", ".pubxml")


def windows(*features: str) -> dict[str, bytes]:
    """The `windows/` folder of a product, by its path in that folder."""
    files = app_files("tally-notes", "Tally Notes", features=["windows", *features],
                      apps_domain="apps.forge.test")  # fmt: skip
    return {p.removeprefix("windows/"): d for p, d in files.items() if p.startswith("windows/")}


def png_size(data: bytes) -> tuple[int, int]:
    assert data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def test_every_placeholder_is_filled_and_every_file_parses() -> None:
    files = windows()
    for path, data in files.items():
        if path.endswith(".png"):
            continue
        text = data.decode()
        assert "@@" not in text, path
        assert "\r\n" not in text, path
        if path.endswith(XML):
            ElementTree.fromstring(text)
        elif path.endswith(".json"):
            json.loads(text)
        elif path.endswith(".cs"):
            assert text.count("{") == text.count("}"), path
            assert "namespace TallyNotes" in text, path
    assert {"App.slnx", "App/App.csproj", "Core/Core.csproj", "Tests/Tests.csproj"} <= set(files)
    assert b"<AssemblyName>TallyNotes</AssemblyName>" in files["App/App.csproj"]


def test_the_client_is_generated_from_the_snapshot_and_points_at_staging() -> None:
    core = windows()["Core/Core.csproj"].decode()
    assert "../../server/openapi.json" in core and "dotnet tool run kiota" in core
    assert "<KiotaArgs>generate --language CSharp" in core
    assert "--exclude-path" not in core
    tools = json.loads(windows()[".config/dotnet-tools.json"])
    assert tools["tools"]["microsoft.openapi.kiota"]["version"] == "1.35.0"
    server = windows()["Core/Server.cs"].decode()
    assert 'new("https://tally-notes.staging.apps.forge.test")' in server
    assert 'new("https://tally-notes.apps.forge.test")' in server
    assert b"Core/Api/" in windows()[".gitignore"]  # generated on every build, never committed


def test_signing_in_and_deleting_the_account_work_in_the_app() -> None:
    account = windows()["Core/Account.cs"].decode()
    assert "client.Api.Auth.Login.PostAsync(new SignIn" in account
    assert "client.Api.Account.DeleteAsync(new DeleteAccount" in account
    window = windows()["App/MainWindow.xaml.cs"].decode()
    assert "Account.SignInAsync" in window and "Account.DeleteAsync" in window
    assert 'Content="Delete my account"' in windows()["App/MainWindow.xaml"].decode()
    tests = windows()["Tests/AccountTests.cs"].decode()
    assert "/api/auth/login" in tests and "/api/account" in tests


def test_every_version_is_pinned() -> None:
    for path, data in windows().items():
        if path.endswith(".csproj"):
            for found in ElementTree.fromstring(data).iter("PackageReference"):
                version = found.get("Version", "")
                assert re.fullmatch(r"\d+(\.\d+){1,3}", version), f"{path}: {found.attrib}"
    project = ElementTree.fromstring(windows()["App/App.csproj"])
    assert project.findtext("PropertyGroup/TargetFramework") == "net10.0-windows10.0.19041.0"
    assert project.findtext("PropertyGroup/UseWinUI") == "true"
    assert project.findtext("PropertyGroup/EnableMsixTooling") == "true"


def test_the_msix_names_the_app_and_has_its_logos() -> None:
    files = windows()
    manifest = files["App/Package.appxmanifest"].decode()
    assert 'Name="TallyNotes"' in manifest and "<DisplayName>Tally Notes</DisplayName>" in manifest
    assert '<rescap:Capability Name="runFullTrust" />' in manifest
    assert '<Capability Name="internetClient" />' in manifest
    for path, size in LOGOS.items():
        assert png_size(files[f"App/{path}"]) == size, path
        assert path.replace("/", "\\") in manifest or "scale-200" in path or "targetsize" in path


def test_digital_goods_are_sold_in_the_microsoft_store_without_a_web_checkout() -> None:
    plain = windows()
    assert "App/Store.cs" not in plain
    paid = windows("payments", "digital_goods")
    store = paid["App/Store.cs"].decode()
    assert "StoreContext.GetDefault()" in store and "RequestPurchaseAsync" in store
    assert "InitializeWithWindow.Initialize" in store  # a desktop app names its window
    assert "--exclude-path &quot;/api/payments/**&quot;" in paid["Core/Core.csproj"].decode()


def test_names_become_namespaces() -> None:
    assert namespace_of("Tally Notes") == "TallyNotes"
    assert namespace_of("9 lives") == "App9Lives"
    assert namespace_of("shop-app") == "ShopApp"


def test_forge_app_new_windows(tmp_path: Path) -> None:
    assert main(["-C", str(tmp_path), "app", "new", "shop", "--windows"]) == 0
    shop = tmp_path / "shop"
    assert 'clients = ["web", "windows"]' in (shop / "forge.app.toml").read_text(encoding="utf-8")
    assert (shop / "windows" / "App" / "App.csproj").is_file()
    assert (shop / "windows" / "App" / "Assets" / "StoreLogo.png").read_bytes()[:4] == b"\x89PNG"
    if os.name != "nt":
        assert (shop / "windows" / "App.slnx").read_bytes().count(b"\r\n") == 0
