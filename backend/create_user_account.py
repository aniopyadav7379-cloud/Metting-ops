#!/usr/bin/env python3
"""
Create a regular user account for testing User Dashboard
"""

from database.database import get_db
from auth.models import User
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def create_user_account():
    db = next(get_db())
    
    # Check if user already exists
    existing_user = db.query(User).filter(User.username == "user").first()
    
    if existing_user:
        print("User 'user' already exists. Updating password...")
        existing_user.hashed_password = pwd_context.hash("user123")
        existing_user.is_active = True
        existing_user.failed_login_attempts = 0
        existing_user.locked_until = None
        db.commit()
        print("✅ User password updated to: user123")
    else:
        # Create regular user account
        user = User(
            username="user",
            email="user@meeting-ops.local",
            full_name="Regular User",
            hashed_password=pwd_context.hash("user123"),
            is_active=True,
            is_verified=True,
            is_superuser=False  # Regular user, not admin
        )
        db.add(user)
        db.commit()
        print("✅ Regular user account created:")
        print("   Username: user")
        print("   Password: user123")
        print("   Role: Regular User (non-admin)")

if __name__ == "__main__":
    create_user_account()