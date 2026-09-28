#!/bin/bash
# Set USB mic gain to 200% (+18dB) for better Whisper transcription
# PipeWire resets volume on reboot, so this needs to run at startup
sleep 5  # Wait for PipeWire to initialize
pactl set-source-volume alsa_input.usb-C-Media_Electronics_Inc._USB_PnP_Sound_Device-00.pro-input-0 200%
echo "USB mic gain set to 200%"
