"""Read-only native records. No initialization, migration, promotion or tool dispatch."""
from __future__ import annotations
from dataclasses import asdict
import sqlite3
from typing import Any


def _connection(board: str) -> sqlite3.Connection:
    from hermes_cli import kanban_db as kb
    path = kb.kanban_db_path(board=board)
    if not path.is_file():
        raise ValueError(f"Native board {board!r} is unavailable; initialize it through Hermes")
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute('BEGIN')
    return connection


def board_run_watermark(board: str) -> int:
    connection = _connection(board)
    try:
        row = connection.execute('SELECT COALESCE(MAX(id), 0) FROM task_runs').fetchone()
        watermark = row[0] if row else 0
        if type(watermark) is not int or watermark < 0: raise ValueError(f"Native board {board!r} returned an invalid run watermark")
        return watermark
    finally: connection.close()


def snapshot(board: str, task_id: str) -> dict[str, Any]:
    from hermes_cli import kanban_db as kb
    connection = _connection(board)
    try:
        task = kb.get_task(connection, task_id)
        if task is None: raise ValueError(f"Native task {task_id!r} was not found on board {board!r}")
        return {'task': asdict(task), 'runs': [asdict(r) for r in kb.list_runs(connection, task_id)], 'events': [asdict(e) for e in kb.list_events(connection, task_id)]}
    finally: connection.close()


def board_snapshot(board: str) -> list[dict[str, Any]]:
    """One read transaction used only to exclude concurrent shared workspaces."""
    from hermes_cli import kanban_db as kb
    connection = _connection(board)
    try:
        return [asdict(task) for task in kb.list_tasks(connection, include_archived=True)]
    finally: connection.close()


def reassign_ready_task(board: str, task_id: str, profile: str) -> bool:
    """Use Hermes' supported reassignment API only after a durable intent.

    This is not plugin SQL: ``reassign_task`` owns native validation, event
    recording, notifications, and failure-counter semantics. It refuses a live
    claim rather than reclaiming a worker.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    connection = kbc.connect(board=board)
    try:
        return kb.reassign_task(connection, task_id, profile, reclaim_first=False,
                                reason="review-correction escalation")
    finally:
        connection.close()
