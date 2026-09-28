"""
Startup script for unified audio capture
Run this to initialize the single audio capture service
"""

import asyncio
import logging
import os
from services.unified_audio_capture import unified_audio_capture

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def main():
    """Start the unified audio capture service"""
    
    # Check if another audio process is running
    import subprocess
    result = subprocess.run(['pgrep', '-f', 'arecord'], capture_output=True)
    if result.returncode == 0:
        pids = result.stdout.decode().strip().split('\n')
        logger.warning(f"⚠️ Found existing arecord processes: {pids}")
        logger.info("Killing existing audio processes...")
        subprocess.run(['pkill', '-f', 'arecord'])
        await asyncio.sleep(1)
    
    # Start unified capture
    logger.info("🚀 Starting Unified Audio Capture Service")
    await unified_audio_capture.start()
    
    # Keep running
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        await unified_audio_capture.stop()

if __name__ == "__main__":
    asyncio.run(main())