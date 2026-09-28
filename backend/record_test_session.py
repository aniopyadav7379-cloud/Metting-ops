#!/usr/bin/env python3
"""
Record a 10-second test session and measure NPU performance
"""

import asyncio
import aiohttp
import json
import time
import sys

async def record_test_session():
    """Record a 10-second session via API"""
    api_url = "http://localhost:9050"
    
    # Login first
    print("🔐 Logging in...")
    async with aiohttp.ClientSession() as session:
        # Login - using form data for auth endpoint
        login_data = aiohttp.FormData()
        login_data.add_field('username', 'admin')
        login_data.add_field('password', 'changeme123!')
        async with session.post(f"{api_url}/api/auth/login", data=login_data) as resp:
            if resp.status != 200:
                print(f"❌ Login failed: {resp.status}")
                return
            auth_data = await resp.json()
            token = auth_data['access_token']
            print("✅ Logged in successfully")
        
        headers = {"Authorization": f"Bearer {token}"}
        
        # Create session
        print("\n📝 Creating recording session...")
        session_data = {
            "name": "NPU Performance Test - 10 seconds",
            "description": "Testing NPU real-time transcription performance"
        }
        async with session.post(f"{api_url}/api/recording-sessions", 
                               json=session_data, headers=headers) as resp:
            if resp.status != 200:
                print(f"❌ Failed to create session: {resp.status}")
                return
            session_info = await resp.json()
            # The session ID is nested in the response
            session_data = session_info.get('session', {})
            session_id = session_data.get('session_id')
            if not session_id:
                print(f"❌ No session ID in response: {session_info}")
                return
            print(f"✅ Created session: {session_id}")
        
        # Start recording
        print("\n🎙️ Starting recording...")
        print("📢 SPEAK NOW! You have 10 seconds...")
        print("")
        
        start_time = time.time()
        async with session.post(f"{api_url}/api/recording-sessions/{session_id}/start", 
                               headers=headers) as resp:
            if resp.status != 200:
                print(f"❌ Failed to start recording: {resp.status}")
                error_text = await resp.text()
                print(f"Error: {error_text}")
                return
            print("✅ Recording started!")
        
        # Show countdown
        for i in range(10, 0, -1):
            print(f"\r⏱️  {i} seconds remaining...", end="", flush=True)
            await asyncio.sleep(1)
        print("\r✅ Recording complete!        ")
        
        # Stop recording
        print("\n⏹️  Stopping recording...")
        stop_time = time.time()
        recording_duration = stop_time - start_time
        
        async with session.post(f"{api_url}/api/recording-sessions/{session_id}/stop", 
                               headers=headers) as resp:
            if resp.status != 200:
                print(f"❌ Failed to stop recording: {resp.status}")
                return
            print("✅ Recording stopped!")
        
        # Get session details with transcription
        print("\n📊 Fetching transcription results...")
        await asyncio.sleep(2)  # Give NPU time to process
        
        async with session.get(f"{api_url}/api/recording-sessions/{session_id}", 
                              headers=headers) as resp:
            if resp.status != 200:
                print(f"❌ Failed to get session: {resp.status}")
                return
            session_details = await resp.json()
        
        # Display results
        print("\n" + "="*60)
        print("📊 NPU TRANSCRIPTION RESULTS")
        print("="*60)
        print(f"Session ID: {session_id}")
        print(f"Recording Duration: {recording_duration:.2f} seconds")
        print(f"Status: {session_details.get('status', 'Unknown')}")
        
        # Get transcriptions
        transcriptions = session_details.get('transcriptions', [])
        if transcriptions:
            print(f"\n📝 Transcription Count: {len(transcriptions)}")
            print("\n🗣️ WHAT YOU SAID:")
            print("-"*60)
            
            full_text = ""
            total_processing_time = 0
            
            for i, trans in enumerate(transcriptions):
                text = trans.get('text', '')
                timestamp = trans.get('timestamp', '')
                confidence = trans.get('confidence', 0)
                processing_time = trans.get('processing_time', 0)
                
                if text.strip():
                    full_text += text + " "
                    total_processing_time += processing_time
                    print(f"[{i+1}] {text}")
                    print(f"    Confidence: {confidence:.2%}")
                    if processing_time > 0:
                        print(f"    NPU Processing: {processing_time:.3f}s")
            
            print("-"*60)
            print(f"\n📄 FULL TRANSCRIPTION:")
            print(full_text.strip() if full_text else "(No speech detected)")
            
            if total_processing_time > 0:
                rtf = recording_duration / total_processing_time
                print(f"\n⚡ NPU PERFORMANCE:")
                print(f"   Total NPU Time: {total_processing_time:.3f}s")
                print(f"   Real-time Factor: {rtf:.1f}x")
                print(f"   Throughput: {recording_duration/total_processing_time:.1f} sec/sec")
        else:
            print("\n⚠️  No transcriptions found yet. The audio might still be processing.")
            print("   Check the dashboard for live updates.")
        
        # Get audio file info
        audio_files = session_details.get('audio_files', [])
        if audio_files:
            print(f"\n💾 Audio Files Saved: {len(audio_files)}")
            for file in audio_files:
                print(f"   - {file.get('filename', 'Unknown')}: {file.get('size', 0)} bytes")

if __name__ == "__main__":
    print("🎤 NPU Real-Time Transcription Test")
    print("This will record 10 seconds of audio and transcribe it using NPU")
    print("-"*60)
    
    try:
        asyncio.run(record_test_session())
    except KeyboardInterrupt:
        print("\n\n⚠️  Test interrupted by user")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()