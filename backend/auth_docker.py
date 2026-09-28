"""Simplified auth for Docker deployment.

DEAD CODE: this router is not imported/mounted anywhere (the live auth is
auth/routes.py + auth/config.py). Kept only to avoid churn; the hardcoded
SECRET_KEY placeholder was removed (security-2) so this file can never ship a
forgeable signing key even if something imports it.
"""
from fastapi import APIRouter, HTTPException, Depends, Form
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import sqlite3
import bcrypt
import jwt
import datetime
import os

router = APIRouter(prefix="/api/auth", tags=["auth"])
security = HTTPBearer()

SECRET_KEY = os.getenv("SECRET_KEY", "")  # security-2: no hardcoded fallback
ALGORITHM = "HS256"

class LoginRequest(BaseModel):
    username: str
    password: str

class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: dict = None

def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))
    except Exception as e:
        print(f"Password verification error: {e}")
        return False

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.datetime.utcnow() + datetime.timedelta(hours=24)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

@router.post("/login")
async def login(username: str = Form(...), password: str = Form(...)):
    """Login endpoint"""
    conn = sqlite3.connect("meeting_sessions.db")
    cursor = conn.cursor()
    
    # Get user
    cursor.execute(
        "SELECT id, username, email, full_name, hashed_password, is_active, is_superuser FROM users WHERE username = ?",
        (username,)
    )
    user = cursor.fetchone()
    conn.close()
    
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    user_id, username_db, email, full_name, hashed_password, is_active, is_superuser = user
    
    if not is_active:
        raise HTTPException(status_code=401, detail="User is inactive")
    
    if not verify_password(password, hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    # Create tokens
    access_token = create_access_token({
        "sub": username_db,
        "user_id": user_id,
        "is_superuser": bool(is_superuser)
    })
    
    # Create refresh token (valid for 7 days)
    refresh_token = create_access_token({
        "sub": username_db,
        "user_id": user_id,
        "is_superuser": bool(is_superuser),
        "exp": datetime.datetime.utcnow() + datetime.timedelta(days=7)
    })
    
    # Create user object
    user_obj = {
        "id": user_id,
        "username": username_db,
        "email": email,
        "full_name": full_name,
        "is_active": is_active,
        "is_superuser": bool(is_superuser),
        "role": "superuser" if is_superuser else "user"
    }
    
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "user": user_obj
    }

@router.get("/me")
async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Get current user info"""
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if username is None:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        # Get full user details from database
        conn = sqlite3.connect("meeting_sessions.db")
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, username, email, full_name, is_active, is_superuser FROM users WHERE username = ?",
            (username,)
        )
        user = cursor.fetchone()
        conn.close()
        
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        user_id, username_db, email, full_name, is_active, is_superuser = user
        
        return {
            "id": user_id,
            "username": username_db,
            "email": email,
            "full_name": full_name,
            "is_active": is_active,
            "is_superuser": bool(is_superuser),
            "role": "superuser" if is_superuser else "user"
        }
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")