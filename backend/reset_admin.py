#!/usr/bin/env python3
"""
Script to check and reset the admin user
"""
import sys
from sqlalchemy.orm import Session
from database.database import SessionLocal, init_database
from auth.service import AuthService
from auth.models import User, AuditLog

def check_admin():
    """Check if admin user exists and show details"""
    init_database()
    db: Session = SessionLocal()
    
    try:
        admin = db.query(User).filter_by(username="admin").first()
        if admin:
            print(f"✅ Admin user exists:")
            print(f"   Username: {admin.username}")
            print(f"   Email: {admin.email}")
            print(f"   Active: {admin.is_active}")
            print(f"   Verified: {admin.is_verified}")
            print(f"   Superuser: {admin.is_superuser}")
            print(f"   Created: {admin.created_at}")
            return True
        else:
            print("❌ Admin user does not exist")
            return False
    finally:
        db.close()

def reset_admin():
    """Reset admin user with default credentials"""
    init_database()
    db: Session = SessionLocal()
    
    try:
        # Remove existing admin if exists
        existing_admin = db.query(User).filter_by(username="admin").first()
        if existing_admin:
            print("Removing existing admin user...")
            # Delete associated audit logs
            db.query(AuditLog).filter_by(user_id=existing_admin.id).delete()
            db.delete(existing_admin)
            db.commit()
        
        # Create new admin
        print("Creating new admin user...")
        admin_user = AuthService.create_user(
            db,
            email="admin@unicorn-commander.local",
            username="admin",
            password="Changeme123!",
            full_name="System Administrator",
            role="admin"
        )
        admin_user.is_superuser = True
        admin_user.is_verified = True
        db.commit()
        
        print("✅ Admin user created successfully!")
        print("   Username: admin")
        print("   Password: Changeme123!")
        print("   Email: admin@unicorn-commander.local")
        print("")
        print("⚠️  IMPORTANT: Change the password immediately after login!")
        
    except Exception as e:
        print(f"❌ Error creating admin user: {e}")
        db.rollback()
    finally:
        db.close()

def list_all_users():
    """List all users in the database"""
    init_database()
    db: Session = SessionLocal()
    
    try:
        users = db.query(User).all()
        if users:
            print(f"Found {len(users)} users:")
            for user in users:
                print(f"  - {user.username} ({user.email}) - {user.role} - {'Active' if user.is_active else 'Inactive'}")
        else:
            print("No users found in database")
    finally:
        db.close()

if __name__ == "__main__":
    if len(sys.argv) > 1:
        command = sys.argv[1]
        if command == "check":
            check_admin()
        elif command == "reset":
            reset_admin()
        elif command == "list":
            list_all_users()
        else:
            print("Usage: python reset_admin.py [check|reset|list]")
    else:
        print("Checking admin user...")
        if not check_admin():
            print("\nWould you like to create the admin user? (y/n): ", end="")
            if input().lower() == 'y':
                reset_admin()