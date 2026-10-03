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
from bson.errors import InvalidId
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
        "architecture": None,
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
def _oid(value: str):
    """ObjectId or None. A malformed id is a lookup miss, not a server error —
    `ObjectId("not-an-objectid")` raises InvalidId, which reached the client as
    a 500 on every history route."""
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None


async def history_update(history_id: str, user_id: str, **fields) -> bool:
    """Partial update. Pass any subset of fields to merge.

    Returns whether a document owned by this user actually matched, so the
    route can 404 instead of reporting "updated" for an entry that does not
    exist or belongs to someone else.
    """
    hid, uid = _oid(history_id), _oid(user_id)
    if hid is None or uid is None:
        return False
    db = get_db()
    fields["updated_at"] = datetime.utcnow()
    result = await db["history"].update_one(
        {"_id": hid, "user_id": uid},
        {"$set": fields},
    )
    return result.matched_count > 0


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
    hid, uid = _oid(history_id), _oid(user_id)
    if hid is None or uid is None:
        return None
    db  = get_db()
    doc = await db["history"].find_one({"_id": hid, "user_id": uid})
    return _serialize(doc) if doc else None


# ── Delete ────────────────────────────────────────────────────────────────────
async def history_delete(history_id: str, user_id: str) -> bool:
    hid, uid = _oid(history_id), _oid(user_id)
    if hid is None or uid is None:
        return False
    db     = get_db()
    result = await db["history"].delete_one({"_id": hid, "user_id": uid})
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
