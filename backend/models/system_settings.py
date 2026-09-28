"""System settings models for Meeting-Ops."""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from enum import Enum


class AudioDevice(BaseModel):
    """Audio device information."""
    id: str = Field(..., description="Device ID (e.g., 'hw:0,0')")
    name: str = Field(..., description="Device name")
    label: str = Field(..., description="Human-readable label")
    sampleRates: List[int] = Field(default_factory=list, description="Supported sample rates")
    channels: int = Field(..., description="Number of channels")
    isDefault: bool = Field(default=False, description="Is default device")
    isUSB: bool = Field(default=False, description="Is USB device")


class AudioVolumeRequest(BaseModel):
    """Audio volume control request."""
    device: str = Field(..., description="Device ID (e.g., 'hw:0,0')")
    volume: int = Field(..., ge=0, le=100, description="Volume percentage (0-100)")


class AudioVolumeResponse(BaseModel):
    """Audio volume response."""
    device: str
    volume: int = Field(..., description="Current volume percentage")
    success: bool = True


class NetworkInfo(BaseModel):
    """Network configuration information."""
    hostname: str = Field(..., description="System hostname")
    ipAddress: str = Field(..., description="IP address")
    gateway: str = Field(..., description="Default gateway")
    interface: str = Field(..., description="Network interface")
    dns1: str = Field(..., description="Primary DNS")
    dns2: str = Field(..., description="Secondary DNS")
    ipMode: str = Field(default="dhcp", description="IP mode (dhcp/static)")
    vlan: Optional[int] = Field(None, description="VLAN ID")


class NetworkConfigRequest(BaseModel):
    """Network configuration update request."""
    hostname: Optional[str] = None
    ipMode: str = Field(default="dhcp", pattern="^(dhcp|static)$")
    ipAddress: Optional[str] = None
    gateway: Optional[str] = None
    dns1: Optional[str] = None
    dns2: Optional[str] = None
    vlan: Optional[int] = None


class SystemApplyRequest(BaseModel):
    """System settings apply request."""
    wifiEnabled: bool = False
    wifiSSID: Optional[str] = None
    wifiPassword: Optional[str] = None
    npuPowerMode: str = Field(default="balanced", pattern="^(performance|balanced|powersave)$")
    cpuGovernor: str = Field(default="ondemand", pattern="^(performance|ondemand|powersave)$")
    enableAutoBackup: bool = True
    backupSchedule: str = Field(default="daily", pattern="^(hourly|daily|weekly)$")


class AuthentikTestRequest(BaseModel):
    """Authentik connection test request."""
    authentikUrl: str = Field(..., description="Authentik base URL")
    clientId: str = Field(..., description="OAuth2 client ID")
    clientSecret: str = Field(..., description="OAuth2 client secret")
    redirectUri: str = Field(..., description="OAuth2 redirect URI")


class AuthentikTestResponse(BaseModel):
    """Authentik connection test response."""
    success: bool
    message: str
    details: Optional[Dict[str, Any]] = None


class AllSettings(BaseModel):
    """Complete system settings."""
    # Recording settings
    recordingQuality: str = Field(default="high", pattern="^(low|medium|high)$")
    defaultMicrophone: str = Field(default="hw:0,0")
    microphoneVolume: int = Field(default=75, ge=0, le=100)
    enableNoiseReduction: bool = True
    enableAutoGainControl: bool = True
    
    # Transcription settings
    transcriptionModel: str = Field(default="large-v3")
    transcriptionLanguage: str = Field(default="en")
    enableSpeakerDiarization: bool = True
    maxSpeakers: int = Field(default=4, ge=1, le=10)
    enableVAD: bool = True
    vadThreshold: float = Field(default=0.5, ge=0.1, le=1.0)
    
    # AI settings
    enableLiveAI: bool = True
    aiProvider: str = Field(default="ollama", pattern="^(ollama|openai|anthropic|granite)$")
    aiModel: str = Field(default="granite3.3:8b")
    aiTemperature: float = Field(default=0.7, ge=0.0, le=1.0)
    enableAutoSummary: bool = True
    enableLiveSummarization: bool = Field(default=True, description="Enable automatic summarization during recording")
    summaryTriggerMode: str = Field(default="word_count", pattern="^(time|word_count)$", description="Trigger mode for summaries")
    summaryUpdateInterval: int = Field(default=60, description="Seconds between summary updates (time mode)")
    summaryWordCountInterval: int = Field(default=500, ge=100, le=5000, description="Words between summary updates (word_count mode)")
    enableFinalSummary: bool = Field(default=True, description="Generate comprehensive summary when recording stops")
    
    # Network settings
    hostname: str = Field(default="meeting-ops")
    ipMode: str = Field(default="dhcp", pattern="^(dhcp|static)$")
    ipAddress: Optional[str] = None
    gateway: Optional[str] = None
    dns1: str = Field(default="8.8.8.8")
    dns2: str = Field(default="8.8.4.4")
    
    # WiFi settings
    wifiEnabled: bool = False
    wifiSSID: Optional[str] = None
    wifiPassword: Optional[str] = None
    
    # System settings
    npuEnabled: bool = True
    npuPowerMode: str = Field(default="balanced", pattern="^(performance|balanced|powersave)$")
    cpuGovernor: str = Field(default="ondemand", pattern="^(performance|ondemand|powersave)$")
    enableAutoBackup: bool = True
    backupSchedule: str = Field(default="daily", pattern="^(hourly|daily|weekly)$")
    backupLocation: str = Field(default="/var/backups/meeting-ops")
    
    # Authentication settings
    authMethod: str = Field(default="local", pattern="^(local|authentik|oauth2|saml)$")
    authentikUrl: Optional[str] = None
    authentikClientId: Optional[str] = None
    authentikClientSecret: Optional[str] = None
    authentikRedirectUri: Optional[str] = None
    
    # Export settings
    defaultExportFormat: str = Field(default="txt", pattern="^(txt|pdf|docx|json)$")
    includeTimestamps: bool = True
    includeSpeakerLabels: bool = True
    includeAISummary: bool = True
    
    # Email settings
    emailEnabled: bool = False
    smtpServer: Optional[str] = None
    smtpPort: int = Field(default=587, ge=1, le=65535)
    smtpUsername: Optional[str] = None
    smtpPassword: Optional[str] = None
    smtpUseTLS: bool = True
    emailFrom: Optional[str] = None
    
    # Storage settings
    maxStorageGB: int = Field(default=500, ge=10)
    autoDeleteDays: int = Field(default=0, description="0 = never delete")
    compressOldRecordings: bool = True
    compressionDays: int = Field(default=30)
    
    # UI settings
    theme: str = Field(default="dark", pattern="^(light|dark|auto)$")
    language: str = Field(default="en", pattern="^(en|es|fr|de|ja|zh)$")
    timezone: str = Field(default="UTC")
    dateFormat: str = Field(default="MM/DD/YYYY")
    timeFormat: str = Field(default="12h", pattern="^(12h|24h)$")