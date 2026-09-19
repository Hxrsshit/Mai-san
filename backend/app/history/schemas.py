"""API and service schemas for history import."""

import uuid
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from app.history.models import ImportFormat, ImportStatus


class ImportSourceRead(BaseModel):
    """A candidate export file. Metadata only.

    No content, no path, no preview. The directory is server configuration and
    the caller has no business learning where it is.
    """

    filename: str
    size_bytes: int
    modified_at: datetime


class ImportSourceList(BaseModel):
    sources: List[ImportSourceRead]
    total: int


class ImportRequest(BaseModel):
    """Start an import of one named file in the import directory."""

    filename: str = Field(min_length=1, max_length=255)


class ImportRunRead(BaseModel):
    """The state of one import run."""

    id: uuid.UUID
    source_filename: str
    #: The first 12 characters of the content hash. Enough to tell two
    #: archives apart in a UI, and not a value worth echoing in full.
    source_fingerprint: str
    source_bytes: int
    import_format: ImportFormat
    status: ImportStatus
    conversations_imported: int
    messages_imported: int
    conversations_skipped: int
    messages_skipped: int
    #: How many credential shapes were masked on the way in. A count only --
    #: never the values, never where they were.
    redactions: int
    memories_derived: int
    error_code: Optional[str] = None
    #: True when this run found the archive already imported and did nothing.
    already_imported: bool = False
    created_at: datetime
    updated_at: datetime


class ImportRunList(BaseModel):
    runs: List[ImportRunRead]
    total: int


__all__ = [
    "ImportRequest",
    "ImportRunList",
    "ImportRunRead",
    "ImportSourceList",
    "ImportSourceRead",
]
