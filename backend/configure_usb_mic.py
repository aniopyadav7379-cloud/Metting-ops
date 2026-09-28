#!/usr/bin/env python3
"""
Configure the system to use USB microphone with correct settings
"""

from database.database import get_db
from services.settings_manager import settings_manager

def configure_usb_mic():
    """Update settings for USB microphone"""
    
    # Update audio settings
    audio_settings = {
        'input_source': {
            'value': 'hw:0,0',  # USB device
            'label': 'USB Microphone',
            'options': ['hw:0,0', 'default', 'hw:2,0'],
            'type': 'select'
        },
        'sample_rate': {
            'value': 48000,  # USB mic supports 48000
            'label': 'Sample Rate',
            'options': [48000, 44100],
            'type': 'select'
        },
        'channels': {
            'value': 1,
            'label': 'Channels',
            'type': 'number'
        }
    }
    
    # Update each setting
    for key, value in audio_settings.items():
        settings_manager.update_setting('audio', key, value)
        print(f"✅ Updated audio.{key} = {value['value']}")
    
    # Get and display current settings
    print("\n📊 Current Audio Settings:")
    current = settings_manager.get_category_settings('audio')
    for key, value in current.items():
        if isinstance(value, dict) and 'value' in value:
            print(f"   {key}: {value['value']}")
        else:
            print(f"   {key}: {value}")

if __name__ == "__main__":
    configure_usb_mic()