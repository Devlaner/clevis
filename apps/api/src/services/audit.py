import json
from src.core.storage import get_conn


def write_audit(actor: str, action: str, target: str, payload: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO audit_logs(actor, action, target, payload) VALUES (?, ?, ?, ?)",
            (actor, action, target, json.dumps(payload)),
        )
