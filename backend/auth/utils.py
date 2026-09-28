import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Union
from passlib.context import CryptContext
from jose import JWTError, jwt
from auth.config import auth_config

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against a hash"""
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    """Hash a password"""
    return pwd_context.hash(password)

def validate_password(password: str) -> tuple[bool, list[str]]:
    """Validate password against policy"""
    errors = []
    
    if len(password) < auth_config.MIN_PASSWORD_LENGTH:
        errors.append(f"Password must be at least {auth_config.MIN_PASSWORD_LENGTH} characters long")
    
    if auth_config.REQUIRE_UPPERCASE and not re.search(r"[A-Z]", password):
        errors.append("Password must contain at least one uppercase letter")
    
    if auth_config.REQUIRE_LOWERCASE and not re.search(r"[a-z]", password):
        errors.append("Password must contain at least one lowercase letter")
    
    if auth_config.REQUIRE_NUMBERS and not re.search(r"\d", password):
        errors.append("Password must contain at least one number")
    
    if auth_config.REQUIRE_SPECIAL_CHARS and not re.search(r"[!@#$%^&*(),.?\":{}|<>]", password):
        errors.append("Password must contain at least one special character")
    
    return len(errors) == 0, errors

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create a JWT access token"""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=auth_config.ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode.update({"exp": expire, "type": "access"})
    encoded_jwt = jwt.encode(to_encode, auth_config.SECRET_KEY, algorithm=auth_config.ALGORITHM)
    return encoded_jwt

def create_refresh_token(data: dict) -> str:
    """Create a JWT refresh token"""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(days=auth_config.REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update({"exp": expire, "type": "refresh"})
    encoded_jwt = jwt.encode(to_encode, auth_config.SECRET_KEY, algorithm=auth_config.ALGORITHM)
    return encoded_jwt

def decode_token(token: str) -> Optional[dict]:
    """Decode and validate a JWT token"""
    try:
        payload = jwt.decode(token, auth_config.SECRET_KEY, algorithms=[auth_config.ALGORITHM])
        return payload
    except JWTError:
        return None

def generate_api_key() -> str:
    """Generate a secure API key"""
    return f"uc_{secrets.token_urlsafe(32)}"

def generate_password_reset_token(email: str) -> str:
    """Generate a password reset token"""
    data = {"email": email, "type": "password_reset"}
    expire = datetime.now(timezone.utc) + timedelta(hours=24)
    data.update({"exp": expire})
    return jwt.encode(data, auth_config.SECRET_KEY, algorithm=auth_config.ALGORITHM)

def verify_password_reset_token(token: str) -> Optional[str]:
    """Verify a password reset token and return the email"""
    try:
        payload = jwt.decode(token, auth_config.SECRET_KEY, algorithms=[auth_config.ALGORITHM])
        if payload.get("type") != "password_reset":
            return None
        return payload.get("email")
    except JWTError:
        return None

def generate_email_verification_token(email: str) -> str:
    """Generate a signed email-verification token (24h expiry).

    Mirrors the password-reset helper but with type='email_verification'
    so a reset token can never be replayed as a verification token and
    vice versa. The email is the subject; verification flips the matching
    user's is_verified flag."""
    data = {
        "email": email,
        "type": "email_verification",
        "exp": datetime.now(timezone.utc) + timedelta(hours=24),
    }
    return jwt.encode(data, auth_config.SECRET_KEY, algorithm=auth_config.ALGORITHM)

def verify_email_verification_token(token: str) -> Optional[str]:
    """Verify an email-verification token and return the email, or None."""
    try:
        payload = jwt.decode(token, auth_config.SECRET_KEY, algorithms=[auth_config.ALGORITHM])
        if payload.get("type") != "email_verification":
            return None
        return payload.get("email")
    except JWTError:
        return None

def check_permission(user_role: str, permission: str) -> bool:
    """Check if a role has a specific permission"""
    from auth.config import PERMISSIONS, ROLE_HIERARCHY
    
    allowed_roles = PERMISSIONS.get(permission, [])
    user_roles = ROLE_HIERARCHY.get(user_role, [user_role])
    
    return any(role in allowed_roles for role in user_roles)

def sanitize_email(email: str) -> str:
    """Sanitize and validate email address"""
    email = email.strip().lower()
    # Basic email validation
    if not re.match(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", email):
        raise ValueError("Invalid email address")
    return email

def sanitize_username(username: str) -> str:
    """Sanitize and validate username"""
    username = username.strip().lower()
    # Username must be alphanumeric with underscores, 3-30 chars
    if not re.match(r"^[a-zA-Z0-9_]{3,30}$", username):
        raise ValueError("Username must be 3-30 characters, alphanumeric and underscores only")
    return username