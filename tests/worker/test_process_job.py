"""Worker job processing: unit-style tests with mocked GitHub HTTP."""

import json
import sqlite3
from unittest.mock import MagicMock, patch

from worker import process_job


def _jobs_conn(tmp_path):
    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY, status TEXT, result TEXT, updated_at TEXT)"
    )
    return conn


def test_process_job_marks_done_on_success(tmp_path):
    conn = _jobs_conn(tmp_path)
    payload = {"owner": "acme", "repo": "demo", "token": "secret"}
    conn.execute(
        "INSERT INTO jobs (id, status, result) VALUES (1, 'processing', NULL)"
    )
    conn.commit()

    mock_response = MagicMock()
    mock_response.status_code = 204
    mock_response.text = ""

    with patch("worker.httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.delete = MagicMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        process_job(conn, (1, json.dumps(payload)))
        conn.commit()

    row = conn.execute("SELECT status, result FROM jobs WHERE id=1").fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "done"
    assert row[1] is not None
    saved = json.loads(row[1])
    assert saved["ok"] is True
    assert saved["status"] == 204


def test_process_job_marks_failed_on_http_error(tmp_path):
    conn = _jobs_conn(tmp_path)
    payload = {"owner": "acme", "repo": "demo", "token": "secret"}
    conn.execute(
        "INSERT INTO jobs (id, status, result) VALUES (2, 'processing', NULL)"
    )
    conn.commit()

    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.text = "upstream error"

    with patch("worker.httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.delete = MagicMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        process_job(conn, (2, json.dumps(payload)))
        conn.commit()

    row = conn.execute("SELECT status, result FROM jobs WHERE id=2").fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "failed"
    assert "GitHub API error" in (row[1] or "")
