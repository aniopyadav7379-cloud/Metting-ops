#!/usr/bin/env python3
"""Initialize admin user for Docker deployment"""
import sqlite3
import bcrypt
import os

# Database path
db_path = "/app/data/meeting_sessions.db"
os.makedirs(os.path.dirname(db_path), exist_ok=True)

# Connect to database
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# Create users table if it doesn't exist
cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    email TEXT UNIQUE NOT NULL,
    full_name TEXT,
    hashed_password TEXT NOT NULL,
    is_active BOOLEAN DEFAULT 1,
    is_superuser BOOLEAN DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")

# Check if admin exists
cursor.execute("SELECT id FROM users WHERE username = 'admin'")
if cursor.fetchone():
    print("Admin user already exists!")
else:
    # Create admin user
    password = "changeme123!"
    hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
    
    cursor.execute("""
    INSERT INTO users (username, email, full_name, hashed_password, is_active, is_superuser)
    VALUES (?, ?, ?, ?, ?, ?)
    """, ("admin", "admin@unicorn-commander.local", "System Administrator", hashed.decode('utf-8'), 1, 1))
    
    conn.commit()
    print("✅ Admin user created successfully!")
    print("Username: admin")
    print("Password: changeme123!")
    print("⚠️  Please change this password immediately after first login!")

conn.close()