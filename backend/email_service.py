#!/usr/bin/env python3
"""
Email Notification Service for Meeting Summaries
"""

import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from typing import List, Dict, Any, Optional
from datetime import datetime
import os
from jinja2 import Template
import asyncio
from pathlib import Path

logger = logging.getLogger(__name__)

class EmailService:
    """Service for sending email notifications with meeting summaries"""
    
    def __init__(self):
        self.smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))
        self.smtp_user = os.getenv("SMTP_USER", "")
        self.smtp_password = os.getenv("SMTP_PASSWORD", "")
        self.smtp_from = os.getenv("SMTP_FROM", self.smtp_user)
        self.use_tls = os.getenv("SMTP_USE_TLS", "true").lower() == "true"
        
        # Email templates
        self.templates = {
            "meeting_summary": self._load_meeting_summary_template(),
            "weekly_digest": self._load_weekly_digest_template(),
            "action_items": self._load_action_items_template()
        }
    
    def _load_meeting_summary_template(self) -> str:
        """Load or create meeting summary email template"""
        return """
<!DOCTYPE html>
<html>
<head>
    <style>
        body { font-family: Arial, sans-serif; color: #333; background-color: #f5f5f5; }
        .container { max-width: 600px; margin: 0 auto; background: white; padding: 20px; border-radius: 10px; }
        .header { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; border-radius: 10px 10px 0 0; text-align: center; }
        .logo { font-size: 24px; font-weight: bold; }
        .subtitle { font-size: 14px; opacity: 0.9; margin-top: 5px; }
        .content { padding: 20px 0; }
        .section { margin-bottom: 30px; }
        .section-title { font-size: 18px; font-weight: bold; margin-bottom: 10px; color: #667eea; }
        .info-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 15px; margin-bottom: 20px; }
        .info-item { background: #f8f9fa; padding: 15px; border-radius: 8px; }
        .info-label { font-size: 12px; color: #666; text-transform: uppercase; }
        .info-value { font-size: 16px; font-weight: bold; color: #333; margin-top: 5px; }
        .transcript-box { background: #f8f9fa; padding: 20px; border-radius: 8px; border-left: 4px solid #667eea; }
        .speaker-segment { margin-bottom: 15px; }
        .speaker-name { font-weight: bold; color: #667eea; margin-bottom: 5px; }
        .speaker-text { color: #555; line-height: 1.6; }
        .summary-box { background: #e7f3ff; padding: 20px; border-radius: 8px; margin-bottom: 20px; }
        .action-item { background: #fff3cd; padding: 12px; border-radius: 6px; margin-bottom: 10px; border-left: 4px solid #ffc107; }
        .footer { text-align: center; color: #999; font-size: 12px; margin-top: 30px; padding-top: 20px; border-top: 1px solid #eee; }
        .button { display: inline-block; padding: 12px 24px; background: #667eea; color: white; text-decoration: none; border-radius: 6px; margin: 10px 5px; }
        .button:hover { background: #5a67d8; }
        .stats { display: flex; justify-content: space-around; margin: 20px 0; }
        .stat { text-align: center; }
        .stat-value { font-size: 24px; font-weight: bold; color: #667eea; }
        .stat-label { font-size: 12px; color: #666; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="logo">🦄 Meeting-Ops</div>
            <div class="subtitle">Meeting Recording & Intelligence Platform</div>
        </div>
        
        <div class="content">
            <h1 style="color: #333; margin-bottom: 20px;">Meeting Summary</h1>
            
            <div class="info-grid">
                <div class="info-item">
                    <div class="info-label">Meeting Title</div>
                    <div class="info-value">{{ meeting_name }}</div>
                </div>
                <div class="info-item">
                    <div class="info-label">Date & Time</div>
                    <div class="info-value">{{ meeting_date }}</div>
                </div>
                <div class="info-item">
                    <div class="info-label">Duration</div>
                    <div class="info-value">{{ duration }}</div>
                </div>
                <div class="info-item">
                    <div class="info-label">Participants</div>
                    <div class="info-value">{{ num_speakers }} speakers</div>
                </div>
            </div>
            
            <div class="stats">
                <div class="stat">
                    <div class="stat-value">{{ transcription_count }}</div>
                    <div class="stat-label">Transcriptions</div>
                </div>
                <div class="stat">
                    <div class="stat-value">{{ confidence }}%</div>
                    <div class="stat-label">Avg Confidence</div>
                </div>
                <div class="stat">
                    <div class="stat-value">{{ processing_time }}s</div>
                    <div class="stat-label">Processing Time</div>
                </div>
            </div>
            
            {% if summary %}
            <div class="section">
                <div class="section-title">AI Summary</div>
                <div class="summary-box">
                    {{ summary | safe }}
                </div>
            </div>
            {% endif %}
            
            {% if action_items %}
            <div class="section">
                <div class="section-title">Action Items</div>
                {% for item in action_items %}
                <div class="action-item">
                    ✓ {{ item }}
                </div>
                {% endfor %}
            </div>
            {% endif %}
            
            {% if key_topics %}
            <div class="section">
                <div class="section-title">Key Topics Discussed</div>
                <ul style="color: #555; line-height: 1.8;">
                    {% for topic in key_topics %}
                    <li>{{ topic }}</li>
                    {% endfor %}
                </ul>
            </div>
            {% endif %}
            
            {% if transcript_segments %}
            <div class="section">
                <div class="section-title">Transcript Highlights</div>
                <div class="transcript-box">
                    {% for segment in transcript_segments[:5] %}
                    <div class="speaker-segment">
                        <div class="speaker-name">{{ segment.speaker }} ({{ segment.time }})</div>
                        <div class="speaker-text">{{ segment.text }}</div>
                    </div>
                    {% endfor %}
                    {% if transcript_segments|length > 5 %}
                    <p style="text-align: center; color: #666; margin-top: 15px;">
                        ... and {{ transcript_segments|length - 5 }} more segments
                    </p>
                    {% endif %}
                </div>
            </div>
            {% endif %}
            
            <div style="text-align: center; margin-top: 30px;">
                {% if view_url %}
                <a href="{{ view_url }}" class="button">View Full Transcript</a>
                {% endif %}
                {% if download_url %}
                <a href="{{ download_url }}" class="button" style="background: #28a745;">Download Recording</a>
                {% endif %}
            </div>
        </div>
        
        <div class="footer">
            <p>This is an automated email from Meeting-Ops</p>
            <p>Part of the Unicorn Commander Suite | Powered by NPU Acceleration</p>
            <p style="margin-top: 10px;">
                <a href="{{ unsubscribe_url }}" style="color: #999;">Unsubscribe</a> | 
                <a href="{{ preferences_url }}" style="color: #999;">Email Preferences</a>
            </p>
        </div>
    </div>
</body>
</html>
        """
    
    def _load_weekly_digest_template(self) -> str:
        """Load weekly digest template"""
        return """
<!DOCTYPE html>
<html>
<head>
    <style>
        body { font-family: Arial, sans-serif; color: #333; background-color: #f5f5f5; }
        .container { max-width: 700px; margin: 0 auto; background: white; padding: 20px; border-radius: 10px; }
        .header { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 40px; border-radius: 10px 10px 0 0; text-align: center; }
        .meeting-card { background: #f8f9fa; padding: 20px; border-radius: 8px; margin-bottom: 15px; border-left: 4px solid #667eea; }
        .meeting-title { font-size: 16px; font-weight: bold; color: #333; margin-bottom: 5px; }
        .meeting-info { font-size: 14px; color: #666; }
        .stats-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 20px; margin: 30px 0; }
        .stat-card { background: #f8f9fa; padding: 20px; border-radius: 8px; text-align: center; }
        .stat-value { font-size: 28px; font-weight: bold; color: #667eea; }
        .stat-label { font-size: 12px; color: #666; margin-top: 5px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>Your Weekly Meeting Digest</h1>
            <p>{{ week_start }} - {{ week_end }}</p>
        </div>
        
        <div class="content">
            <div class="stats-grid">
                <div class="stat-card">
                    <div class="stat-value">{{ total_meetings }}</div>
                    <div class="stat-label">Total Meetings</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{{ total_hours }}h</div>
                    <div class="stat-label">Meeting Time</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{{ total_participants }}</div>
                    <div class="stat-label">Participants</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{{ action_items_count }}</div>
                    <div class="stat-label">Action Items</div>
                </div>
            </div>
            
            <h2 style="color: #667eea; margin-top: 40px;">Meeting Summaries</h2>
            {% for meeting in meetings %}
            <div class="meeting-card">
                <div class="meeting-title">{{ meeting.name }}</div>
                <div class="meeting-info">
                    {{ meeting.date }} | {{ meeting.duration }} | {{ meeting.participants }} participants
                </div>
                {% if meeting.summary %}
                <p style="margin-top: 10px; color: #555;">{{ meeting.summary }}</p>
                {% endif %}
            </div>
            {% endfor %}
        </div>
    </div>
</body>
</html>
        """
    
    def _load_action_items_template(self) -> str:
        """Load action items reminder template"""
        return """
<!DOCTYPE html>
<html>
<head>
    <style>
        body { font-family: Arial, sans-serif; color: #333; }
        .container { max-width: 600px; margin: 0 auto; padding: 20px; }
        .header { background: #ffc107; color: #333; padding: 20px; border-radius: 8px; text-align: center; margin-bottom: 20px; }
        .action-item { background: #fff3cd; padding: 15px; border-radius: 6px; margin-bottom: 15px; border-left: 4px solid #ffc107; }
        .item-title { font-weight: bold; margin-bottom: 5px; }
        .item-details { font-size: 14px; color: #666; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2>Action Items Reminder</h2>
            <p>You have {{ total_items }} pending action items</p>
        </div>
        
        {% for item in action_items %}
        <div class="action-item">
            <div class="item-title">{{ item.title }}</div>
            <div class="item-details">
                From: {{ item.meeting_name }} ({{ item.meeting_date }})<br>
                Assigned to: {{ item.assignee }}
            </div>
        </div>
        {% endfor %}
    </div>
</body>
</html>
        """
    
    async def send_meeting_summary(self, 
                                 recipients: List[str],
                                 meeting_data: Dict[str, Any],
                                 attachments: Optional[List[Dict[str, Any]]] = None) -> bool:
        """Send meeting summary email to recipients"""
        try:
            # Prepare template data
            template_data = {
                "meeting_name": meeting_data.get("name", "Untitled Meeting"),
                "meeting_date": meeting_data.get("date", datetime.now().strftime("%B %d, %Y at %I:%M %p")),
                "duration": self._format_duration(meeting_data.get("duration_seconds", 0)),
                "num_speakers": meeting_data.get("num_speakers", 0),
                "transcription_count": meeting_data.get("transcription_count", 0),
                "confidence": int(meeting_data.get("avg_confidence", 0) * 100),
                "processing_time": round(meeting_data.get("processing_time", 0), 1),
                "summary": meeting_data.get("summary", ""),
                "action_items": meeting_data.get("action_items", []),
                "key_topics": meeting_data.get("key_topics", []),
                "transcript_segments": meeting_data.get("transcript_segments", []),
                "view_url": meeting_data.get("view_url", ""),
                "download_url": meeting_data.get("download_url", ""),
                "unsubscribe_url": "#",
                "preferences_url": "#"
            }
            
            # Render HTML template
            template = Template(self.templates["meeting_summary"])
            html_content = template.render(**template_data)
            
            # Create message
            msg = MIMEMultipart('mixed')
            msg['Subject'] = f"Meeting Summary: {template_data['meeting_name']}"
            msg['From'] = self.smtp_from
            msg['To'] = ', '.join(recipients)
            
            # Add HTML body
            html_part = MIMEText(html_content, 'html')
            msg.attach(html_part)
            
            # Add attachments if provided
            if attachments:
                for attachment in attachments:
                    self._add_attachment(msg, attachment)
            
            # Send email
            await self._send_email(recipients, msg)
            
            logger.info(f"✅ Meeting summary sent to {len(recipients)} recipients")
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to send meeting summary: {e}")
            return False
    
    async def send_weekly_digest(self,
                               recipients: List[str],
                               week_data: Dict[str, Any]) -> bool:
        """Send weekly meeting digest"""
        try:
            template = Template(self.templates["weekly_digest"])
            html_content = template.render(**week_data)
            
            msg = MIMEMultipart()
            msg['Subject'] = f"Weekly Meeting Digest - {week_data['week_start']}"
            msg['From'] = self.smtp_from
            msg['To'] = ', '.join(recipients)
            
            html_part = MIMEText(html_content, 'html')
            msg.attach(html_part)
            
            await self._send_email(recipients, msg)
            
            logger.info(f"✅ Weekly digest sent to {len(recipients)} recipients")
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to send weekly digest: {e}")
            return False
    
    async def send_action_items_reminder(self,
                                       recipients: List[str],
                                       action_items: List[Dict[str, Any]]) -> bool:
        """Send action items reminder"""
        try:
            template_data = {
                "total_items": len(action_items),
                "action_items": action_items
            }
            
            template = Template(self.templates["action_items"])
            html_content = template.render(**template_data)
            
            msg = MIMEMultipart()
            msg['Subject'] = f"Action Items Reminder - {len(action_items)} pending items"
            msg['From'] = self.smtp_from
            msg['To'] = ', '.join(recipients)
            
            html_part = MIMEText(html_content, 'html')
            msg.attach(html_part)
            
            await self._send_email(recipients, msg)
            
            logger.info(f"✅ Action items reminder sent to {len(recipients)} recipients")
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to send action items reminder: {e}")
            return False
    
    def _add_attachment(self, msg: MIMEMultipart, attachment: Dict[str, Any]):
        """Add attachment to email message"""
        try:
            part = MIMEBase('application', 'octet-stream')
            
            if 'content' in attachment:
                part.set_payload(attachment['content'])
            elif 'file_path' in attachment:
                with open(attachment['file_path'], 'rb') as f:
                    part.set_payload(f.read())
            else:
                return
            
            encoders.encode_base64(part)
            part.add_header(
                'Content-Disposition',
                f'attachment; filename= {attachment.get("filename", "attachment")}'
            )
            msg.attach(part)
            
        except Exception as e:
            logger.error(f"Failed to add attachment: {e}")
    
    async def _send_email(self, recipients: List[str], msg: MIMEMultipart):
        """Send email using SMTP"""
        if not self.smtp_user or not self.smtp_password:
            logger.warning("⚠️ SMTP not configured - email not sent")
            logger.info(f"Would have sent email to: {recipients}")
            logger.debug(f"Subject: {msg['Subject']}")
            return
        
        # Send email in thread pool to avoid blocking
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._send_email_sync, recipients, msg)
    
    def _send_email_sync(self, recipients: List[str], msg: MIMEMultipart):
        """Synchronous email sending"""
        with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
            if self.use_tls:
                server.starttls()
            
            server.login(self.smtp_user, self.smtp_password)
            server.send_message(msg, self.smtp_from, recipients)
    
    def _format_duration(self, seconds: int) -> str:
        """Format duration in human-readable format"""
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        
        if hours > 0:
            return f"{hours}h {minutes}m"
        else:
            return f"{minutes}m"
    
    def validate_config(self) -> bool:
        """Validate email configuration"""
        if not self.smtp_host or not self.smtp_port:
            logger.warning("SMTP host/port not configured")
            return False
        
        if not self.smtp_user or not self.smtp_password:
            logger.warning("SMTP credentials not configured")
            return False
        
        return True


