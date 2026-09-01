"""Stage 4E: the three executable tools, and their bounds.

Each tool is tested directly against a real directory. The bounds are the
interesting part -- a tool that works is table stakes, a tool that refuses
correctly is the deliverable.
"""

import pytest

from app.execution.errors import ToolFailure, WorkspaceViolation
from app.execution.tools import (
    CreateTextFileTool,
    ExecutionContext,
    ListWorkspaceFilesTool,
    ReadTextFileTool,
)


@pytest.fixture
def context(workspace) -> ExecutionContext:
    return ExecutionContext(
        workspace_root=workspace,
        max_file_bytes=1000,
        max_list_results=10,
        max_list_depth=3,
    )


def _run(tool, context, **arguments):
    return tool.run(tool.validate_arguments(arguments), context)


# --- create_text_file -------------------------------------------------------


def test_creating_a_file_writes_it(context, workspace) -> None:
    outcome = _run(CreateTextFileTool(), context, path="a.txt", content="hello")

    assert (workspace / "a.txt").read_text() == "hello"
    assert "a.txt" in outcome.summary


def test_creating_a_file_in_a_subdirectory_makes_the_directory(
    context, workspace
) -> None:
    _run(CreateTextFileTool(), context, path="notes/2026/today.txt", content="x")

    assert (workspace / "notes" / "2026" / "today.txt").read_text() == "x"


def test_creating_over_an_existing_file_is_refused(context, workspace) -> None:
    """Creating is not overwriting, and the difference is not the tool's to
    decide -- it is a separate approved parameter."""
    (workspace / "a.txt").write_text("original")

    with pytest.raises(ToolFailure):
        _run(CreateTextFileTool(), context, path="a.txt", content="replacement")

    assert (workspace / "a.txt").read_text() == "original"


def test_overwriting_works_when_it_was_approved(context, workspace) -> None:
    (workspace / "a.txt").write_text("original")

    _run(CreateTextFileTool(), context, path="a.txt", content="new", overwrite=True)

    assert (workspace / "a.txt").read_text() == "new"


def test_writing_through_a_symlink_is_refused(context, workspace) -> None:
    """`O_EXCL | O_NOFOLLOW`, so there is no check-then-open window.

    Testing the flags rather than a preceding `is_symlink()` check: a check
    could pass and the link be created before the open, and the write would
    land outside. The kernel refusing at open time has no such window.
    """
    outside = workspace.parent / "target.txt"
    outside.write_text("original")
    (workspace / "link.txt").symlink_to(outside)

    with pytest.raises((ToolFailure, WorkspaceViolation)):
        _run(CreateTextFileTool(), context, path="link.txt", content="through")

    assert outside.read_text() == "original"


# --- read_text_file ---------------------------------------------------------


def test_reading_returns_the_contents(context, workspace) -> None:
    (workspace / "a.txt").write_text("some notes")

    outcome = _run(ReadTextFileTool(), context, path="a.txt")

    assert outcome.data["content"] == "some notes"


def test_reading_a_missing_file_fails_cleanly(context) -> None:
    with pytest.raises(ToolFailure):
        _run(ReadTextFileTool(), context, path="nothing.txt")


def test_reading_a_file_over_the_size_bound_is_refused(context, workspace) -> None:
    """The bound is checked against the file, not the read buffer."""
    (workspace / "big.txt").write_text("x" * 2000)

    with pytest.raises(ToolFailure):
        _run(ReadTextFileTool(), context, path="big.txt")


def test_reading_a_binary_file_is_refused(context, workspace) -> None:
    """A null byte means this is not text, whatever the extension says."""
    (workspace / "image.txt").write_bytes(bytes([0x89]) + b"PNG\r\n\x1a\n\x00binary")

    with pytest.raises(ToolFailure):
        _run(ReadTextFileTool(), context, path="image.txt")


def test_reading_a_directory_is_refused(context, workspace) -> None:
    (workspace / "folder").mkdir()

    with pytest.raises(ToolFailure):
        _run(ReadTextFileTool(), context, path="folder")


