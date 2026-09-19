"""Stage 5C: importing ChatGPT history.

Three endpoints, and one shape worth explaining: there is no upload.

An upload endpoint would mean multipart form parsing, which means adding
`python-multipart` and putting Starlette's form parser on a reachable code
path. This application has no form parsing at all today, which is precisely
why the Starlette form-parsing advisory does not apply to it, and a history
importer is a poor reason to give that up. It would also push a
multi-hundred-megabyte export through the ASGI request path.

So the operator bind-mounts a directory, the browser lists what is in it, and
the import runs server-side against a file the server already has. The
directory is configuration; the caller supplies only a filename, which is
resolved by path identity rather than string inspection -- see
`app.history.sources`.

Nothing here returns archive content. `/sources` returns filenames and sizes,
and a run returns counts. A route that could echo an imported message back
would be a way to read the archive through the API, and the archive is exactly
the untrusted historical data that must not travel.
"""

import uuid

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import AppSettings, HistoryImport
from app.core.logging import get_logger
from app.history.schemas import (
    ImportRequest,
    ImportRunList,
    ImportRunRead,
    ImportSourceList,
    ImportSourceRead,
)
from app.history.service import HistoryImportError
from app.history.sources import list_sources

logger = get_logger(__name__)

router = APIRouter(prefix="/api/history", tags=["history"])

#: Reason codes that describe a bad request rather than a server problem.
#: Anything not listed is a 500, so a new failure mode is loud by default.
_CLIENT_ERRORS = frozenset(
    {
        "invalid_filename",
        "outside_import_directory",
        "source_not_found",
        "source_empty",
        "source_too_large",
        "source_unreadable",
        "unsupported_format",
        "zip_unreadable",
        "zip_missing_conversations",
        "zip_too_many_members",
        "uncompressed_too_large",
        "not_utf8",
        "malformed_json",
        "unexpected_document_shape",
    }
)


def _read(archive, already_imported: bool = False) -> ImportRunRead:
    return ImportRunRead(
        id=archive.id,
        source_filename=archive.source_filename,
        source_fingerprint=archive.source_sha256[:12],
        source_bytes=archive.source_bytes,
        import_format=archive.import_format,
        status=archive.status,
        conversations_imported=archive.conversations_imported,
        messages_imported=archive.messages_imported,
        conversations_skipped=archive.conversations_skipped,
        messages_skipped=archive.messages_skipped,
        redactions=archive.redactions,
        memories_derived=archive.memories_derived,
        error_code=archive.error_code,
        already_imported=already_imported,
        created_at=archive.created_at,
        updated_at=archive.updated_at,
    )


@router.get("/sources", response_model=ImportSourceList)
async def get_sources(settings: AppSettings) -> ImportSourceList:
    """Export files available to import. Metadata only, never content."""
    if not settings.HISTORY_IMPORT_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="history_import_disabled"
        )
    sources = list_sources(settings)
    return ImportSourceList(
        sources=[
            ImportSourceRead(
                filename=source.filename,
                size_bytes=source.size_bytes,
                modified_at=source.modified_at,
            )
            for source in sources
        ],
        total=len(sources),
    )


@router.post(
    "/imports", response_model=ImportRunRead, status_code=status.HTTP_201_CREATED
)
async def start_import(
    payload: ImportRequest, service: HistoryImport
) -> ImportRunRead:
    """Import one export file.

    Idempotent by content: importing the same file twice returns the first
    run with `already_imported` set, rather than building a second copy of the
    corpus.

    Conflict evaluation runs inside the import, because it costs no model
    call. Entity and relationship extraction do not -- see
    `HistoryImportService._derive_memories` for why an import does not fire
    hundreds of them.
    """
    try:
        archive, already, _derived = await service.import_file(payload.filename)
    except HistoryImportError as exc:
        if exc.code == "history_import_disabled":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=exc.code
            )
        if exc.code in _CLIENT_ERRORS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=exc.code
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=exc.code
        )

    return _read(archive, already_imported=already)


@router.get("/imports", response_model=ImportRunList)
async def get_runs(
    service: HistoryImport, limit: int = Query(default=50, ge=1, le=200)
) -> ImportRunList:
    runs, total = await service.list_runs(limit=limit)
    return ImportRunList(runs=[_read(run) for run in runs], total=total)


@router.get("/imports/{run_id}", response_model=ImportRunRead)
async def get_run(run_id: uuid.UUID, service: HistoryImport) -> ImportRunRead:
    archive = await service.get_run(run_id)
    if archive is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="import_run_not_found"
        )
    return _read(archive)


__all__ = ["router"]
