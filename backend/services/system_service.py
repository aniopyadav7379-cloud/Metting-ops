"""System management service for Meeting-Ops."""

import os
import json
import subprocess
import re
import socket
try:
    # Optional: only used by get_network_info(). netifaces is a C-extension that
    # can fail to build/install on newer Pythons; guard the import so its absence
    # can't drop this whole module — and with it any router that imports
    # SystemService (e.g. the auth-bearing websocket_auto_summary router).
    import netifaces
except Exception:  # pragma: no cover - environment-dependent
    netifaces = None
from pathlib import Path
from typing import List, Dict, Any, Optional
import logging
import asyncio
import aiohttp

from models.system_settings import (
    AudioDevice, AllSettings, NetworkInfo,
    AudioVolumeResponse, AuthentikTestResponse
)

logger = logging.getLogger(__name__)

# Settings file location. Env-overridable; defaults beside this backend rather than
# an absolute path from one machine (this ran mkdir() on /srv at import).
SETTINGS_FILE = Path(
    os.getenv(
        "SYSTEM_SETTINGS_FILE",
        str(Path(__file__).resolve().parents[1] / "config" / "system_settings.json"),
    )
)
SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)


class SystemService:
    """Service for system management operations."""
    
    @staticmethod
    def load_settings() -> AllSettings:
        """Load all settings from file or return defaults."""
        try:
            if SETTINGS_FILE.exists():
                with open(SETTINGS_FILE, 'r') as f:
                    data = json.load(f)
                    return AllSettings(**data)
            else:
                # Return default settings
                settings = AllSettings()
                SystemService.save_settings(settings)
                return settings
        except Exception as e:
            logger.error(f"Error loading settings: {e}")
            return AllSettings()
    
    @staticmethod
    def save_settings(settings: AllSettings) -> bool:
        """Save settings to file."""
        try:
            with open(SETTINGS_FILE, 'w') as f:
                json.dump(settings.dict(), f, indent=2)
            logger.info("Settings saved successfully")
            return True
        except Exception as e:
            logger.error(f"Error saving settings: {e}")
            return False
    
    @staticmethod
    def get_audio_devices() -> List[AudioDevice]:
        """Get list of available audio devices."""
        devices = []
        
        try:
            # Get audio devices using arecord
            result = subprocess.run(
                ['arecord', '-l'],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode == 0:
                # Parse arecord output
                lines = result.stdout.split('\n')
                current_card = None
                
                for line in lines:
                    # Match card line
                    card_match = re.match(r'card (\d+): (\w+) \[(.*?)\]', line)
                    if card_match:
                        card_num = card_match.group(1)
                        card_name = card_match.group(2)
                        card_label = card_match.group(3)
                        current_card = card_num
                    
                    # Match device line
                    device_match = re.match(r'\s+device (\d+): (.*?) \[(.*?)\]', line)
                    if device_match and current_card is not None:
                        device_num = device_match.group(1)
                        device_name = device_match.group(2)
                        device_label = device_match.group(3)
                        
                        # Determine if USB
                        is_usb = 'USB' in card_label or 'USB' in device_label
                        
                        # Default sample rates for USB devices
                        sample_rates = [44100, 48000] if is_usb else [44100, 48000, 96000]
                        
                        device = AudioDevice(
                            id=f"hw:{current_card},{device_num}",
                            name=device_name,
                            label=f"{card_label} - {device_label}",
                            sampleRates=sample_rates,
                            channels=1 if 'Mono' in device_label else 2,
                            isDefault=(current_card == '0' and device_num == '0'),
                            isUSB=is_usb
                        )
                        devices.append(device)
            
            # If no devices found through arecord, add default
            if not devices:
                devices.append(AudioDevice(
                    id="hw:0,0",
                    name="Default",
                    label="Default Audio Device",
                    sampleRates=[44100, 48000],
                    channels=1,
                    isDefault=True,
                    isUSB=False
                ))
                
        except Exception as e:
            logger.error(f"Error getting audio devices: {e}")
            # Return a default device on error
            devices.append(AudioDevice(
                id="hw:0,0",
                name="Error",
                label="Could not detect audio devices",
                sampleRates=[44100],
                channels=1,
                isDefault=True,
                isUSB=False
            ))
        
        return devices
    
    @staticmethod
    def set_audio_volume(device: str, volume: int) -> AudioVolumeResponse:
        """Set audio device volume."""
        try:
            # Extract card number from device (e.g., "hw:0,0" -> "0")
            card_match = re.match(r'hw:(\d+),\d+', device)
            if not card_match:
                return AudioVolumeResponse(
                    device=device,
                    volume=volume,
                    success=False
                )
            
            card_num = card_match.group(1)
            
            # USB mic (card 0) has range 0-16, convert from percentage
            if card_num == '0':
                # USB mic special handling
                actual_volume = int((volume / 100) * 16)
                cmd = ['amixer', '-c', card_num, 'set', 'Mic', str(actual_volume)]
            else:
                # Regular sound card, use percentage
                cmd = ['amixer', '-c', card_num, 'set', 'Capture', f'{volume}%']
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            
            if result.returncode == 0:
                logger.info(f"Set volume for {device} to {volume}%")
                return AudioVolumeResponse(
                    device=device,
                    volume=volume,
                    success=True
                )
            else:
                logger.error(f"Failed to set volume: {result.stderr}")
                return AudioVolumeResponse(
                    device=device,
                    volume=volume,
                    success=False
                )
                
        except Exception as e:
            logger.error(f"Error setting audio volume: {e}")
            return AudioVolumeResponse(
                device=device,
                volume=volume,
                success=False
            )
    
    @staticmethod
    def get_audio_volume(device: str) -> int:
        """Get current audio device volume as percentage."""
        try:
            # Extract card number from device
            card_match = re.match(r'hw:(\d+),\d+', device)
            if not card_match:
                return 75  # Default
            
            card_num = card_match.group(1)
            
            # Get current volume
            if card_num == '0':
                # USB mic
                cmd = ['amixer', '-c', card_num, 'get', 'Mic']
            else:
                cmd = ['amixer', '-c', card_num, 'get', 'Capture']
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            
            if result.returncode == 0:
                # Parse volume from output
                # Look for pattern like: [75%] or [12] (for USB mic)
                volume_match = re.search(r'\[(\d+)%?\]', result.stdout)
                if volume_match:
                    vol = int(volume_match.group(1))
                    
                    # Convert USB mic range (0-16) to percentage
                    if card_num == '0' and vol <= 16:
                        return int((vol / 16) * 100)
                    else:
                        return vol
            
            return 75  # Default if can't determine
            
        except Exception as e:
            logger.error(f"Error getting audio volume: {e}")
            return 75
    
    @staticmethod
    def get_network_info() -> NetworkInfo:
        """Get current network configuration."""
        try:
            # Get hostname
            hostname = socket.gethostname()

            if netifaces is None:
                # netifaces unavailable — return hostname + safe defaults instead
                # of erroring (network introspection is non-critical telemetry).
                return NetworkInfo(
                    hostname=hostname, ipAddress="0.0.0.0", gateway="0.0.0.0",
                    interface="unknown", dns1="8.8.8.8", dns2="8.8.4.4",
                    ipMode="dhcp", vlan=None,
                )

            # Get primary network interface
            gws = netifaces.gateways()
            default_interface = gws['default'].get(netifaces.AF_INET, [None, None])[1]
            
            if not default_interface:
                # Fallback to first available interface
                interfaces = netifaces.interfaces()
                for iface in interfaces:
                    if iface != 'lo':
                        default_interface = iface
                        break
            
            # Get IP address
            ip_address = "0.0.0.0"
            if default_interface:
                addrs = netifaces.ifaddresses(default_interface)
                if netifaces.AF_INET in addrs:
                    ip_address = addrs[netifaces.AF_INET][0]['addr']
            
            # Get gateway
            gateway = gws['default'].get(netifaces.AF_INET, ["0.0.0.0"])[0]
            
            # Get DNS servers from resolv.conf
            dns_servers = []
            try:
                with open('/etc/resolv.conf', 'r') as f:
                    for line in f:
                        if line.startswith('nameserver'):
                            dns_servers.append(line.split()[1])
            except:
                dns_servers = ['8.8.8.8', '8.8.4.4']
            
            dns1 = dns_servers[0] if len(dns_servers) > 0 else '8.8.8.8'
            dns2 = dns_servers[1] if len(dns_servers) > 1 else '8.8.4.4'
            
            # Check if using DHCP (simple check)
            ip_mode = "dhcp"  # Default assumption
            try:
                # Check if NetworkManager is managing the interface
                result = subprocess.run(
                    ['nmcli', 'con', 'show'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if 'manual' in result.stdout.lower():
                    ip_mode = "static"
            except:
                pass
            
            return NetworkInfo(
                hostname=hostname,
                ipAddress=ip_address,
                gateway=gateway,
                interface=default_interface or "unknown",
                dns1=dns1,
                dns2=dns2,
                ipMode=ip_mode,
                vlan=None
            )
            
        except Exception as e:
            logger.error(f"Error getting network info: {e}")
            return NetworkInfo(
                hostname="unknown",
                ipAddress="0.0.0.0",
                gateway="0.0.0.0",
                interface="unknown",
                dns1="8.8.8.8",
                dns2="8.8.4.4",
                ipMode="dhcp",
                vlan=None
            )
    
    @staticmethod
    async def update_network_config(config: Dict[str, Any]) -> bool:
        """Update network configuration (requires sudo)."""
        try:
            # Update hostname if provided
            if config.get('hostname'):
                # This would require sudo
                logger.info(f"Would update hostname to: {config['hostname']}")
                # subprocess.run(['sudo', 'hostnamectl', 'set-hostname', config['hostname']])
            
            # Update network settings
            if config.get('ipMode') == 'static':
                logger.info(f"Would set static IP: {config.get('ipAddress')}")
                # This would require NetworkManager commands with sudo
                # subprocess.run(['sudo', 'nmcli', 'con', 'mod', ...])
            
            # For now, just save to settings file
            settings = SystemService.load_settings()
            for key, value in config.items():
                if hasattr(settings, key):
                    setattr(settings, key, value)
            
            return SystemService.save_settings(settings)
            
        except Exception as e:
            logger.error(f"Error updating network config: {e}")
            return False
    
    @staticmethod
    async def apply_system_settings(config: Dict[str, Any]) -> bool:
        """Apply system settings like WiFi, NPU, performance."""
        try:
            # WiFi configuration
            if 'wifiEnabled' in config:
                if config['wifiEnabled'] and config.get('wifiSSID'):
                    logger.info(f"Would enable WiFi with SSID: {config['wifiSSID']}")
                    # This would require NetworkManager commands
                    # subprocess.run(['sudo', 'nmcli', 'dev', 'wifi', 'connect', ...])
                else:
                    logger.info("Would disable WiFi")
                    # subprocess.run(['sudo', 'nmcli', 'radio', 'wifi', 'off'])
            
            # NPU power mode
            if 'npuPowerMode' in config:
                logger.info(f"Would set NPU power mode to: {config['npuPowerMode']}")
                # This would require NPU-specific commands
                # echo performance > /sys/class/accel/accel0/device/power_profile
            
            # CPU governor
            if 'cpuGovernor' in config:
                logger.info(f"Would set CPU governor to: {config['cpuGovernor']}")
                # subprocess.run(['sudo', 'cpupower', 'frequency-set', '-g', config['cpuGovernor']])
            
            # Save all settings
            settings = SystemService.load_settings()
            for key, value in config.items():
                if hasattr(settings, key):
                    setattr(settings, key, value)
            
            return SystemService.save_settings(settings)
            
        except Exception as e:
            logger.error(f"Error applying system settings: {e}")
            return False
    
    @staticmethod
    async def test_authentik_connection(
        authentik_url: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str
    ) -> AuthentikTestResponse:
        """Test Authentik connection."""
        try:
            # Test connection to Authentik
            async with aiohttp.ClientSession() as session:
                # Try to get OpenID configuration
                discovery_url = f"{authentik_url}/application/o/.well-known/openid-configuration"
                
                async with session.get(discovery_url, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        
                        # Verify expected endpoints exist
                        if 'authorization_endpoint' in data and 'token_endpoint' in data:
                            return AuthentikTestResponse(
                                success=True,
                                message="Successfully connected to Authentik",
                                details={
                                    "authorization_endpoint": data.get('authorization_endpoint'),
                                    "token_endpoint": data.get('token_endpoint'),
                                    "issuer": data.get('issuer')
                                }
                            )
                        else:
                            return AuthentikTestResponse(
                                success=False,
                                message="Invalid Authentik response - missing required endpoints",
                                details=data
                            )
                    else:
                        return AuthentikTestResponse(
                            success=False,
                            message=f"Failed to connect to Authentik - HTTP {response.status}",
                            details={"status": response.status}
                        )
                        
        except asyncio.TimeoutError:
            return AuthentikTestResponse(
                success=False,
                message="Connection to Authentik timed out",
                details=None
            )
        except Exception as e:
            return AuthentikTestResponse(
                success=False,
                message=f"Error connecting to Authentik: {str(e)}",
                details=None
            )