# --- list_workspace_files ---------------------------------------------------


def test_listing_returns_relative_paths_only(context, workspace) -> None:
    """Never absolute: where the workspace lives is not the caller's business."""
    (workspace / "a.txt").write_text("x")
    (workspace / "sub").mkdir()
    (workspace / "sub" / "b.txt").write_text("y")

    outcome = _run(ListWorkspaceFilesTool(), context)

    files = outcome.data["files"]
    assert sorted(files) == ["a.txt", "sub/b.txt"]
    assert not any(str(workspace) in name for name in files)


def test_listing_is_bounded_by_result_count(context, workspace) -> None:
    for index in range(50):
        (workspace / f"file-{index}.txt").write_text("x")

    outcome = _run(ListWorkspaceFilesTool(), context)

    assert len(outcome.data["files"]) <= context.max_list_results


def test_listing_does_not_follow_symlinked_directories(context, workspace) -> None:
    """A symlink is not a door out, for listing either."""
    outside = workspace.parent / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("not yours")
    (workspace / "link").symlink_to(outside, target_is_directory=True)

    outcome = _run(ListWorkspaceFilesTool(), context)

    assert not any("secret" in name for name in outcome.data["files"])


def test_listing_an_escaping_subdirectory_is_refused(context) -> None:
    with pytest.raises(WorkspaceViolation):
        _run(ListWorkspaceFilesTool(), context, path="../..")


# --- Argument validation ----------------------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"path": "a.txt"},
        {"path": "", "content": "x"},
        {"path": "a.txt", "content": "x", "unexpected": True},
        {"path": "a.txt", "content": "x", "overwrite": "yes-please"},
    ],
)
def test_bad_arguments_are_refused_before_anything_runs(
    context, workspace, arguments
) -> None:
    with pytest.raises(ToolFailure):
        _run(CreateTextFileTool(), context, **arguments)

    assert list(workspace.iterdir()) == []


def test_a_validation_failure_names_fields_but_never_values(context) -> None:
    """A refusal must not echo what was sent back at whoever sent it."""
    tool = CreateTextFileTool()

    with pytest.raises(ToolFailure) as failure:
        tool.validate_arguments({"path": "a.txt", "secret_token": "hunter2"})

    assert "hunter2" not in failure.value.detail
    assert "secret_token" in failure.value.detail


# --- Path components, independent of containment ----------------------------


def test_a_traversal_that_stays_inside_is_still_refused(context, workspace) -> None:
    """`..` is refused as a *component*, not only when it escapes.

    `nested/../inside.txt` resolves inside the workspace, so the containment
    check has no objection to it -- this is the one case where the component
    rule is the only thing that fires. Two reasons it should:

    - The approved string and the file touched would differ. The audit journal
      would record `nested/../inside.txt` while `inside.txt` changed, and an
      audit trail that does not name the file it changed is worth less.
    - Many strings would map to one file, so a fingerprint would stop being a
      reliable identity for an action.
    """
    with pytest.raises(WorkspaceViolation):
        _run(
            CreateTextFileTool(), context,
            path="nested/../inside.txt", content="x",
        )

    assert list(workspace.iterdir()) == []


def test_pathlib_normalisation_is_not_relied_on_for_traversal(context) -> None:
    """`.` and empty components never reach the component check, `..` does.

    `PurePosixPath` drops `.` and empty segments during parsing, so those two
    members of the forbidden set are unreachable through `parts` -- they are
    belt and braces against a future change of parser, not live checks. `..`
    is preserved by pathlib, which is why it is the member that fires and the
    one the test above exercises.

    Worth pinning: if a later refactor parsed paths differently, the set would
    silently start doing more work than this comment claims.
    """
    from pathlib import PurePosixPath

    assert PurePosixPath("./a.txt").parts == ("a.txt",)
    assert PurePosixPath("a//b.txt").parts == ("a", "b.txt")
    assert PurePosixPath("a/../b.txt").parts == ("a", "..", "b.txt")
