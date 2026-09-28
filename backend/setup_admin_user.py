#!/usr/bin/env python3
"""
Setup admin user for Meeting-Ops
"""

import sqlite3
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def setup_database():
    """Create users table and admin user"""
    conn = sqlite3.connect("meeting_sessions.db")
    cur = conn.cursor()
    
    # Create users table if it doesn't exist
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT,
            hashed_password TEXT NOT NULL,
            is_superuser BOOLEAN DEFAULT 0,
            full_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Check if admin user exists
    cur.execute("SELECT username FROM users WHERE username = ?", ("admin",))
    if not cur.fetchone():
        # Create admin user with password "admin123"
        hashed_password = pwd_context.hash("admin123")
        cur.execute("""
            INSERT INTO users (username, email, hashed_password, is_superuser, full_name)
            VALUES (?, ?, ?, ?, ?)
        """, ("admin", "admin@meeting-ops.local", hashed_password, 1, "Administrator"))
        print("✅ Admin user created: admin / admin123")
    else:
        # Update admin password to ensure it's correct
        hashed_password = pwd_context.hash("admin123")
        cur.execute("""
            UPDATE users 
            SET hashed_password = ?
            WHERE username = ?
        """, (hashed_password, "admin"))
        print("✅ Admin password reset to: admin123")
    
    # Also ensure user account exists
    cur.execute("SELECT username FROM users WHERE username = ?", ("user",))
    if not cur.fetchone():
        hashed_password = pwd_context.hash("user123")
        cur.execute("""
            INSERT INTO users (username, email, hashed_password, is_superuser, full_name)
            VALUES (?, ?, ?, ?, ?)
        """, ("user", "user@meeting-ops.local", hashed_password, 0, "Standard User"))
        print("✅ User account created: user / user123")
    
    conn.commit()
    
    # List all users
    cur.execute("SELECT username, email, is_superuser FROM users")
    users = cur.fetchall()
    print("\n📋 Current users in database:")
    for user in users:
        role = "Admin" if user[2] else "User"
        print(f"  - {user[0]} ({user[1]}) - {role}")
    
    conn.close()

if __name__ == "__main__":
    print("🔧 Setting up Meeting-Ops database and users...")
    setup_database()
    print("\n✅ Database setup complete!")
    print("\n🚀 You can now login with:")
    print("  Admin: admin / admin123")
    print("  User:  user / user123")