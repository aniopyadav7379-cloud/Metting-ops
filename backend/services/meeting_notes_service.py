"""
Meeting Notes Service using Decoupled Model Configuration
"""
import json
import asyncio
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone
import hashlib

from services.model_providers import ModelProviderFactory, ModelParameters

logger = logging.getLogger(__name__)


class MeetingNotesService:
    """Service for generating AI-powered meeting notes using any configured model"""
    
    def __init__(self, model_config: Dict[str, Any] = None, redis_client = None):
        """
        Initialize meeting notes service
        
        Args:
            model_config: Model configuration dict (provider, model_name, etc.)
            redis_client: Optional Redis client for caching
        """
        # Default to Granite 3.3 2B on llama.cpp Vulkan
        self.model_config = model_config or {
            "provider": "openai_compatible",
            "model_name": "granite-3.3-2b-instruct",
            "api_endpoint": "http://localhost:11437/v1"
        }
        
        self.provider = ModelProviderFactory.create_from_config(self.model_config)
        self.redis = redis_client
        self.cache_ttl = 3600  # 1 hour cache
        
        # Parameters optimized for meeting notes generation
        self.generation_params = ModelParameters(
            temperature=0.7,  # Balanced creativity/consistency
            max_tokens=2048,  # Enough for detailed notes
            top_p=0.9,
            provider_params={
                "num_ctx": 32768,  # Large context for full transcripts
                "num_gpu": 99,    # Use all GPU layers
                "repeat_penalty": 1.1
            }
        )
    
    def _get_cache_key(self, prefix: str, content: str) -> str:
        """Generate cache key from content"""
        content_hash = hashlib.md5(content.encode()).hexdigest()[:8]
        return f"meeting_notes:{prefix}:{content_hash}"
    
    async def test_connection(self) -> bool:
        """Test if the model provider is available"""
        try:
            return await self.provider.health_check()
        except Exception as e:
            logger.error(f"Connection test failed: {e}")
            return False
    
    async def generate_meeting_notes(self, transcript: str, use_cache: bool = True) -> Dict[str, Any]:
        """
        Generate structured meeting notes from transcript
        
        Args:
            transcript: Meeting transcript text
            use_cache: Whether to use cached results
            
        Returns:
            Dictionary with meeting notes structure
        """
        if not transcript.strip():
            return self._empty_notes()
        
        # Check cache first
        if use_cache and self.redis:
            try:
                cache_key = self._get_cache_key("notes", transcript)
                cached = await self.redis.get(cache_key)
                if cached:
                    logger.info("Returning cached meeting notes")
                    return json.loads(cached)
            except Exception as e:
                logger.warning(f"Cache read failed: {e}")
        
        # Generate notes with model
        try:
            prompt = self._build_notes_prompt(transcript)
            system_prompt = "You are an expert meeting assistant. Extract structured information from meeting transcripts and format it as valid JSON."
            
            response = await self.provider.generate(
                prompt=prompt,
                system_prompt=system_prompt,
                parameters=self.generation_params
            )
            
            # Parse the response
            notes = self._parse_notes_response(response.content)
            
            # Cache the result
            if self.redis and use_cache:
                try:
                    cache_key = self._get_cache_key("notes", transcript)
                    await self.redis.setex(cache_key, self.cache_ttl, json.dumps(notes))
                except Exception as e:
                    logger.warning(f"Cache write failed: {e}")
            
            logger.info(f"Generated meeting notes using {response.model}")
            return notes
            
        except Exception as e:
            logger.error(f"Error generating meeting notes: {e}")
            return self._empty_notes_with_error(str(e))
    
    async def generate_incremental_notes(
        self, 
        new_transcript: str, 
        previous_notes: Dict[str, Any],
        use_cache: bool = True
    ) -> Dict[str, Any]:
        """
        Generate incremental notes by updating previous notes with new content
        
        Args:
            new_transcript: New transcript segment
            previous_notes: Previously generated notes
            use_cache: Whether to use cached results
            
        Returns:
            Updated meeting notes
        """
        if not new_transcript.strip():
            return previous_notes
        
        try:
            prompt = self._build_incremental_prompt(new_transcript, previous_notes)
            system_prompt = "You are updating meeting notes with new information. Merge new content with existing notes while avoiding duplication."
            
            response = await self.provider.generate(
                prompt=prompt,
                system_prompt=system_prompt,
                parameters=self.generation_params
            )
            
            # Parse and return updated notes
            updated_notes = self._parse_notes_response(response.content)
            
            logger.info(f"Updated incremental notes using {response.model}")
            return updated_notes
            
        except Exception as e:
            logger.error(f"Error generating incremental notes: {e}")
            return previous_notes  # Return previous notes if update fails
    
    def _build_notes_prompt(self, transcript: str) -> str:
        """Build the prompt for initial notes generation"""
        return f"""
        Analyze this meeting transcript and generate structured notes.
        Extract actionable information and key discussion points.
        
        TRANSCRIPT:
        {transcript}
        
        Generate a JSON response with this exact structure:
        {{
            "summary": "Brief 2-3 sentence meeting summary",
            "keyPoints": ["Key point 1", "Key point 2"],
            "decisions": ["Decision 1", "Decision 2"],
            "actionItems": [
                {{"task": "Task description", "owner": "Person name or 'Unassigned'", "deadline": "Date or 'TBD'", "priority": "high/medium/low"}}
            ],
            "questions": ["Unresolved question 1"],
            "nextSteps": ["Next step 1", "Next step 2"],
            "participants": ["Name 1", "Name 2"],
            "sentiment": "positive/neutral/negative",
            "topics": ["Topic 1", "Topic 2"],
            "lastUpdated": "{datetime.now(timezone.utc).isoformat()}",
            "isIncremental": false
        }}
        
        Guidelines:
        - Extract real information from the transcript only
        - Use empty arrays [] if information is not available
        - Keep responses concise and actionable
        - Identify speakers/participants when possible
        - Format as valid JSON only
        """
    
    def _build_incremental_prompt(self, new_transcript: str, previous_notes: Dict[str, Any]) -> str:
        """Build the prompt for incremental notes updates"""
        return f"""
        Update the existing meeting notes with new transcript content.
        Merge new information while avoiding duplication.
        
        EXISTING NOTES:
        {json.dumps(previous_notes, indent=2)}
        
        NEW TRANSCRIPT SEGMENT:
        {new_transcript}
        
        Return updated JSON with the same structure:
        - Add new key points, decisions, action items
        - Update summary to include new content
        - Merge participants lists
        - Add new topics if they emerge
        - Update sentiment if it changes
        - Set "isIncremental": true
        - Update "lastUpdated" timestamp
        
        Return only valid JSON with the complete updated structure.
        """
    
    def _parse_notes_response(self, response: str) -> Dict[str, Any]:
        """Parse the model response into structured notes"""
        try:
            # Try to extract JSON from response
            response = response.strip()
            
            # Look for JSON content
            if response.startswith('{') and response.endswith('}'):
                return json.loads(response)
            
            # Try to find JSON block in markdown-style response
            import re
            json_match = re.search(r'```json\s*(\{.*?\})\s*```', response, re.DOTALL)
            if json_match:
                return json.loads(json_match.group(1))
            
            # Try to find any JSON-like content
            json_match = re.search(r'(\{.*\})', response, re.DOTALL)
            if json_match:
                return json.loads(json_match.group(1))
            
            # If no valid JSON found, create structured notes from text
            return self._text_to_notes(response)
            
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error: {e}, Response: {response[:200]}...")
            return self._text_to_notes(response)
        except Exception as e:
            logger.error(f"Error parsing notes response: {e}")
            return self._empty_notes_with_error("Failed to parse AI response")
    
    def _text_to_notes(self, text: str) -> Dict[str, Any]:
        """Convert plain text response to structured notes as fallback"""
        return {
            "summary": text[:200] + "..." if len(text) > 200 else text,
            "keyPoints": [text[:100]] if text.strip() else [],
            "decisions": [],
            "actionItems": [],
            "questions": [],
            "nextSteps": [],
            "participants": [],
            "sentiment": "neutral",
            "topics": [],
            "lastUpdated": datetime.now(timezone.utc).isoformat(),
            "isIncremental": False
        }
    
    def _empty_notes(self) -> Dict[str, Any]:
        """Return empty notes structure"""
        return {
            "summary": "",
            "keyPoints": [],
            "decisions": [],
            "actionItems": [],
            "questions": [],
            "nextSteps": [],
            "participants": [],
            "sentiment": "neutral",
            "topics": [],
            "lastUpdated": datetime.now(timezone.utc).isoformat(),
            "isIncremental": False
        }
    
    def _empty_notes_with_error(self, error_msg: str) -> Dict[str, Any]:
        """Return empty notes with error message"""
        notes = self._empty_notes()
        notes["error"] = error_msg
        return notes


