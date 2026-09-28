#!/usr/bin/env python3
"""Update remote endpoint to use available model"""

import requests

BASE_URL = "http://localhost:9050"

# Login
response = requests.post(
    f"{BASE_URL}/api/auth/login",
    data={"username": "admin", "password": "admin123"}
)
token = response.json()["access_token"]
headers = {"Authorization": f"Bearer {token}"}

# Update the remote endpoint to use gemma2:9b
print("Updating remote endpoint to use gemma2:9b...")
update_response = requests.put(
    f"{BASE_URL}/api/llm-settings/endpoints/remote_gemma3_27b",
    json={
        "model": "gemma2:9b",
        "context_window": 8192,
        "description": "Remote Gemma2 9B for testing"
    },
    headers=headers
)

if update_response.status_code == 200:
    print("✅ Updated remote endpoint")
    
    # Test the endpoint
    print("Testing remote endpoint...")
    test_response = requests.post(
        f"{BASE_URL}/api/llm-settings/endpoints/remote_gemma3_27b/test",
        headers=headers
    )
    
    if test_response.status_code == 200:
        result = test_response.json()
        print(f"✅ Test successful!")
        print(f"   Status: {result.get('status')}")
        print(f"   Has model: {result.get('has_configured_model')}")
    else:
        print(f"❌ Test failed: {test_response.text}")
else:
    print(f"❌ Update failed: {update_response.text}")