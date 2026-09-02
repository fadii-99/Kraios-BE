"""Where the BOQ agent's own transcript lives on disk, and how to drop it.

Kept apart from `main_agent` on purpose: that module imports the agent runtime
(strands, model clients, tools), and clearing a session must not depend on any
of it. The delete path needs this while tearing rows down, and a worker image
without the agent installed still has to be able to forget a conversation.

The agent's transcript is separate from the `ConversationMessage` rows the UI
reads. Both have to be cleared together, or a deleted chat block goes on
shaping the agent's answers.
"""
import os
import shutil
import tempfile

try:  # pragma: no cover - the prefix has been stable across strands versions
    from strands.session.file_session_manager import SESSION_PREFIX
except Exception:
    SESSION_PREFIX = "session_"


def session_directory(session_id: str) -> str:
    """Where strands persists this session's transcript."""
    base = os.path.join(tempfile.gettempdir(), "strands", "sessions")
    return os.path.join(base, f"{SESSION_PREFIX}{session_id}")


def clear_session(session_id: str) -> bool:
    """Forget a project's BOQ conversation entirely.

    The whole session goes, not just the deleted turn: the transcript is a
    linear run of messages in which a tool call and its result are separate
    entries, so lifting one turn out of the middle would leave a `tool_use`
    with no `tool_result`, which providers reject outright.

    A cleared session is not a blind agent — every turn is sent the project
    context again — it only stops it quoting a turn the user removed.

    Returns whether a session was actually removed.
    """
    directory = session_directory(session_id)
    if not os.path.isdir(directory):
        return False
    shutil.rmtree(directory, ignore_errors=True)
    return not os.path.isdir(directory)
