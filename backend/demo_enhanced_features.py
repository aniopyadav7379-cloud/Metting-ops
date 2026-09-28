#!/usr/bin/env python3
"""
Demonstration of Enhanced Unicorn Commander Features
Shows meeting templates, speaker diarization, and Ollama integration
"""
import asyncio
from services.llm_summarization_service import llm_summarization_service
from services.meeting_templates import MeetingTemplates

async def demo_enhanced_features():
    print("🦄 Unicorn Commander - Enhanced Features Demo")
    print("=" * 60)
    
    # 1. Show available meeting templates
    print("\n📋 Available Meeting Templates:")
    print("-" * 40)
    templates = MeetingTemplates.list_templates()
    for template in templates:
        print(f"  • {template['name']} ({template['id']})")
        print(f"    {template['description']}")
        print(f"    Typical duration: {template['typical_duration']}")
        print()
    
    # 2. Demo with Sprint Retrospective template
    print("\n🎯 Demo: Sprint Retrospective Meeting")
    print("-" * 40)
    
    # Sample meeting data with speaker roles
    session_data = {
        'meeting_type': 'Sprint Retrospective',
        'duration_seconds': 3600,  # 60 minutes
        'speakers': [
            {'speaker_id': 'speaker_1', 'name': 'Sarah Chen', 'role': 'Scrum Master'},
            {'speaker_id': 'speaker_2', 'name': 'Mike Johnson', 'role': 'Developer'},
            {'speaker_id': 'speaker_3', 'name': 'Lisa Wang', 'role': 'Product Owner'},
            {'speaker_id': 'speaker_4', 'name': 'James Smith', 'role': 'QA Engineer'}
        ]
    }
    
    # Retrospective transcription with clear diarization
    transcriptions = [
        # Opening
        {'speaker_id': 'speaker_1', 'text': "Welcome everyone to our sprint retrospective. Let's start by discussing what went well this sprint.", 'confidence': 0.95},
        
        # What went well
        {'speaker_id': 'speaker_2', 'text': "I think our daily standups were really effective this time. Everyone was on time and we kept them short.", 'confidence': 0.93},
        {'speaker_id': 'speaker_3', 'text': "Agreed! Also, the new CI/CD pipeline Mike set up saved us a lot of time on deployments.", 'confidence': 0.94},
        {'speaker_id': 'speaker_4', 'text': "The automated testing caught several bugs before they reached production. That was a huge win.", 'confidence': 0.92},
        {'speaker_id': 'speaker_2', 'text': "Thanks! I'm also happy with how we handled the urgent customer request mid-sprint without derailing other work.", 'confidence': 0.91},
        
        # What didn't go well
        {'speaker_id': 'speaker_1', 'text': "Now let's talk about what didn't go so well. Any challenges we faced?", 'confidence': 0.95},
        {'speaker_id': 'speaker_3', 'text': "The requirements for the payment feature kept changing. We had to redo work multiple times.", 'confidence': 0.93},
        {'speaker_id': 'speaker_4', 'text': "Yeah, and we didn't have enough test data for the new features. I had to create it manually which took time.", 'confidence': 0.90},
        {'speaker_id': 'speaker_2', 'text': "The backend and frontend teams were out of sync on the API changes. We need better communication there.", 'confidence': 0.92},
        
        # Improvements
        {'speaker_id': 'speaker_1', 'text': "Good points. What can we improve for next sprint?", 'confidence': 0.94},
        {'speaker_id': 'speaker_3', 'text': "I'll work on getting clearer requirements upfront and documenting any changes immediately.", 'confidence': 0.93},
        {'speaker_id': 'speaker_2', 'text': "We should have a quick API design review before implementation starts.", 'confidence': 0.91},
        {'speaker_id': 'speaker_4', 'text': "I can set up a test data generator tool so we don't have this problem again.", 'confidence': 0.90},
        {'speaker_id': 'speaker_1', 'text': "Excellent action items. I'll schedule the API design reviews. Let's aim for even better collaboration next sprint!", 'confidence': 0.95}
    ]
    
    # Get the retrospective template
    template = MeetingTemplates.get_template("retrospective")
    
    print(f"\n🤖 Using template: {template['name']}")
    print(f"📝 System prompt preview:")
    print(template['system_prompt'][:200] + "...")
    
    # Custom instructions for this specific retro
    custom_instructions = """
    Pay special attention to:
    - Technical debt items mentioned
    - Team morale indicators
    - Process improvements that can be implemented immediately
    """
    
    # LLM parameters for Ollama
    llm_params = {
        "temperature": 0.7,
        "top_p": 0.9,
        "num_predict": 2048,
        "num_ctx": 4096
    }
    
    print(f"\n⚙️ LLM Parameters:")
    print(f"  - Model: gemma3n:latest")
    print(f"  - Temperature: {llm_params['temperature']}")
    print(f"  - Context window: {llm_params['num_ctx']}")
    
    print(f"\n🎙️ Meeting participants:")
    for speaker in session_data['speakers']:
        print(f"  - {speaker['name']} ({speaker['role']})")
    
    print(f"\n💬 Transcript segments: {len(transcriptions)}")
    print(f"⏱️ Duration: {session_data['duration_seconds']//60} minutes")
    
    print("\n" + "="*60)
    print("🚀 Generating meeting summary with Ollama...")
    print("="*60)
    
    try:
        # Generate comprehensive summary with template
        summary = await llm_summarization_service.generate_comprehensive_summary(
            session_data=session_data,
            transcriptions=transcriptions,
            speakers=session_data['speakers'],
            summary_style='detailed',
            template_type='retrospective',
            custom_instructions=custom_instructions,
            llm_parameters=llm_params
        )
        
        # Display the enhanced summary
        print("\n📊 RETROSPECTIVE SUMMARY")
        print("="*60)
        
        print("\n📝 Executive Summary:")
        print("-" * 40)
        print(summary.get('executive_summary', 'No summary available'))
        
        print("\n✅ What Went Well:")
        print("-" * 40)
        # Extract from summary based on template structure
        
        print("\n❌ Challenges:")
        print("-" * 40)
        # Extract from summary
        
        print("\n🎯 Action Items:")
        print("-" * 40)
        for item in summary.get('action_items', []):
            assignee = item.get('assignee', 'Team')
            print(f"• {item.get('task', 'Unknown task')}")
            print(f"  Assigned to: {assignee}")
            print(f"  Priority: {item.get('priority', 'medium')}")
        
        print("\n🎭 Team Sentiment:")
        print("-" * 40)
        sentiment = summary.get('sentiment', {})
        print(f"• Overall: {sentiment.get('overall_tone', 'neutral')}")
        print(f"• Collaboration: {sentiment.get('collaboration', 'good')}")
        print(f"• Engagement: {sentiment.get('engagement', 'medium')}")
        
        print("\n👥 Speaker Insights:")
        print("-" * 40)
        for insight in summary.get('speaker_insights', []):
            speaker_id = insight.get('speaker_id')
            speaker = next((s for s in session_data['speakers'] if s['speaker_id'] == speaker_id), {})
            if speaker:
                print(f"• {speaker.get('name', 'Unknown')} ({speaker.get('role', '')})")
                print(f"  Contributions: {insight.get('total_contributions', 0)}")
                print(f"  Style: {insight.get('speaking_style', 'unknown')}")
        
        print("\n📈 Meeting Quality Metrics:")
        print("-" * 40)
        metrics = summary.get('quality_metrics', {})
        print(f"• Participation Balance: {metrics.get('speaker_participation_balance', 0)*100:.0f}%")
        print(f"• Average Confidence: {metrics.get('avg_confidence', 0)*100:.0f}%")
        print(f"• NPU Acceleration: {'Yes' if metrics.get('npu_accelerated', False) else 'No'}")
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
    
    print("\n" + "="*60)
    print("✅ Demo complete! Key features demonstrated:")
    print("  1. Meeting templates with specific prompts")
    print("  2. Speaker diarization with names and roles")
    print("  3. Custom instructions for specific needs")
    print("  4. Ollama LLM parameters configuration")
    print("  5. Comprehensive structured output")

if __name__ == "__main__":
    asyncio.run(demo_enhanced_features())