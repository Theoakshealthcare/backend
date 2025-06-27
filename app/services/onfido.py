import os
import requests
from typing import Dict, Any, List, Optional
from app.core.config import settings


class OnfidoService:
    def __init__(self):
        self.api_key = settings.ONFIDO_API_KEY
        self.base_url = "https://api.onfido.com/v3.6"
        self.headers = {
            "Authorization": f"Token token={self.api_key}",
            "Content-Type": "application/json"
        }

    def create_applicant(self, first_name: str, last_name: str, email: str, 
                        phone: Optional[str] = None, dob: Optional[str] = None,
                        address: Optional[Dict] = None) -> Dict[str, Any]:
        """Create an Onfido applicant"""
        data = {
            "first_name": first_name,
            "last_name": last_name,
            "email": email
        }
        
        if phone:
            data["phone_number"] = phone
        if dob:
            data["dob"] = dob
        if address:
            data["address"] = address

        response = requests.post(
            f"{self.base_url}/applicants",
            headers=self.headers,
            json=data
        )
        
        if response.status_code == 201:
            return response.json()
        else:
            raise Exception(f"Failed to create applicant: {response.text}")

    def generate_sdk_token(self, applicant_id: str, referrer: str = "*://localhost:*/*") -> str:
        """Generate SDK token for mobile integration"""
        data = {
            "applicant_id": applicant_id,
            "referrer": referrer
        }

        response = requests.post(
            f"{self.base_url}/sdk_token",
            headers=self.headers,
            json=data
        )
        
        if response.status_code == 201:
            return response.json()["token"]
        else:
            raise Exception(f"Failed to generate SDK token: {response.text}")

    def create_check(self, applicant_id: str, report_names: List[str] = None) -> Dict[str, Any]:
        """Create identity verification check"""
        if report_names is None:
            report_names = ["document", "facial_similarity_photo"]

        data = {
            "applicant_id": applicant_id,
            "report_names": report_names,
            "tags": ["caregiver_verification"],
            "suppress_form_emails": True
        }

        response = requests.post(
            f"{self.base_url}/checks",
            headers=self.headers,
            json=data
        )
        
        if response.status_code == 201:
            return response.json()
        else:
            raise Exception(f"Failed to create check: {response.text}")

    def get_check_status(self, check_id: str) -> Dict[str, Any]:
        """Get check status and results"""
        response = requests.get(
            f"{self.base_url}/checks/{check_id}",
            headers=self.headers
        )
        
        if response.status_code == 200:
            return response.json()
        else:
            raise Exception(f"Failed to get check status: {response.text}")

    def get_applicant_documents(self, applicant_id: str) -> List[Dict[str, Any]]:
        """Get all documents for an applicant"""
        response = requests.get(
            f"{self.base_url}/documents?applicant_id={applicant_id}",
            headers=self.headers
        )
        
        if response.status_code == 200:
            return response.json()["documents"]
        else:
            raise Exception(f"Failed to get documents: {response.text}")

    def download_document(self, document_id: str) -> bytes:
        """Download document file"""
        response = requests.get(
            f"{self.base_url}/documents/{document_id}/download",
            headers={"Authorization": f"Token token={self.api_key}"}
        )
        
        if response.status_code == 200:
            return response.content
        else:
            raise Exception(f"Failed to download document: {response.text}")


# Initialize service
onfido_service = OnfidoService()
