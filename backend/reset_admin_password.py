#!/usr/bin/env python3
"""
Reset admin password for testing
"""

from database.database import get_db
from auth.models import User
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def reset_admin_password():
    db = next(get_db())
    
    # Find admin user
    admin = db.query(User).filter(User.username == "admin").first()
    
    if admin:
        # Reset password to admin123
        admin.hashed_password = pwd_context.hash("admin123")
        admin.is_active = True
        admin.failed_login_attempts = 0
        admin.locked_until = None
        db.commit()
        print("✅ Admin password reset to: admin123")
    else:
        # Create admin user
        admin = User(
            username="admin",
            email="admin@example.com",
            full_name="Admin User",
            hashed_password=pwd_context.hash("admin123"),
            is_active=True,
            is_verified=True,
            is_superuser=True
        )
        db.add(admin)
        db.commit()
        print("✅ Admin user created with password: admin123")

if __name__ == "__main__":
    reset_admin_password()