import json
from src.core.storage import get_conn


def enqueue(job_type: str, payload: dict) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO jobs(job_type, payload) VALUES (?, ?)",
            (job_type, json.dumps(payload)),
        )
        return int(cur.lastrowid)


def list_jobs(limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, job_type, status, result, created_at, updated_at FROM jobs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {
            "id": r[0],
            "job_type": r[1],
            "status": r[2],
            "result": r[3],
            "created_at": r[4],
            "updated_at": r[5],
        }
        for r in rows
    ]
