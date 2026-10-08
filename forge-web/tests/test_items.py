"""Items from a sandbox are rebuilt from checked fields; the rest is dropped."""

from forge_web.chats.items import check_item


def test_ready_keeps_only_well_formed_commands() -> None:
    item = {
        "type": "ready",
        "session_id": "s" * 100,
        "commands": [
            {"name": "/plan", "usage": "/plan", "help": "show the plan"},
            {"name": "/review", "usage": "/review [arguments]", "help": "x" * 1000, "custom": True},
            {"name": "/Bad Name", "help": "spaces and capitals"},
            {"name": "/x", "usage": "<script>", "help": 3},
            "not a command",
        ],
        "extra": "dropped",
    }
    checked = check_item(item)
    assert checked is not None and set(checked) == {"type", "session_id", "commands"}
    assert len(checked["session_id"]) == 64
    names = [c["name"] for c in checked["commands"]]
    assert names == ["/plan", "/review", "/x"]
    review = checked["commands"][1]
    assert review["custom"] is True and len(review["help"]) == 300
    assert checked["commands"][2] == {"name": "/x", "usage": "/x", "help": "3", "custom": False}


def test_ready_without_commands_and_too_many_commands() -> None:
    assert check_item({"type": "ready", "session_id": "s"})["commands"] == []  # type: ignore[index]
    many = [{"name": f"/c{i}"} for i in range(500)]
    checked = check_item({"type": "ready", "commands": many})
    assert checked is not None and len(checked["commands"]) == 200
