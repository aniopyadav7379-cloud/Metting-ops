from datetime import datetime, timedelta, timezone
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import or_
import logging

from auth.models import User, Organization, UserOrganization, AuditLog
from auth.organization import (
    ensure_default_organization,
    ensure_organization,
    sync_user_organizations,
)
from auth.utils import (
    get_password_hash, verify_password, validate_password,
    create_access_token, create_refresh_token, generate_api_key,
    sanitize_email, sanitize_username
)
from auth.config import auth_config

logger = logging.getLogger(__name__)

class AuthService:
    """Service for authentication and user management"""

    @staticmethod
    def ensure_default_organization(db: Session) -> Organization:
        """Get or create the shared migration safety-net organization."""
        return ensure_default_organization(db)

    @staticmethod
    def ensure_organization(
        db: Session,
        slug: str,
        *,
        name: Optional[str] = None,
    ) -> Organization:
        """Get or create an organization by slug."""
        return ensure_organization(db, slug, name=name)
    
    @staticmethod
    def create_user(
        db: Session,
        email: str,
        username: str,
        password: str,
        full_name: Optional[str] = None,
        organization_id: Optional[int] = None,
        role: str = "user",
        created_by_id: Optional[int] = None,
        personal_org: bool = False,
    ) -> User:
        """Create a new user.

        Org assignment: an explicit ``organization_id`` always wins.
        Otherwise ``personal_org=True`` (self-serve consumer signup)
        provisions a PRIVATE per-user org (``{username}-personal``, user as
        admin) so consumers never share a workspace — same convention as the
        SSO new-user path in ``sync_user_organizations``. ``personal_org=False``
        (admin-created users, appliance) keeps the legacy shared default-org
        behavior.
        """
        # Validate inputs
        email = sanitize_email(email)
        username = sanitize_username(username)
        
        # Check if user already exists
        if db.query(User).filter(or_(User.email == email, User.username == username)).first():
            raise ValueError("User with this email or username already exists")
        
        # Validate password
        valid, errors = validate_password(password)
        if not valid:
            raise ValueError(f"Invalid password: {', '.join(errors)}")
        
        # Create user
        user = User(
            email=email,
            username=username,
            hashed_password=get_password_hash(password),
            full_name=full_name,
            is_active=True,
            is_verified=False
        )
        
        db.add(user)
        db.commit()
        db.refresh(user)
        
        # Add to organization if specified
        if organization_id:
            user_org = UserOrganization(
                user_id=user.id,
                organization_id=organization_id,
                role=role,
                invited_by=created_by_id
            )
            db.add(user_org)
            db.commit()
        elif personal_org:
            # Self-serve consumer: private per-user workspace so they never
            # land in (and see) another tenant's data. Mirrors the SSO
            # new-user convention: slug `{username}-personal`, user is admin.
            display = full_name or email or username
            personal = ensure_organization(
                db, f"{username}-personal", name=f"{display} (personal)"
            )
            db.add(
                UserOrganization(
                    user_id=user.id,
                    organization_id=personal.id,
                    role="admin",
                    invited_by=created_by_id,
                )
            )
            db.commit()
        else:
            default_org = ensure_default_organization(db)
            db.add(
                UserOrganization(
                    user_id=user.id,
                    organization_id=default_org.id,
                    role=role,
                    invited_by=created_by_id,
                )
            )
            db.commit()
        
        # Log user creation
        AuthService.log_action(
            db, user.id, "user_created", "user", str(user.id),
            details={"email": email, "username": username}
        )
        
        logger.info(f"Created new user: {username} ({email})")
        return user
    
    @staticmethod
    def authenticate_user(db: Session, username: str, password: str) -> Optional[User]:
        """Authenticate a user with username/email and password"""
        # Try to find user by username or email
        user = db.query(User).filter(
            or_(User.username == username.lower(), User.email == username.lower())
        ).first()
        
        if not user:
            return None
        
        # Check if account is locked
        if user.locked_until and user.locked_until > datetime.now(timezone.utc):
            logger.warning(f"Login attempt for locked account: {username}")
            return None
        
        # Verify password
        if not verify_password(password, user.hashed_password):
            # Increment failed login attempts
            user.failed_login_attempts += 1
            
            # Lock account if too many failed attempts
            if user.failed_login_attempts >= auth_config.MAX_LOGIN_ATTEMPTS:
                user.locked_until = datetime.now(timezone.utc) + timedelta(
                    minutes=auth_config.LOCKOUT_DURATION_MINUTES
                )
                logger.warning(f"Account locked due to failed login attempts: {username}")
            
            db.commit()
            return None
        
        # Reset failed login attempts on successful login
        user.failed_login_attempts = 0
        user.locked_until = None
        user.last_login = datetime.now(timezone.utc)
        db.commit()
        
        # Log successful login
        AuthService.log_action(db, user.id, "login", "user", str(user.id))
        
        return user
    
    @staticmethod
    def create_tokens(user: User, organization_id: Optional[int] = None) -> dict:
        """Create access and refresh tokens for a user"""
        # Get user's primary organization if not specified
        if not organization_id and user.organizations:
            organization_id = user.organizations[0].organization_id
        
        token_data = {
            "sub": str(user.id),
            "username": user.username,
            "email": user.email,
            "org_id": organization_id
        }
        
        access_token = create_access_token(token_data)
        refresh_token = create_refresh_token(token_data)
        
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer"
        }
    
    @staticmethod
    def get_user_by_id(db: Session, user_id: int) -> Optional[User]:
        """Get user by ID"""
        return db.query(User).filter(User.id == user_id).first()
    
    @staticmethod
    def get_user_by_email(db: Session, email: str) -> Optional[User]:
        """Get user by email"""
        return db.query(User).filter(User.email == email.lower()).first()
    
    @staticmethod
    def get_user_by_api_key(db: Session, api_key: str) -> Optional[User]:
        """Get user by API key"""
        return db.query(User).filter(User.api_key == api_key).first()
    
    @staticmethod
    def update_user(
        db: Session,
        user_id: int,
        full_name: Optional[str] = None,
        department: Optional[str] = None,
        phone: Optional[str] = None,
        avatar_url: Optional[str] = None
    ) -> User:
        """Update user profile"""
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise ValueError("User not found")
        
        if full_name is not None:
            user.full_name = full_name
        if department is not None:
            user.department = department
        if phone is not None:
            user.phone = phone
        if avatar_url is not None:
            user.avatar_url = avatar_url
        
        user.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(user)
        
        return user
    
    @staticmethod
    def change_password(db: Session, user_id: int, old_password: str, new_password: str) -> bool:
        """Change user password"""
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return False
        
        # Verify old password
        if not verify_password(old_password, user.hashed_password):
            return False
        
        # Validate new password
        valid, errors = validate_password(new_password)
        if not valid:
            raise ValueError(f"Invalid password: {', '.join(errors)}")
        
        # Update password
        user.hashed_password = get_password_hash(new_password)
        user.updated_at = datetime.now(timezone.utc)
        db.commit()
        
        # Log password change
        AuthService.log_action(db, user_id, "password_changed", "user", str(user_id))
        
        return True
    
    @staticmethod
    def generate_user_api_key(db: Session, user_id: int) -> str:
        """Generate a new API key for a user"""
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise ValueError("User not found")
        
        # Generate new API key
        api_key = generate_api_key()
        user.api_key = api_key
        user.api_key_created_at = datetime.now(timezone.utc)
        db.commit()
        
        # Log API key generation
        AuthService.log_action(db, user_id, "api_key_generated", "user", str(user_id))
        
        return api_key

    @staticmethod
    def reset_user_password(db: Session, username: str, new_password: str) -> bool:
        """Reset a user's password without the old password (admin action)"""
        user = db.query(User).filter(User.username == username).first()
        if not user:
            return False

        # Validate new password
        valid, errors = validate_password(new_password)
        if not valid:
            raise ValueError(f"Invalid password: {', '.join(errors)}")

        # Update password
        user.hashed_password = get_password_hash(new_password)
        user.updated_at = datetime.now(timezone.utc)
        db.commit()

        # Log password reset
        AuthService.log_action(db, user.id, "password_reset", "user", str(user.id))

        return True
    
    @staticmethod
    def get_user_role(db: Session, user_id: int, organization_id: int) -> Optional[str]:
        """Get user's role in an organization"""
        user_org = db.query(UserOrganization).filter(
            UserOrganization.user_id == user_id,
            UserOrganization.organization_id == organization_id
        ).first()
        
        if user_org:
            return user_org.role
        
        # Check if user is superuser
        user = db.query(User).filter(User.id == user_id).first()
        if user and user.is_superuser:
            return "superuser"
        
        return None
    
    @staticmethod
    def log_action(
        db: Session,
        user_id: int,
        action: str,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        details: Optional[dict] = None,
        organization_id: Optional[int] = None
    ):
        """Log an audit action"""
        audit_log = AuditLog(
            user_id=user_id,
            organization_id=organization_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            ip_address=ip_address,
            user_agent=user_agent,
            details=details
        )
        db.add(audit_log)
        db.commit()

    @staticmethod
    def get_or_create_sso_user(
        db: Session,
        email: str,
        username: Optional[str] = None,
        full_name: Optional[str] = None,
        is_superuser: bool = False,
        groups: Optional[list[str]] = None,
    ) -> User:
        """Look up a user by SSO email, auto-provisioning if absent.

        Used by Keycloak/oauth2-proxy forward-auth — the upstream proxy
        has already validated the OIDC session, so we trust the headers
        and just maintain a local User row for FK relationships.
        """
        from auth.utils import get_password_hash
        import secrets as _secrets
        import re as _re

        email = sanitize_email(email)
        groups = groups or []
        user = db.query(User).filter(User.email == email).first()
        if user:
            updated = False
            if user.is_superuser != is_superuser:
                user.is_superuser = is_superuser
                updated = True
            if not user.is_active:
                user.is_active = True
                updated = True
            if not user.is_verified:
                user.is_verified = True
                updated = True
            if full_name and user.full_name != full_name:
                user.full_name = full_name
                updated = True
            if updated:
                db.commit()
                db.refresh(user)
            sync_user_organizations(db, user, groups)
            return user

        # Auto-provision. Keycloak's preferred_username is often the full
        # email (e.g. connect@shafenkhan.com) which sanitize_username rejects
        # because of '@' and '.'. Strip non-alphanumeric chars from the
        # email local-part and require >=3 chars before sanitizing; back-fill
        # with a random token if the result is too short.
        raw = (username or email).split('@')[0]
        raw = _re.sub(r"[^a-zA-Z0-9_]", "_", raw).strip("_")[:30]
        if len(raw) < 3:
            raw = f"user_{_secrets.token_hex(4)}"
        candidate = sanitize_username(raw)
        if db.query(User).filter(User.username == candidate).first():
            candidate = f"{candidate}_{_secrets.token_hex(2)}"

        user = User(
            email=email,
            username=candidate,
            hashed_password=get_password_hash(_secrets.token_urlsafe(48)),
            full_name=full_name or candidate,
            is_active=True,
            is_verified=True,
            is_superuser=is_superuser,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        sync_user_organizations(db, user, groups)

        logger.info(
            "SSO auto-provisioned user: %s (%s) admin=%s groups=%s",
            candidate,
            email,
            is_superuser,
            groups,
        )
        return user
