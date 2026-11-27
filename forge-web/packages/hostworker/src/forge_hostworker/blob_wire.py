"""What the server and a storage node send each other about blobs (W66; the server may import
this module).

A storage node keeps sealed files for Forge Web (a project's snapshots): Forge Web encrypts
them, the node only stores bytes. A `blob_put` job makes the node fetch the blob with
`GET /api/host/blob/<id>` (only a blob being handed to this node), check its size and SHA-256
and keep it at `<data>/blobs/<owner>/<id>`; `blob_get` sends it back with
`PUT /api/host/blob/<id>` (only while Forge Web asked for it); `blob_delete` removes it. The
jobs run only on a host with the `storage` role.
"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

BlobKind = Literal["blob_put", "blob_get", "blob_delete"]
BLOB_KINDS: tuple[str, ...] = ("blob_put", "blob_get", "blob_delete")
BLOB_APP = "blobs"  # the jobs' app: a node's blob jobs run one after the other
BLOB_ID = r"^[0-9a-f]{32}$"
OWNER = r"^[a-z0-9][a-z0-9-]{0,63}$"  # whose blob it is (a project id): a folder name
MAX_BLOB = 50 * 1024**3


class BlobRef(BaseModel):
    """One blob: its id, whose it is, and its size and SHA-256 (of the sealed bytes)."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=BLOB_ID)
    owner: str = Field(pattern=OWNER)
    size: int = Field(ge=0, le=MAX_BLOB)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def blob_path(root: Path, blob: BlobRef) -> Path:
    """Where a node keeps a blob (both names are checked by the model's patterns)."""
    return root / blob.owner / blob.id
