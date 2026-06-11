"""
history_db.py — Work-history persistence
==========================================
Stores one document per user task (search → analyze → execute → audit).
Hard-caps at 20 entries per user (oldest deleted automatically).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from bson import ObjectId
from auth import get_db


# ── Create ────────────────────────────────────────────────────────────────────
async def history_create(user_id: str, task: str, repos: list) -> str:
    """Insert a new history entry; prune so at most 20 exist for this user."""
    db  = get_db()
    doc = {
        "user_id":    ObjectId(user_id),
        "task":       task,
        "repos":      repos,           # top-3 search results
        "selected_repo": None,
        "analysis":   None,
        "execution":  None,
        "audit":      None,
        "job_id":     "",
        "status":     "searching",
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }
    result = await db["history"].insert_one(doc)

    # Keep only the latest 20 for this user
    cursor = db["history"].find(
        {"user_id": ObjectId(user_id)},
        sort=[("created_at", -1)],
        skip=20,
    )
    old_ids = [d["_id"] async for d in cursor]
    if old_ids:
        await db["history"].delete_many({"_id": {"$in": old_ids}})

    return str(result.inserted_id)


# ── Update ────────────────────────────────────────────────────────────────────
async def history_update(history_id: str, user_id: str, **fields) -> None:
    """Partial update.  Pass any subset of fields to merge."""
    db = get_db()
    fields["updated_at"] = datetime.utcnow()
    await db["history"].update_one(
        {"_id": ObjectId(history_id), "user_id": ObjectId(user_id)},
        {"$set": fields},
    )


# ── Read ──────────────────────────────────────────────────────────────────────
async def history_list(user_id: str, limit: int = 20) -> list[dict]:
    db     = get_db()
    cursor = db["history"].find(
        {"user_id": ObjectId(user_id)},
        sort=[("created_at", -1)],
    ).limit(limit)
    out = []
    async for doc in cursor:
        out.append(_serialize(doc))
    return out


async def history_get(history_id: str, user_id: str) -> Optional[dict]:
    db  = get_db()
    doc = await db["history"].find_one({
        "_id":     ObjectId(history_id),
        "user_id": ObjectId(user_id),
    })
    return _serialize(doc) if doc else None


# ── Delete ────────────────────────────────────────────────────────────────────
async def history_delete(history_id: str, user_id: str) -> bool:
    db     = get_db()
    result = await db["history"].delete_one({
        "_id":     ObjectId(history_id),
        "user_id": ObjectId(user_id),
    })
    return result.deleted_count > 0


# ── Serializer ────────────────────────────────────────────────────────────────
def _serialize(doc: dict) -> dict:
    doc["_id"]     = str(doc["_id"])
    doc["user_id"] = str(doc["user_id"])
    # Convert any nested ObjectId (safety net)
    for k, v in doc.items():
        if isinstance(v, ObjectId):
            doc[k] = str(v)
        if isinstance(v, datetime):
            doc[k] = v.isoformat()
    return doc
