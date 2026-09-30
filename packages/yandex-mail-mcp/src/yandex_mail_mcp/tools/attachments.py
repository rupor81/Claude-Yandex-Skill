"""``mail_attachments_list`` and ``mail_attachment_download``.

No protocol library here. Listing transfers no content; downloading writes one
file into one directory and nowhere else.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, Field

from yandex_core.errors import ProtocolError

from ..client.imap_client import IMAPMailClient

__all__ = [
    "DEFAULT_DIRECTORY",
    "DOWNLOAD_TOOL",
    "LIST_TOOL",
    "build_mail_attachment_download",
    "build_mail_attachments_list",
]

LIST_TOOL = "mail_attachments_list"
DOWNLOAD_TOOL = "mail_attachment_download"
DEFAULT_DIRECTORY = "~/Downloads/Yandex Mail"

ClientProvider = Callable[[], Awaitable[IMAPMailClient]]


class Attachment(BaseModel):
    part: str = Field(description="Part id; pass it to `mail_attachment_download`.")
    filename: str = Field(description="File name as the sender gave it, decoded.")
    mime_type: str = Field(description="MIME type, e.g. application/pdf.")
    size_bytes: int | None = Field(
        description="Approximate size of the file; exact size is returned on download."
    )


class AttachmentList(BaseModel):
    uid: int
    folder: str
    subject: str
    attachments: list[Attachment] = Field(
        description=(
            "Files the message carries. Inline images such as signature logos are "
            "not included, matching `has_attachments` in `mail_messages_list`."
        )
    )


class Download(BaseModel):
    path: str = Field(description="Where the file was written.")
    bytes: int = Field(description="Exact number of bytes written.")
    mime_type: str
    filename: str


def build_mail_attachments_list(
    client_provider: ClientProvider,
) -> Callable[..., Awaitable[AttachmentList]]:
    async def mail_attachments_list(
        uid: Annotated[int, Field(ge=1, description="UID from `mail_messages_list`.")],
        folder: Annotated[
            str, Field(default="INBOX", description="The folder the UID came from.")
        ] = "INBOX",
    ) -> AttachmentList:
        """List a message's attachments -- name, type, size -- without downloading them."""
        folder_name = (folder or "").strip() or "INBOX"
        client = await client_provider()
        header, parts = await client.list_attachments(folder=folder_name, uid=uid)
        return AttachmentList(
            uid=uid,
            folder=folder_name,
            subject=header.subject,
            attachments=[
                Attachment(
                    part=p.section,
                    filename=p.filename,
                    mime_type=p.mime_type,
                    size_bytes=_decoded_size(p.encoded_size, p.encoding),
                )
                for p in parts
            ],
        )

    mail_attachments_list.__name__ = LIST_TOOL
    return mail_attachments_list


def build_mail_attachment_download(
    client_provider: ClientProvider,
) -> Callable[..., Awaitable[Download]]:
    async def mail_attachment_download(
        uid: Annotated[int, Field(ge=1, description="UID from `mail_messages_list`.")],
        part: Annotated[
            str, Field(description="Part id from `mail_attachments_list`.")
        ],
        folder: Annotated[
            str, Field(default="INBOX", description="The folder the UID came from.")
        ] = "INBOX",
        directory: Annotated[
            str,
            Field(
                default=DEFAULT_DIRECTORY,
                description=f"Local folder to save into (default {DEFAULT_DIRECTORY}).",
            ),
        ] = DEFAULT_DIRECTORY,
        filename: Annotated[
            str | None,
            Field(
                default=None,
                description="Name to save as; the attachment's own name by default.",
            ),
        ] = None,
        overwrite: Annotated[
            bool,
            Field(
                default=False, description="Replace a file that already exists there."
            ),
        ] = False,
    ) -> Download:
        """Save one attachment to a local folder; returns the path and byte count.

        An existing file is never replaced unless `overwrite` is true.
        """
        target_dir = Path(directory or DEFAULT_DIRECTORY).expanduser().resolve()
        if filename is not None:
            _checked_name(filename)  # refused before anything is fetched
        client = await client_provider()
        found, data = await client.fetch_attachment(
            folder=(folder or "").strip() or "INBOX", uid=uid, section=str(part)
        )
        name = (
            _checked_name(filename)
            if filename is not None
            else _safe_name(found.filename)
        )
        target = (target_dir / name).resolve()
        if target.parent != target_dir:
            raise ProtocolError(f"{name!r} would be written outside {target_dir}.")
        if target.exists() and not overwrite:
            raise ProtocolError(
                f"{target} already exists and was left as it is. Pass `overwrite: true` "
                "to replace it, or a different `filename`."
            )
        target_dir.mkdir(parents=True, exist_ok=True)
        # Written beside the target, then renamed: an interruption never leaves a
        # truncated file under the real name.
        fd, temp = tempfile.mkstemp(dir=target_dir, prefix=".part-")
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(data)
            Path(temp).replace(target)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise
        return Download(
            path=str(target), bytes=len(data), mime_type=found.mime_type, filename=name
        )

    mail_attachment_download.__name__ = DOWNLOAD_TOOL
    return mail_attachment_download


def _checked_name(name: str) -> str:
    """A caller-given name must be a plain file name, or it is refused."""
    bad = (
        not name
        or name in {".", ".."}
        or name.startswith(".")
        or "/" in name
        or "\\" in name
        or any(ord(c) < 32 for c in name)
    )
    if bad:
        raise ProtocolError(
            f"`filename` must be a plain file name, not a path: {name!r}."
        )
    return name


def _safe_name(name: str) -> str:
    """A sender-given name, reduced to something that can only land in the folder."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = "".join(c for c in base if ord(c) >= 32).strip().lstrip(".")
    return base or "attachment"


def _decoded_size(encoded: int | None, encoding: str) -> int | None:
    if encoded is None:
        return None
    # Measured: the server wraps base64 at 76 characters plus CRLF, and counts the
    # line breaks in the part's size -- 78 bytes on the wire carry 57 of file.
    return encoded * 57 // 78 if encoding == "base64" else encoded