class MeetingNotesBuffer:
    """Buffer for accumulating transcript and managing note updates"""
    
    def __init__(self, session_id: str, service: MeetingNotesService):
        self.session_id = session_id
        self.service = service
        self.transcript_buffer = []
        self.last_notes = None
        self.last_update_time = datetime.now(timezone.utc)
        self.update_interval = 30  # seconds
        self.min_words = 50  # minimum words before generating notes
    
    def add_transcript(self, text: str):
        """Add new transcript text to buffer"""
        if text.strip():
            self.transcript_buffer.append({
                "text": text,
                "timestamp": datetime.now(timezone.utc)
            })
    
    def get_full_transcript(self) -> str:
        """Get the complete transcript text"""
        return " ".join([item["text"] for item in self.transcript_buffer])
    
    def get_new_transcript(self, since: datetime = None) -> str:
        """Get transcript text since a specific time"""
        if since is None:
            since = self.last_update_time
        
        new_items = [
            item["text"] for item in self.transcript_buffer 
            if item["timestamp"] > since
        ]
        return " ".join(new_items)
    
    def should_update_notes(self) -> bool:
        """Check if notes should be updated based on time and content"""
        now = datetime.now(timezone.utc)
        time_since_update = (now - self.last_update_time).total_seconds()
        
        if time_since_update < self.update_interval:
            return False
        
        new_transcript = self.get_new_transcript()
        word_count = len(new_transcript.split())
        
        return word_count >= self.min_words
    
    async def update_notes(self, force: bool = False) -> Optional[Dict[str, Any]]:
        """Update meeting notes if conditions are met"""
        if not force and not self.should_update_notes():
            return None
        
        try:
            full_transcript = self.get_full_transcript()
            
            if self.last_notes and not force:
                # Incremental update
                new_transcript = self.get_new_transcript()
                notes = await self.service.generate_incremental_notes(
                    new_transcript, self.last_notes
                )
            else:
                # Full generation
                notes = await self.service.generate_meeting_notes(full_transcript)
            
            self.last_notes = notes
            self.last_update_time = datetime.now(timezone.utc)
            
            return notes
            
        except Exception as e:
            logger.error(f"Error updating notes for session {self.session_id}: {e}")
            return None
    
    def clear(self):
        """Clear the buffer"""
        self.transcript_buffer = []
        self.last_notes = None
        self.last_update_time = datetime.now(timezone.utc)