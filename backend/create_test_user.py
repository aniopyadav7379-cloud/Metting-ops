#!/usr/bin/env python3
"""Create a test user for development"""

from database.database import SessionLocal, init_database
from auth.models import User
from auth.utils import get_password_hash
import sys

def create_test_user():
    # Initialize database
    init_database()
    
    db = SessionLocal()
    try:
        # Check if admin user exists
        admin = db.query(User).filter(User.username == "admin").first()
        if admin:
            print("✅ Admin user already exists")
            return
            
        # Create admin user
        admin_user = User(
            username="admin",
            email="admin@meetingops.local",
            hashed_password=get_password_hash("admin123"),
            is_active=True,
            is_superuser=True,
            role="superuser"
        )
        
        db.add(admin_user)
        db.commit()
        
        print("✅ Created admin user:")
        print("   Username: admin")
        print("   Password: admin123")
        print("   Role: superuser")
        
    except Exception as e:
        print(f"❌ Error creating user: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    create_test_user()