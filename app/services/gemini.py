from google import genai
from typing import Dict, Any, List, Optional
import json
import logging
import re

from app.core.config import settings

log = logging.getLogger(__name__)

# Tried in order after settings.GEMINI_MODEL when a model answers 404 (retired or not enabled for the key).
FALLBACK_MODELS = ("gemini-3.1-flash-lite",)


def _status_code(exc: Exception) -> Optional[int]:
    """HTTP status carried by a google-genai APIError (``code``), if any."""
    code = getattr(exc, "code", None)
    return code if isinstance(code, int) else None


def _model_chain() -> List[str]:
    primary = settings.GEMINI_MODEL
    return [primary] + [m for m in FALLBACK_MODELS if m != primary]


class GeminiService:
    def __init__(self):
        # Built on first use: the client refuses to construct without a key, and the module-level
        # service is created at import time (CI and tests have no key).
        self._client: Optional[genai.Client] = None

    @property
    def client(self) -> genai.Client:
        if self._client is None:
            self._client = genai.Client(api_key=settings.GEMINI_API_KEY)
        return self._client

    @staticmethod
    def _log_failure(operation: str, exc: Exception) -> None:
        """Warn about a failed AI call. Only the exception class and HTTP status are logged: the prompt and the
        answers are health data, and the exception text can echo request details."""
        log.warning("Gemini %s failed: %s (status=%s)", operation, type(exc).__name__, _status_code(exc))

    async def _generate_text(self, prompt: str) -> str:
        """Ask the configured model; on a 404 (model retired for this key) try the next one in the chain."""
        chain = _model_chain()
        for i, model in enumerate(chain):
            try:
                response = await self.client.aio.models.generate_content(model=model, contents=prompt)
                return response.text
            except Exception as exc:
                if _status_code(exc) == 404 and i < len(chain) - 1:
                    log.warning("Gemini model %s answered 404, trying %s", model, chain[i + 1])
                    continue
                raise

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
            raw_text = await self._generate_text(prompt)

            # Parse the JSON response
            result = json.loads(raw_text)
            return result
            
        except Exception as e:
            self._log_failure("transcript analysis", e)
            return {
                "summary": "Unable to analyze transcript",
                "observations": [],
                "recommendations": [],
                "keywords": [],
                "urgency_level": "low",
                "follow_up_required": False
            }

    def _extract_json_from_markdown(self, text):
        match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
        if match:
            return match.group(1)
        return text

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
            raw_text = await self._generate_text(prompt)
            json_text = self._extract_json_from_markdown(raw_text)
            return json_text  # Return the extracted JSON or text
        except Exception as e:
            self._log_failure("care recommendations", e)
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
            raw_text = await self._generate_text(prompt)
            json_text = self._extract_json_from_markdown(raw_text)
            return json.loads(json_text)
        except Exception as e:
            self._log_failure("care patterns", e)
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
            raw_text = await self._generate_text(prompt)
            json_text = self._extract_json_from_markdown(raw_text)
            result = json.loads(json_text)
            return result
        except Exception as e:
            self._log_failure("vitals analysis", e)
            return {
                "summary": "Unable to analyze vitals.",
                "concerns": [],
                "recommendations": [],
                "key_vitals": []
            }

    async def analyze_fallguard_assessment(self, answers: list, total_score: int) -> dict:
        """Analyze FallGuard answers and score, return summary and recommendations"""
        prompt = f"""
        You are a medical AI assistant. Analyze the following fall risk assessment answers and total score. 
        Provide a brief risk summary and personalized recommendations for fall prevention.
        
        Answers: {json.dumps(answers, indent=2)}
        Total Score: {total_score}
        
        Respond in JSON with:
        {{
            "summary": "...",
            "recommendations": ["...", "..."]
        }}
        """
        try:
            raw_text = await self._generate_text(prompt)
            json_text = self._extract_json_from_markdown(raw_text)
            result = json.loads(json_text)
            return result
        except Exception as e:
            self._log_failure("fallguard assessment", e)
            return {"summary": "Unable to analyze", "recommendations": []}


# Initialize service
gemini_service = GeminiService()
