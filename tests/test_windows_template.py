"""The Windows app of a full-stack product (S72, S73): WinUI 3 on .NET 10, downloaded from the
product's website as an installer or a ZIP that Velopack makes and updates, with a client of the
server's API that Kiota generates from its snapshot on every build, sign-in and account deletion
in the app; it sells on the website, not in a store. Built and packed for real on Windows under
the `fullstack` marker."""

import json
import os
import re
import struct
from pathlib import Path
from xml.etree import ElementTree

from forge.app_template import app_files
from forge.cli import main
from forge.windows_template import ICON_SIZES, namespace_of

XML = (".csproj", ".slnx", ".props", ".xaml", ".manifest", ".pubxml")


def windows(*features: str) -> dict[str, bytes]:
    """The `windows/` folder of a product, by its path in that folder."""
    files = app_files("tally-notes", "Tally Notes", features=["windows", *features],
                      apps_domain="apps.forge.test")  # fmt: skip
    return {p.removeprefix("windows/"): d for p, d in files.items() if p.startswith("windows/")}


def png_size(data: bytes) -> tuple[int, int]:
    assert data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def icon_sizes(data: bytes) -> list[tuple[int, int]]:
    """The sizes of the PNGs in an ICO, checked against its directory."""
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1)
    sizes = []
    for index in range(count):
        width, height, _, _, planes, bits, length, offset = struct.unpack(
            "<BBBBHHII", data[6 + 16 * index : 22 + 16 * index]
        )
        found = png_size(data[offset : offset + length])
        assert found == (width or 256, height or 256) and (planes, bits) == (1, 24)
        sizes.append(found)
    return sizes


def test_every_placeholder_is_filled_and_every_file_parses() -> None:
    files = windows()
    for path, data in files.items():
        if path.endswith(".ico"):
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
    tools = json.loads(windows()[".config/dotnet-tools.json"])["tools"]
    velopack = {p.get("Include"): p.get("Version") for p in project.iter("PackageReference")}
    assert tools["vpk"]["version"] == velopack["Velopack"] == "1.2.161"  # the two must match


def test_the_app_is_downloaded_from_the_website_not_from_a_store() -> None:
    files = windows()
    project = ElementTree.fromstring(files["App/App.csproj"])
    assert project.findtext("PropertyGroup/WindowsPackageType") == "None"  # no MSIX
    assert project.findtext("PropertyGroup/WindowsAppSDKSelfContained") == "true"
    assert project.findtext("PropertyGroup/SelfContained") == "true"  # runs without installs
    assert project.findtext("PropertyGroup/ApplicationIcon") == "App.ico"
    assert not [path for path in files if path.endswith(".appxmanifest") or "Store" in path]
    assert icon_sizes(files["App/App.ico"]) == [(size, size) for size in ICON_SIZES]
    server = files["Core/Server.cs"].decode()
    assert 'Downloads = new("https://tally-notes.apps.forge.test/downloads/windows")' in server


def test_velopack_starts_first_and_updates_come_from_the_website() -> None:
    files = windows()
    assert "DISABLE_XAML_GENERATED_MAIN" in files["App/App.csproj"].decode()
    program = files["App/Program.cs"].decode()
    main = program[program.index("static void Main") :]
    assert main.index("VelopackApp.Build().Run();") < main.index("Application.Start")
    updates = files["App/Updates.cs"].decode()
    assert "new(Server.Downloads.ToString())" in updates
    assert "manager.IsInstalled" in updates and "ApplyUpdatesAndRestart(found)" in updates
    window = files["App/MainWindow.xaml.cs"].decode()
    assert "updates.CheckAsync()" in window and "updates.ApplyAsync()" in window
    assert 'x:Name="UpdateButton"' in files["App/MainWindow.xaml"].decode()
    ignored = files[".gitignore"].decode()
    assert "publish/" in ignored and "releases/" in ignored


def test_a_paid_app_sells_on_the_website() -> None:
    plain = windows()
    assert "Pricing" not in plain["Core/Server.cs"].decode()
    for paid in (windows("payments"), windows("payments", "digital_goods")):
        server = paid["Core/Server.cs"].decode()
        assert 'Pricing = new("https://tally-notes.apps.forge.test/pricing")' in server
        assert "Launcher.LaunchUriAsync(Server.Pricing)" in paid["App/MainWindow.xaml.cs"].decode()
        assert "--exclude-path" not in paid["Core/Core.csproj"].decode()
        assert "StoreContext" not in b"".join(paid.values()).decode("utf-8", "replace")


def test_the_website_links_the_downloads() -> None:
    layout = "web/src/components/Layout.tsx"
    with_windows = app_files("tally-notes", features=["windows"])[layout]
    assert b'<a href="/downloads">Download for Windows</a>' in with_windows
    assert b'href="/downloads"' not in app_files("tally-notes")[layout]


def test_names_become_namespaces() -> None:
    assert namespace_of("Tally Notes") == "TallyNotes"
    assert namespace_of("9 lives") == "App9Lives"
    assert namespace_of("shop-app") == "ShopApp"


def test_forge_app_new_windows(tmp_path: Path) -> None:
    assert main(["-C", str(tmp_path), "app", "new", "shop", "--windows"]) == 0
    shop = tmp_path / "shop"
    assert 'clients = ["web", "windows"]' in (shop / "forge.app.toml").read_text(encoding="utf-8")
    assert (shop / "windows" / "App" / "App.csproj").is_file()
    assert (shop / "windows" / "App" / "App.ico").read_bytes()[:4] == b"\x00\x00\x01\x00"
    if os.name != "nt":
        assert (shop / "windows" / "App.slnx").read_bytes().count(b"\r\n") == 0