# FastAPI endpoints
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, EmailStr
from typing import List, Optional

email_router = APIRouter(prefix="/api/email", tags=["email"])

class EmailRequest(BaseModel):
    recipients: List[EmailStr]
    session_id: Optional[str] = None
    include_transcript: bool = False
    include_audio: bool = False

@email_router.post("/send-summary")
async def send_meeting_summary_endpoint(
    request: EmailRequest,
    background_tasks: BackgroundTasks
):
    """Send meeting summary email"""
    email_service = EmailService()
    
    if not email_service.validate_config():
        raise HTTPException(status_code=503, detail="Email service not configured")
    
    # Mock meeting data for testing
    meeting_data = {
        "name": "Team Standup Meeting",
        "date": datetime.now().strftime("%B %d, %Y at %I:%M %p"),
        "duration_seconds": 1800,
        "num_speakers": 3,
        "transcription_count": 15,
        "avg_confidence": 0.92,
        "processing_time": 2.5,
        "summary": "The team discussed the progress on the Q1 roadmap, identified blockers in the authentication module, and planned the sprint retrospective for next week.",
        "action_items": [
            "John to review and merge the authentication PR by EOD",
            "Sarah to update the project timeline with new estimates",
            "Team to prepare feedback for sprint retrospective"
        ],
        "key_topics": [
            "Q1 Roadmap Progress",
            "Authentication Module Status",
            "Sprint Retrospective Planning",
            "Resource Allocation"
        ]
    }
    
    # Add to background tasks
    background_tasks.add_task(
        email_service.send_meeting_summary,
        request.recipients,
        meeting_data
    )
    
    return {"status": "queued", "recipients": len(request.recipients)}

@email_router.post("/test")
async def test_email_configuration():
    """Test email configuration"""
    email_service = EmailService()
    
    if not email_service.validate_config():
        return {
            "status": "not_configured",
            "message": "Email service requires SMTP configuration",
            "required_env_vars": [
                "SMTP_HOST",
                "SMTP_PORT", 
                "SMTP_USER",
                "SMTP_PASSWORD"
            ]
        }
    
    return {
        "status": "configured",
        "smtp_host": email_service.smtp_host,
        "smtp_port": email_service.smtp_port,
        "smtp_from": email_service.smtp_from,
        "use_tls": email_service.use_tls
    }