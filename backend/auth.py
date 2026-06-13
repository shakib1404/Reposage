"""
auth.py — Authentication & Authorization
=========================================
• bcrypt password hashing (offloaded to thread pool — never blocks event loop)
• JWT access tokens (7-day expiry)
• Password-reset tokens (1-hour expiry, stored in MongoDB)
• Gmail SMTP for reset emails
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import smtplib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import bcrypt as _bcrypt

from dotenv import load_dotenv
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId

load_dotenv()

# Thread pool dedicated to bcrypt so it never blocks the async event loop
_bcrypt_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bcrypt")

# ── Config ────────────────────────────────────────────────────────────────────
MONGO_URL          = os.getenv("MONGO_URL", "")
JWT_SECRET         = os.getenv("JWT_SECRET", "changeme")
GMAIL_USER         = os.getenv("GMAIL_USER", "")
GMAIL_PASS         = os.getenv("GMAIL_PASS", "")
JWT_ALGORITHM      = "HS256"
JWT_EXPIRE_DAYS    = 7
RESET_EXPIRE_HOURS = 1
DB_NAME            = "repomaster"
BCRYPT_ROUNDS      = 10   # 10 ≈ 50ms; 12 ≈ 200ms — both are secure

# ── MongoDB (shared client) ───────────────────────────────────────────────────
_mongo_client: Optional[AsyncIOMotorClient] = None

def get_db():
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = AsyncIOMotorClient(
            MONGO_URL,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=10000,
            maxPoolSize=10,
            minPoolSize=1,
        )
    return _mongo_client[DB_NAME]


# ── Password hashing (runs in thread pool — never blocks event loop) ──────────
# Pre-hash with SHA-256 so bcrypt never sees a password > 72 bytes.

def _normalise(plain: str) -> bytes:
    return hashlib.sha256(plain.encode("utf-8")).digest()

def _sync_hash(plain: str) -> str:
    return _bcrypt.hashpw(_normalise(plain), _bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("utf-8")

def _sync_verify(plain: str, hashed: str) -> bool:
    try:
        return _bcrypt.checkpw(_normalise(plain), hashed.encode("utf-8"))
    except Exception:
        return False

async def hash_password(plain: str) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_bcrypt_pool, _sync_hash, plain)

async def verify_password(plain: str, hashed: str) -> bool:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_bcrypt_pool, _sync_verify, plain, hashed)


# ── JWT ───────────────────────────────────────────────────────────────────────
_bearer = HTTPBearer(auto_error=False)

def create_access_token(user_id: str) -> str:
    payload = {
        "sub": str(user_id),
        "exp": datetime.utcnow() + timedelta(days=JWT_EXPIRE_DAYS),
        "iat": datetime.utcnow(),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def _decode_token(token: str) -> Optional[str]:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None

async def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> dict:
    """FastAPI dependency — injects the authenticated user or raises 401."""
    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user_id = _decode_token(creds.credentials)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    db   = get_db()
    user = await db["users"].find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    user["_id"] = str(user["_id"])
    return user

async def get_optional_user(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> Optional[dict]:
    """Same as get_current_user but returns None instead of raising 401."""
    if not creds:
        return None
    user_id = _decode_token(creds.credentials)
    if not user_id:
        return None
    try:
        db   = get_db()
        user = await db["users"].find_one({"_id": ObjectId(user_id)})
        if not user:
            return None
        user["_id"] = str(user["_id"])
        return user
    except Exception:
        return None


# ── User CRUD ─────────────────────────────────────────────────────────────────
async def create_user(username: str, email: str, password: str) -> dict:
    db = get_db()
    # Check duplicates and hash password concurrently
    email_exists, username_exists, hashed = await asyncio.gather(
        db["users"].find_one({"email": email.lower()}),
        db["users"].find_one({"username": username}),
        hash_password(password),
    )
    if email_exists:
        raise HTTPException(status_code=400, detail="Email already registered")
    if username_exists:
        raise HTTPException(status_code=400, detail="Username already taken")

    doc = {
        "username":      username,
        "email":         email.lower(),
        "password":      hashed,
        "created_at":    datetime.utcnow(),
        "reset_token":   None,
        "reset_expires": None,
    }
    result = await db["users"].insert_one(doc)
    doc["_id"] = str(result.inserted_id)
    return doc

async def authenticate_user(email: str, password: str) -> dict:
    db   = get_db()
    user = await db["users"].find_one({"email": email.lower()})
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not await verify_password(password, user["password"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    user["_id"] = str(user["_id"])
    return user


# ── Password reset ────────────────────────────────────────────────────────────
async def generate_reset_token(email: str) -> str:
    """Store a reset token in the user document and return it."""
    db   = get_db()
    user = await db["users"].find_one({"email": email.lower()})
    if not user:
        # Return silently — don't reveal whether email exists
        return ""
    token = secrets.token_urlsafe(32)
    expires = datetime.utcnow() + timedelta(hours=RESET_EXPIRE_HOURS)
    await db["users"].update_one(
        {"_id": user["_id"]},
        {"$set": {"reset_token": token, "reset_expires": expires}},
    )
    return token

async def reset_password(token: str, new_password: str) -> bool:
    db   = get_db()
    user = await db["users"].find_one({"reset_token": token})
    if not user:
        raise HTTPException(status_code=400, detail="Invalid reset token")
    if datetime.utcnow() > user.get("reset_expires", datetime.min):
        raise HTTPException(status_code=400, detail="Reset token expired")
    hashed = await hash_password(new_password)
    await db["users"].update_one(
        {"_id": user["_id"]},
        {"$set": {
            "password":      hashed,
            "reset_token":   None,
            "reset_expires": None,
        }},
    )
    return True


# ── Gmail email sender ────────────────────────────────────────────────────────
def send_reset_email(to_email: str, token: str,
                     frontend_url: str = "http://localhost:5173") -> None:
    """Send a password-reset email via Gmail SMTP SSL."""
    reset_link = f"{frontend_url}?reset_token={token}"

    msg           = MIMEMultipart("alternative")
    msg["Subject"] = "RepoSage — Reset your password"
    msg["From"]    = GMAIL_USER
    msg["To"]      = to_email

    html = f"""
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#0d1117;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
  <div style="max-width:480px;margin:40px auto;background:#161b22;border:1px solid #30363d;border-radius:12px;padding:32px">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:24px">
      <div style="width:32px;height:32px;background:#5d8eff;border-radius:8px;display:flex;align-items:center;justify-content:center">
        <span style="color:white;font-size:16px">⌨</span>
      </div>
      <span style="color:#e6edf3;font-size:18px;font-weight:600">RepoSage</span>
    </div>
    <h2 style="color:#e6edf3;font-size:20px;font-weight:600;margin:0 0 12px">Reset your password</h2>
    <p style="color:#8b949e;font-size:14px;line-height:1.6;margin:0 0 24px">
      You requested a password reset for your RepoSage account.
      Click the button below to set a new password. This link expires in <strong style="color:#e6edf3">1 hour</strong>.
    </p>
    <a href="{reset_link}"
       style="display:inline-block;padding:12px 28px;background:#5d8eff;color:white;border-radius:8px;
              text-decoration:none;font-weight:600;font-size:14px;margin-bottom:24px">
      Reset Password
    </a>
    <p style="color:#6e7681;font-size:12px;line-height:1.6;margin:0 0 8px">
      Or copy this link into your browser:
    </p>
    <p style="color:#5d8eff;font-size:12px;word-break:break-all;margin:0">
      {reset_link}
    </p>
    <hr style="border:none;border-top:1px solid #30363d;margin:24px 0">
    <p style="color:#6e7681;font-size:12px;margin:0">
      If you didn't request this, you can safely ignore this email.
      Your password won't change until you click the link above.
    </p>
  </div>
</body>
</html>"""

    msg.attach(MIMEText(html, "html"))
    msg.attach(MIMEText(
        f"Reset your RepoSage password: {reset_link}\n\nThis link expires in 1 hour.",
        "plain",
    ))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as smtp:
        smtp.login(GMAIL_USER, GMAIL_PASS)
        smtp.sendmail(GMAIL_USER, to_email, msg.as_string())
