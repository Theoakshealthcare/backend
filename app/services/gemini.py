import google.generativeai as genai
from typing import Dict, Any, List, Optional
import json
import asyncio
from concurrent.futures import ThreadPoolExecutor

from app.core.config import settings


class GeminiService:
    def __init__(self):
        genai.configure(api_key=settings.GEMINI_API_KEY)
        self.model = genai.GenerativeModel('gemini-1.5-flash')
        self.executor = ThreadPoolExecutor(max_workers=3)

    async def transcribe_and_analyze_audio(self, audio_transcript: str, 
                                         patient_info: Dict[str, Any]) -> Dict[str, Any]:
        """Analyze audio transcript and generate medical notes"""
        
        prompt = f"""
        You are a medical AI assistant helping to analyze caregiver notes. 
        
        Patient Information:
        - Service Type: {patient_info.get('service_type', 'N/A')}
        - Assistance Type: {patient_info.get('assistance_type', 'N/A')}
        - Condition: {patient_info.get('condition', 'N/A')}
        - Special Instructions: {patient_info.get('special_instructions', 'N/A')}
        
        Audio Transcript:
        {audio_transcript}
        
        Please analyze this transcript and provide:
        1. A concise summary of the care session
        2. Key medical observations or concerns
        3. Recommended follow-up actions
        4. Important keywords/tags for categorization
        
        Format your response as JSON with the following structure:
        {{
            "summary": "Brief summary of the session",
            "observations": ["observation 1", "observation 2"],
            "recommendations": ["recommendation 1", "recommendation 2"],
            "keywords": ["keyword1", "keyword2", "keyword3"],
            "urgency_level": "low|medium|high",
            "follow_up_required": true/false
        }}
        """
        
        try:
            # Run the AI generation in a thread pool to avoid blocking
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                self.executor, 
                self._generate_content, 
                prompt
            )
            
            # Parse the JSON response
            result = json.loads(response.text)
            return result
            
        except Exception as e:
            print(f"Error analyzing audio transcript: {e}")
            return {
                "summary": "Unable to analyze transcript",
                "observations": [],
                "recommendations": [],
                "keywords": [],
                "urgency_level": "low",
                "follow_up_required": False
            }

    def _generate_content(self, prompt: str):
        """Synchronous method to generate content"""
        return self.model.generate_content(prompt)

    async def generate_care_recommendations(self, booking_history: List[Dict[str, Any]], 
                                          current_condition: str) -> str:
        """Generate personalized care recommendations based on history"""
        
        history_text = "\n".join([
            f"Session {i+1}: {session.get('summary', 'N/A')}" 
            for i, session in enumerate(booking_history[-5:])  # Last 5 sessions
        ])
        
        prompt = f"""
        Based on the patient's care history and current condition, provide personalized recommendations:
        
        Current Condition: {current_condition}
        
        Recent Care History:
        {history_text}
        
        Please provide specific, actionable care recommendations for the caregiver.
        """
        
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                self.executor, 
                self._generate_content, 
                prompt
            )
            return response.text
        except Exception as e:
            print(f"Error generating recommendations: {e}")
            return "Unable to generate recommendations at this time."

    async def analyze_care_patterns(self, notes: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Analyze patterns in care notes to identify trends"""
        
        notes_text = "\n".join([
            f"Date: {note.get('created_at', 'N/A')}\nContent: {note.get('content', 'N/A')}\n---"
            for note in notes
        ])
        
        prompt = f"""
        Analyze these care notes to identify patterns, trends, and insights:
        
        {notes_text}
        
        Provide analysis in JSON format:
        {{
            "improvement_trends": ["trend 1", "trend 2"],
            "concerning_patterns": ["pattern 1", "pattern 2"],
            "care_effectiveness": "improving|stable|declining",
            "recommendations": ["rec 1", "rec 2"]
        }}
        """
        
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                self.executor, 
                self._generate_content, 
                prompt
            )
            return json.loads(response.text)
        except Exception as e:
            print(f"Error analyzing care patterns: {e}")
            return {
                "improvement_trends": [],
                "concerning_patterns": [],
                "care_effectiveness": "stable",
                "recommendations": []
            }

    async def analyze_vitals(self, vitals: list) -> dict:
        """Analyze patient vitals and return insights, trends, and recommendations."""
        prompt = f"""
        You are a medical AI assistant. Analyze the following patient vitals history and provide:
        1. A summary of the patient's health trends.
        2. Any concerning patterns or out-of-range values.
        3. Recommendations for the patient or caregiver.
        4. A list of key vitals that should be monitored closely.
        
        Vitals data (JSON):
        {vitals}
        
        Respond in JSON with keys: summary, concerns, recommendations, key_vitals.
        """
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                self.executor,
                self._generate_content,
                prompt
            )
            # Parse the JSON response
            result = json.loads(response.text)
            return result
        except Exception as e:
            print(f"Error analyzing vitals: {e}")
            return {
                "summary": "Unable to analyze vitals.",
                "concerns": [],
                "recommendations": [],
                "key_vitals": []
            }


# Initialize service
gemini_service = GeminiService()
