import json
import os
import sqlite3
import time

import httpx

DB_PATH = os.getenv("DATABASE_PATH", "data/app.db")
SLEEP = int(os.getenv("WORKER_POLL_SECONDS", "5"))
BASE = os.getenv("GITHUB_API_BASE", "https://api.github.com")


def process_job(conn: sqlite3.Connection, row: tuple):
    job_id, payload_raw = row
    payload = json.loads(payload_raw)
    owner = payload["owner"]
    repo = payload["repo"]
    token = payload["token"]

    params = {}
    if payload.get("key"):
        params["key"] = payload["key"]
    if payload.get("ref"):
        params["ref"] = payload["ref"]

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        with httpx.Client(timeout=20) as client:
            resp = client.delete(f"{BASE}/repos/{owner}/{repo}/actions/caches", headers=headers, params=params)
        if resp.status_code >= 300:
            raise RuntimeError(f"GitHub API error: {resp.status_code} {resp.text}")
        result = json.dumps({"ok": True, "status": resp.status_code})
        conn.execute("UPDATE jobs SET status='done', result=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (result, job_id))
    except Exception as error:
        conn.execute("UPDATE jobs SET status='failed', result=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (str(error), job_id))


def run() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    while True:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute("SELECT id, payload FROM jobs WHERE status='queued' AND job_type='github.clear_actions_cache' ORDER BY id LIMIT 1").fetchone()
            if row:
                conn.execute("UPDATE jobs SET status='processing', updated_at=CURRENT_TIMESTAMP WHERE id=?", (row[0],))
                conn.commit()
                process_job(conn, row)
                conn.commit()
        time.sleep(SLEEP)


if __name__ == "__main__":
    run()
