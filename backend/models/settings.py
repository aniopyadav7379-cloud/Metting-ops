"""
Settings models for Meeting-Ops application configuration
"""
from pydantic import BaseModel, Field
from typing import Optional, Dict, List, Any
from datetime import datetime
from enum import Enum


class NotificationSettings(BaseModel):
    """Notification preferences for various events"""
    onRecordingStart: bool = True
    onRecordingStop: bool = True
    onTranscriptionComplete: bool = True
    onReportGenerated: bool = False


class StorageSettings(BaseModel):
    """Storage and retention settings"""
    retentionDays: int = Field(default=90, ge=1, le=365)
    autoArchive: bool = True
    compressionEnabled: bool = False


class ApplicationSettings(BaseModel):
    """Main application settings model"""
    defaultLLMProvider: str = Field(default="ollama", pattern="^(ollama|openai|anthropic|granite)$")
    transcriptionLanguage: str = Field(default="en", min_length=2, max_length=5)
    transcriptionModel: str = Field(default="large-v3")
    autoSaveInterval: int = Field(default=30, ge=10, le=300)
    enableNPUAcceleration: bool = True
    audioSampleRate: int = Field(default=16000, ge=8000, le=48000)
    audioInputDevice: str = "default"
    chunkDuration: int = Field(default=10, ge=5, le=30)
    enableVAD: bool = True
    vadThreshold: float = Field(default=0.5, ge=0.0, le=1.0)
    maxRecordingDuration: int = Field(default=7200, ge=60, le=28800)  # 1 min to 8 hours
    enableAutoTranscription: bool = True
    enableSpeakerDiarization: bool = True
    maxSpeakers: int = Field(default=4, ge=1, le=10)
    notificationSettings: NotificationSettings = Field(default_factory=NotificationSettings)
    storageSettings: StorageSettings = Field(default_factory=StorageSettings)


class AgentConfig(BaseModel):
    """Configuration for individual AI agents"""
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    maxTokens: int = Field(default=4096, ge=100, le=32000)
    topP: float = Field(default=0.9, ge=0.0, le=1.0)
    frequencyPenalty: float = Field(default=0.0, ge=-2.0, le=2.0)
    presencePenalty: float = Field(default=0.0, ge=-2.0, le=2.0)
    timeout: int = Field(default=30, ge=5, le=300)


class AgentFeatures(BaseModel):
    """Feature flags for AI agents"""
    autoSummarization: bool = True
    actionItemExtraction: bool = True
    sentimentAnalysis: bool = False
    topicModeling: bool = False
    speakerIdentification: bool = True
    keywordExtraction: bool = True


class ProcessingRules(BaseModel):
    """Rules for AI processing"""
    minTranscriptLength: int = Field(default=100, ge=10)
    maxProcessingTime: int = Field(default=300, ge=30)
    retryAttempts: int = Field(default=3, ge=1, le=10)
    chunkSize: int = Field(default=4000, ge=500, le=8000)


class PromptTemplate(BaseModel):
    """AI prompt template"""
    id: str
    name: str
    description: str
    prompt: str
    isActive: bool = True
    category: str
    createdAt: Optional[datetime] = None
    updatedAt: Optional[datetime] = None


class AgentSettings(BaseModel):
    """Complete agent settings model"""
    defaultAgent: str = "summarization"
    agents: Dict[str, AgentConfig] = Field(default_factory=lambda: {
        "transcription": AgentConfig(temperature=0.3),
        "summarization": AgentConfig(temperature=0.5),
        "analysis": AgentConfig(temperature=0.7),
        "extraction": AgentConfig(temperature=0.3)
    })
    prompts: List[PromptTemplate] = Field(default_factory=list)
    features: AgentFeatures = Field(default_factory=AgentFeatures)
    processingRules: ProcessingRules = Field(default_factory=ProcessingRules)


class VocabularyTerm(BaseModel):
    """Vocabulary term model"""
    id: Optional[str] = None
    term: str
    expansion: str
    category: str = "general"
    priority: int = Field(default=0, ge=0, le=100)
    contextHints: List[str] = Field(default_factory=list)
    isActive: bool = True
    createdAt: Optional[datetime] = None
    updatedAt: Optional[datetime] = None


class VocabularySet(BaseModel):
    """Vocabulary set model"""
    id: Optional[str] = None
    name: str
    description: str
    industry: str = "general"
    isActive: bool = True
    termCount: int = 0
    lastUpdated: Optional[datetime] = None


class VocabularyResponse(BaseModel):
    """Response model for vocabulary endpoints"""
    terms: List[VocabularyTerm]
    sets: List[VocabularySet]


class LiveTemplateContent(BaseModel):
    """Live template content during meeting"""
    executive_summary: Optional[str] = None
    action_items: Optional[str] = None
    key_decisions: Optional[str] = None
    participants: Optional[str] = None
    next_steps: Optional[str] = None
    risks_issues: Optional[str] = None


class GenerateTemplateRequest(BaseModel):
    """Request to generate a template"""
    session_id: str
    template_id: str


class UpdateTemplateRequest(BaseModel):
    """Request to update template content"""
    content: str


class SettingsUpdateResponse(BaseModel):
    """Generic response for settings updates"""
    status: str = "success"
    message: str = "Settings updated"