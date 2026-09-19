"""Import orchestration: file to archive to derived memory.

    file -> detect -> hash -> idempotency -> parse -> archive -> derive

Four properties this module is responsible for:

**Idempotency.** The archive's content hash is unique in the database. Running
the same export twice returns the first run instead of building a second copy
of the corpus. The check is the unique index, not a preceding SELECT: two
concurrent imports of the same file would both pass a check-then-act.

**Recoverability.** A run that fails part-way leaves its archive row with a
status and a reason code, and the conversations it already committed stay
committed. Re-running skips what is present -- conversations and messages are
unique per archive -- so a failed import is resumed rather than restarted.

**Separation.** Raw history lands in the archive tables. Derived knowledge
goes through the existing `MemoryService`, so imported memories are
deduplicated, conflict-resolved and retrieved by exactly the code that handles
live knowledge. Nothing here writes a `Memory` directly.

**Authority.** Imported content is untrusted historical data. Only messages
authored by the user are eligible to become memories; assistant, system and
tool content is archived and never mined. That is enforced structurally by
`EXTRACTABLE_ROLES` rather than by a prompt instruction.
"""

import uuid
from typing import List, NamedTuple, Optional, Sequence, Tuple

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import DatabaseError
from app.core.logging import get_logger
from app.knowledge.service import KnowledgeService
from app.history.models import (
    EXTRACTABLE_ROLES,
    ImportedArchive,
    ImportedConversation,
    ImportedMessage,
    ImportFormat,
    ImportStatus,
)
from app.history.parser import (
    ImportParseError,
    ParsedConversation,
    detect_format,
    parse_conversations,
    read_document,
)
from app.history.sources import ImportSourceError, resolve, sha256_of
from app.memory.models import Memory
from app.memory.service import MemoryProvenance, MemoryService

logger = get_logger(__name__)

_DB_ERRORS = (SQLAlchemyError, OSError)


class ImportOutcome(NamedTuple):
    """What one import call produced.

    `derived_memory_ids` is here so the caller can hand them to the same
    background pipeline a live turn uses. Returning them rather than running
    that pipeline inline is deliberate: entity and relationship extraction
    open their own sessions and commit, and doing that inside the import's
    transaction would mean a failure three steps later could roll back the
    archive.
    """

    archive: ImportedArchive
    already_imported: bool
    derived_memory_ids: List[uuid.UUID] = []


