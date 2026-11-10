"""Shared test-only defaults for required encryption contracts."""

import os


os.environ.setdefault(
    "TASK_EVENT_ENCRYPTION_KEY",
    "test-only-task-event-key-not-for-any-runtime-use",
)
