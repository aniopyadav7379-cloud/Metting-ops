#!/usr/bin/env python3
"""
Add default users directly to PostgreSQL
"""

import psycopg2
from passlib.context import CryptContext

# Database connection
conn = psycopg2.connect(
    host="localhost",
    database="meeting_sessions",
    user="meetingops",
    password="meetingops123"
)
cur = conn.cursor()

# Password hasher
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def ensure_user(username, email, password, full_name, is_superuser):
    """Ensure user exists with given credentials"""
    
    # Check if user exists
    cur.execute("SELECT id FROM users WHERE username = %s", (username,))
    existing = cur.fetchone()
    
    hashed_pw = pwd_context.hash(password)
    
    if existing:
        # Update existing user
        cur.execute("""
            UPDATE users 
            SET email = %s, 
                hashed_password = %s,
                full_name = %s,
                is_superuser = %s,
                is_active = true,
                is_verified = true
            WHERE username = %s
        """, (email, hashed_pw, full_name, is_superuser, username))
        print(f"✅ Updated {username} - password: {password}")
    else:
        # Insert new user
        cur.execute("""
            INSERT INTO users (username, email, hashed_password, full_name, is_superuser, is_active, is_verified)
            VALUES (%s, %s, %s, %s, %s, true, true)
        """, (username, email, hashed_pw, full_name, is_superuser))
        print(f"✅ Created {username} - password: {password}")
    
    conn.commit()

print("🔧 Setting up default users...")

# Admin user
ensure_user(
    username="admin",
    email="admin@meeting-ops.local",
    password="admin123",
    full_name="System Administrator",
    is_superuser=True
)

# Regular user  
ensure_user(
    username="user",
    email="user@meeting-ops.local",
    password="user123",
    full_name="Regular User",
    is_superuser=False
)

# Verify users
cur.execute("SELECT username, email, is_superuser FROM users ORDER BY username")
users = cur.fetchall()

print("\n📋 Users in database:")
for username, email, is_super in users:
    role = "Admin" if is_super else "User"
    print(f"   - {username} ({role}): {email}")

cur.close()
conn.close()

print("\n✅ Default accounts ready:")
print("   Admin: admin / admin123")
print("   User:  user / user123")