class HistoryImportError(Exception):
    """An import could not be started or completed. Carries a reason code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class HistoryImportService:
    def __init__(
        self,
        session: AsyncSession,
        memory_service: Optional[MemoryService] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._memories = memory_service

    # --- Reads ---------------------------------------------------------------

    async def list_runs(self, limit: int = 50) -> Tuple[List[ImportedArchive], int]:
        statement = (
            select(ImportedArchive)
            .order_by(ImportedArchive.created_at.desc())
            .limit(limit)
        )
        try:
            rows = (await self._session.execute(statement)).scalars().all()
            total = (
                await self._session.execute(
                    select(func.count()).select_from(ImportedArchive)
                )
            ).scalar_one()
        except _DB_ERRORS as exc:
            raise DatabaseError(f"Failed to list import runs: {exc}") from exc
        return list(rows), int(total)

    async def get_run(self, run_id: uuid.UUID) -> Optional[ImportedArchive]:
        try:
            return await self._session.get(ImportedArchive, run_id)
        except _DB_ERRORS as exc:
            raise DatabaseError(f"Failed to load import run: {exc}") from exc

    # --- The import ----------------------------------------------------------

    async def import_file(self, filename: str) -> ImportOutcome:
        """Import one export file.

        `already_imported` is the idempotency answer: `True` means this file
        had already been imported and nothing was done.
        """
        if not self._settings.HISTORY_IMPORT_ENABLED:
            raise HistoryImportError("history_import_disabled")

        try:
            path = resolve(filename, self._settings)
            digest = sha256_of(path, self._settings)
        except ImportSourceError as exc:
            raise HistoryImportError(exc.code) from exc

        existing = await self._find_by_digest(digest)
        if existing is not None:
            logger.info(
                "Import skipped: this archive is already imported",
                extra={"archive_id": str(existing.id), "status": existing.status.value},
            )
            return ImportOutcome(existing, True, [])

        try:
            import_format = detect_format(path, self._settings)
        except ImportParseError as exc:
            raise HistoryImportError(exc.code) from exc

        archive = ImportedArchive(
            source_filename=path.name,
            source_sha256=digest,
            source_bytes=path.stat().st_size,
            import_format=import_format,
            status=ImportStatus.PARSING,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(archive)
                await self._session.flush()
        except IntegrityError:
            # Another import of the same file committed between the lookup and
            # this insert. The database settled it; return the winner.
            await self._session.rollback()
            winner = await self._find_by_digest(digest)
            if winner is None:  # pragma: no cover - the unique index just fired
                raise HistoryImportError("import_conflict")
            return ImportOutcome(winner, True, [])

        logger.info(
            "Import started",
            extra={
                "archive_id": str(archive.id),
                "format": import_format.value,
                # The filename is user-supplied; the fingerprint is what
                # identifies the archive without echoing input back.
                "fingerprint": digest[:12],
            },
        )

        try:
            document = read_document(path, import_format, self._settings)
            outcome = parse_conversations(document, self._settings)
        except ImportParseError as exc:
            archive.status = ImportStatus.FAILED
            archive.error_code = exc.code
            await self._session.flush()
            logger.warning(
                "Import failed during parsing",
                extra={"archive_id": str(archive.id), "error_code": exc.code},
            )
            return ImportOutcome(archive, False, [])

        await self._persist(archive, outcome.conversations)

        archive.conversations_skipped = outcome.conversations_skipped
        archive.messages_skipped = outcome.messages_skipped
        archive.redactions = outcome.redactions
        archive.status = ImportStatus.EXTRACTING
        await self._session.flush()

        derived_ids = await self._derive_memories(archive)
        archive.memories_derived = len(derived_ids)

        archive.status = (
            ImportStatus.PARTIAL if outcome.truncated_by_limit else ImportStatus.COMPLETED
        )
        await self._session.flush()

        logger.info(
            "Import finished",
            extra={
                "archive_id": str(archive.id),
                "status": archive.status.value,
                "conversations": archive.conversations_imported,
                "messages": archive.messages_imported,
                "redactions": archive.redactions,
                "memories_derived": len(derived_ids),
            },
        )
        return ImportOutcome(archive, False, derived_ids)

    async def _find_by_digest(self, digest: str) -> Optional[ImportedArchive]:
        statement = select(ImportedArchive).where(
            ImportedArchive.source_sha256 == digest
        )
        try:
            return (await self._session.execute(statement)).scalar_one_or_none()
        except _DB_ERRORS as exc:
            raise DatabaseError(f"Failed to look up an archive: {exc}") from exc

    async def _persist(
        self, archive: ImportedArchive, conversations: Sequence[ParsedConversation]
    ) -> None:
        """Write the raw archive, one conversation per savepoint.

        Per-conversation savepoints are what make a failed import resumable: a
        conversation that violates a constraint is rolled back alone, and the
        ones already written stay written.
        """
        for parsed in conversations:
            try:
                async with self._session.begin_nested():
                    conversation = ImportedConversation(
                        archive_id=archive.id,
                        external_id=parsed.external_id,
                        title=parsed.title,
                        source_created_at=parsed.created_at,
                        source_updated_at=parsed.updated_at,
                        message_count=len(parsed.messages),
                    )
                    self._session.add(conversation)
                    await self._session.flush()

                    for message in parsed.messages:
                        self._session.add(
                            ImportedMessage(
                                conversation_id=conversation.id,
                                external_id=message.external_id,
                                role=message.role,
                                content=message.content,
                                content_type=message.content_type,
                                sequence=message.sequence,
                                source_created_at=message.created_at,
                                redactions=message.redactions,
                                truncated=message.truncated,
                            )
                        )
                    await self._session.flush()
            except IntegrityError:
                # Already present from an earlier run of the same archive.
                logger.info(
                    "Imported conversation skipped: already present",
                    extra={"archive_id": str(archive.id)},
                )
                continue
            except _DB_ERRORS as exc:
                raise DatabaseError(f"Failed to store imported history: {exc}") from exc

            archive.conversations_imported += 1
            archive.messages_imported += len(parsed.messages)

        await self._session.flush()

    # --- Derived memories -----------------------------------------------------

    async def _derive_memories(self, archive: ImportedArchive) -> List[uuid.UUID]:
        """Extract memories from the user's own words, within a call budget.

        Bounded by `IMPORT_MAX_EXTRACTION_CALLS`, because this is the only part
        of an import that costs model calls: a 5,000-conversation archive must
        not become 5,000 requests the moment someone clicks import. What the
        budget did not reach is left for a re-run.

        Conflict evaluation runs here too, and costs nothing extra:
        `app.knowledge` imports nothing from `app.llm`.

        Entity and relationship extraction deliberately do **not** run. Both
        are a model call *per memory*, and where a live turn produces one or
        two, an import produces hundreds -- turning one click into an
        unbounded burst of requests. Imported memories are ordinary rows, so
        a later backfill can enrich them; conflict detection meanwhile falls
        back to text matching, which is the same route it uses for any memory
        whose entities have not been extracted yet.
        """
        if not self._settings.IMPORT_MEMORY_EXTRACTION_ENABLED:
            return []
        if self._memories is None or self._memories.extractor is None:
            logger.info(
                "Import extraction skipped: no provider configured",
                extra={"archive_id": str(archive.id)},
            )
            return []

        knowledge = (
            KnowledgeService(self._session, self._settings)
            if self._settings.CONFLICT_DETECTION_ENABLED
            else None
        )
        conversations = await self._conversations_for(archive.id)
        budget = self._settings.IMPORT_MAX_EXTRACTION_CALLS
        derived: List[uuid.UUID] = []

        for conversation in conversations:
            if budget <= 0:
                break

            messages = await self._extractable_messages(conversation.id)
            window, anchor = self._window(messages)
            if window is None or anchor is None:
                continue

            budget -= 1
            stored = await self._memories.store_imported(
                user_text=window,
                provenance=MemoryProvenance.imported(
                    imported_message_id=anchor.id,
                    # The message's own time when the export gave one, else the
                    # conversation's. Both may be absent, and the fallback is
                    # the archive's own creation time -- recorded rather than
                    # invented, and never newer than now, so an undated import
                    # cannot outrank a live statement made later.
                    stated_at=(
                        anchor.source_created_at
                        or conversation.source_created_at
                        or archive.created_at
                    ),
                ),
            )
            for memory in stored:
                derived.append(memory.id)
                if knowledge is not None:
                    # Lexical and structural throughout -- `app.knowledge`
                    # imports nothing from `app.llm` -- so evaluating every
                    # imported memory costs queries, not requests. This is
                    # what makes an imported correction able to retire an
                    # earlier imported belief, and a later live statement able
                    # to retire both.
                    await knowledge.evaluate_memory(memory)

        return derived

    async def _conversations_for(
        self, archive_id: uuid.UUID
    ) -> Sequence[ImportedConversation]:
        statement = (
            select(ImportedConversation)
            .where(ImportedConversation.archive_id == archive_id)
            .order_by(ImportedConversation.source_created_at.asc().nulls_last())
        )
        try:
            return (await self._session.execute(statement)).scalars().all()
        except _DB_ERRORS as exc:
            raise DatabaseError(f"Failed to load imported conversations: {exc}") from exc

    async def _extractable_messages(
        self, conversation_id: uuid.UUID
    ) -> Sequence[ImportedMessage]:
        """The user's own messages, in order.

        The role filter is the authority boundary. Assistant text is what a
        model once said, not what the user believes; system text is instruction
        content and must never be mined at all. Filtering in the query rather
        than in Python means no caller can forget.
        """
        statement = (
            select(ImportedMessage)
            .where(
                ImportedMessage.conversation_id == conversation_id,
                ImportedMessage.role.in_(tuple(EXTRACTABLE_ROLES)),
            )
            .order_by(ImportedMessage.sequence.asc())
        )
        try:
            return (await self._session.execute(statement)).scalars().all()
        except _DB_ERRORS as exc:
            raise DatabaseError(f"Failed to load imported messages: {exc}") from exc

    def _window(
        self, messages: Sequence[ImportedMessage]
    ) -> Tuple[Optional[str], Optional[ImportedMessage]]:
        """A bounded slice of user text, and the message to attribute it to.

        The anchor is the *first* user message, because it is the one whose
        timestamp best represents when the conversation's context began, and
        because provenance should point at something the user actually wrote.
        """
        if not messages:
            return None, None

        pieces: List[str] = []
        total = 0
        for message in messages:
            remaining = self._settings.IMPORT_EXTRACTION_WINDOW_CHARS - total
            if remaining <= 0:
                break
            pieces.append(message.content[:remaining])
            total += min(len(message.content), remaining)

        text = "\n\n".join(pieces).strip()
        if len(text) < self._settings.IMPORT_MIN_CONVERSATION_CHARS:
            # Too little to be worth a model call. Short exchanges are the bulk
            # of any export and carry almost no durable context.
            return None, None
        return text, messages[0]


__all__ = ["HistoryImportError", "HistoryImportService", "ImportOutcome"]
