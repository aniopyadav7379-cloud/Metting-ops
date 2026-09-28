"""
Authentik SSO Integration for Meeting-Ops Backend
Handles authentication via Authentik forward auth headers
"""
from fastapi import HTTPException, Request, Depends, status
from typing import Optional, Dict, Any, List
import os
import logging
import requests
from datetime import datetime, timedelta, timezone
import jwt
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class AuthentikUser(BaseModel):
    """User model from Authentik headers"""
    username: str
    email: str
    name: str
    uid: str
    groups: List[str]
    is_admin: bool = False
    is_active: bool = True
    authenticated_at: datetime


class AuthentikAuth:
    """Authentik authentication handler"""
    
    def __init__(self):
        self.authentik_url = os.getenv('AUTHENTIK_URL', 'http://authentik-server:9000')
        self.api_token = os.getenv('AUTHENTIK_API_TOKEN', '')
        self.verify_ssl = os.getenv('AUTHENTIK_VERIFY_SSL', 'true').lower() == 'true'
        self.admin_groups = os.getenv('AUTHENTIK_ADMIN_GROUPS', 'admin,Meeting-Ops-Admins').split(',')
        
        # Setup session
        self.session = requests.Session()
        if self.api_token:
            self.session.headers.update({
                'Authorization': f'Bearer {self.api_token}',
                'Content-Type': 'application/json'
            })
    
    def extract_user_from_headers(self, request: Request) -> Optional[AuthentikUser]:
        """
        Extract user information from Authentik forward auth headers
        """
        try:
            # Check for Authentik headers
            username = request.headers.get('X-authentik-username')
            email = request.headers.get('X-authentik-email')
            name = request.headers.get('X-authentik-name')
            uid = request.headers.get('X-authentik-uid')
            groups_header = request.headers.get('X-authentik-groups', '')
            
            if not username or not email:
                logger.debug("Missing required Authentik headers")
                return None
            
            # Parse groups
            groups = [g.strip() for g in groups_header.split(',') if g.strip()] if groups_header else []
            
            # Check if user is admin
            is_admin = any(group in self.admin_groups for group in groups)
            
            user = AuthentikUser(
                username=username,
                email=email,
                name=name or username,
                uid=uid or username,
                groups=groups,
                is_admin=is_admin,
                authenticated_at=datetime.now(timezone.utc)
            )
            
            logger.info(f"Authenticated user: {username} (admin: {is_admin}, groups: {groups})")
            return user
            
        except Exception as e:
            logger.error(f"Error extracting user from headers: {e}")
            return None
    
    def verify_user_with_authentik(self, user: AuthentikUser) -> bool:
        """
        Verify user with Authentik API (optional additional check)
        """
        if not self.api_token:
            logger.debug("No API token, skipping Authentik verification")
            return True
        
        try:
            # Query Authentik API to verify user
            response = self.session.get(
                f"{self.authentik_url}/api/v3/core/users/",
                params={'username': user.username},
                verify=self.verify_ssl
            )
            
            if response.status_code == 200:
                users = response.json().get('results', [])
                if users and users[0].get('is_active', False):
                    return True
            
            logger.warning(f"User verification failed for {user.username}")
            return False
            
        except Exception as e:
            logger.error(f"Error verifying user with Authentik: {e}")
            # If verification fails, still allow if headers are present
            return True
    
    def create_or_update_local_user(self, authentik_user: AuthentikUser):
        """
        Create or update local user record based on Authentik data
        """
        try:
            from sqlalchemy.orm import Session
            from database.database import get_db
            from models.user import User
            
            db: Session = next(get_db())
            
            # Check if user exists
            user = db.query(User).filter(User.username == authentik_user.username).first()
            
            if user:
                # Update existing user
                user.email = authentik_user.email
                user.full_name = authentik_user.name
                user.is_active = True
                user.is_superuser = authentik_user.is_admin
                user.last_login = datetime.now(timezone.utc)
            else:
                # Create new user
                user = User(
                    username=authentik_user.username,
                    email=authentik_user.email,
                    full_name=authentik_user.name,
                    is_active=True,
                    is_superuser=authentik_user.is_admin,
                    hashed_password="",  # No password needed for SSO users
                    created_at=datetime.now(timezone.utc),
                    last_login=datetime.now(timezone.utc)
                )
                db.add(user)
            
            db.commit()
            logger.info(f"User {authentik_user.username} synced to local database")
            return user
            
        except Exception as e:
            logger.error(f"Error creating/updating local user: {e}")
            return None


# Global auth instance
authentik_auth = AuthentikAuth()


async def get_current_user(request: Request) -> AuthentikUser:
    """
    Dependency to get current authenticated user from Authentik headers
    """
    # Extract user from headers
    user = authentik_auth.extract_user_from_headers(request)
    
    if not user:
        # Check if we're in development mode
        if os.getenv('DEVELOPMENT_MODE', 'false').lower() == 'true':
            # Return a mock user for development
            logger.warning("Development mode: using mock user")
            return AuthentikUser(
                username="dev-user",
                email="dev@meeting-ops.local",
                name="Development User",
                uid="dev-uid",
                groups=["admin"],
                is_admin=True,
                authenticated_at=datetime.now(timezone.utc)
            )
        
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated - missing Authentik headers",
            headers={"WWW-Authenticate": "Bearer"}
        )
    
    # Optionally verify with Authentik API
    if not authentik_auth.verify_user_with_authentik(user):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User verification failed"
        )
    
    # Sync user to local database
    authentik_auth.create_or_update_local_user(user)
    
    return user


async def get_current_admin_user(current_user: AuthentikUser = Depends(get_current_user)) -> AuthentikUser:
    """
    Dependency to require admin user
    """
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
    return current_user


async def get_current_active_user(current_user: AuthentikUser = Depends(get_current_user)) -> AuthentikUser:
    """
    Dependency to require active user
    """
    if not current_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive user"
        )
    return current_user


class AuthentikPermissions:
    """Permission checking utilities"""
    
    @staticmethod
    def check_group_permission(user: AuthentikUser, required_groups: List[str]) -> bool:
        """Check if user is in any of the required groups"""
        return any(group in user.groups for group in required_groups)
    
    @staticmethod
    def check_admin_permission(user: AuthentikUser) -> bool:
        """Check if user has admin permissions"""
        return user.is_admin
    
    @staticmethod
    def require_groups(required_groups: List[str]):
        """Decorator to require specific groups"""
        def decorator(current_user: AuthentikUser = Depends(get_current_user)):
            if not AuthentikPermissions.check_group_permission(current_user, required_groups):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Access denied. Required groups: {required_groups}"
                )
            return current_user
        return decorator


# Backward compatibility with existing auth system
async def get_current_user_legacy(request: Request):
    """
    Legacy compatibility for existing endpoints
    Returns user in the format expected by existing code
    """
    authentik_user = await get_current_user(request)
    
    # Convert to legacy user format if needed
    class LegacyUser:
        def __init__(self, authentik_user: AuthentikUser):
            self.id = authentik_user.uid
            self.username = authentik_user.username
            self.email = authentik_user.email
            self.full_name = authentik_user.name
            self.is_active = authentik_user.is_active
            self.is_superuser = authentik_user.is_admin
            self.role = "admin" if authentik_user.is_admin else "user"
    
    return LegacyUser(authentik_user)