import json
import asyncio
from typing import Dict, List, Set
from fastapi import WebSocket, WebSocketDisconnect
from uuid import UUID

class ConnectionManager:
    def __init__(self):
        # Store active connections by user_id
        self.active_connections: Dict[str, WebSocket] = {}
        # Store user types for routing
        self.user_types: Dict[str, str] = {}
        # Store booking participants
        self.booking_participants: Dict[str, Set[str]] = {}

    async def connect(self, websocket: WebSocket, user_id: str, user_type: str):
        await websocket.accept()
        self.active_connections[user_id] = websocket
        self.user_types[user_id] = user_type
        print(f"User {user_id} ({user_type}) connected")

    def disconnect(self, user_id: str):
        if user_id in self.active_connections:
            del self.active_connections[user_id]
        if user_id in self.user_types:
            del self.user_types[user_id]
        print(f"User {user_id} disconnected")

    async def send_personal_message(self, message: dict, user_id: str):
        if user_id in self.active_connections:
            try:
                await self.active_connections[user_id].send_text(json.dumps(message))
            except:
                # Connection is broken, remove it
                self.disconnect(user_id)

    async def send_booking_message(self, message: dict, booking_id: str):
        """Send message to all participants in a booking"""
        if booking_id in self.booking_participants:
            for user_id in self.booking_participants[booking_id]:
                await self.send_personal_message(message, user_id)

    async def notify_nearby_caregivers(self, caregiver_ids: List[str], booking_data: dict):
        """Send booking notification to nearby caregivers"""
        message = {
            "type": "new_booking_request",
            "data": booking_data
        }
        
        for caregiver_id in caregiver_ids:
            await self.send_personal_message(message, caregiver_id)

    async def notify_booking_update(self, booking_id: str, patient_id: str, caregiver_id: str, update_data: dict):
        """Notify booking participants of status updates"""
        message = {
            "type": "booking_update",
            "booking_id": booking_id,
            "data": update_data
        }
        
        # Notify patient
        await self.send_personal_message(message, patient_id)
        
        # Notify caregiver if assigned
        if caregiver_id:
            await self.send_personal_message(message, caregiver_id)

    def add_to_booking(self, booking_id: str, user_id: str):
        """Add user to booking participants"""
        if booking_id not in self.booking_participants:
            self.booking_participants[booking_id] = set()
        self.booking_participants[booking_id].add(user_id)

    def remove_from_booking(self, booking_id: str, user_id: str):
        """Remove user from booking participants"""
        if booking_id in self.booking_participants:
            self.booking_participants[booking_id].discard(user_id)
            if not self.booking_participants[booking_id]:
                del self.booking_participants[booking_id]


# Global connection manager
manager = ConnectionManager()
