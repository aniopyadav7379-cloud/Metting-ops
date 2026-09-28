#!/usr/bin/env python3
"""
Script to enable or disable user registration
"""
import os
import sys

def enable_registration(enable=True):
    env_file = '.env'
    
    # Read existing .env file
    if os.path.exists(env_file):
        with open(env_file, 'r') as f:
            lines = f.readlines()
    else:
        lines = []
    
    # Check if ALLOW_REGISTRATION exists
    found = False
    for i, line in enumerate(lines):
        if line.startswith('ALLOW_REGISTRATION='):
            lines[i] = f'ALLOW_REGISTRATION={"true" if enable else "false"}\n'
            found = True
            break
    
    # If not found, add it
    if not found:
        lines.append(f'\n# Registration settings\n')
        lines.append(f'ALLOW_REGISTRATION={"true" if enable else "false"}\n')
    
    # Write back
    with open(env_file, 'w') as f:
        f.writelines(lines)
    
    print(f"Registration {'enabled' if enable else 'disabled'} successfully!")
    print("Please restart the server for changes to take effect.")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "disable":
        enable_registration(False)
    else:
        enable_registration(True)