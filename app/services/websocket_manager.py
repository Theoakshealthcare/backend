import json
import logging
import asyncio
from typing import Dict, Set, Optional, List
from datetime import datetime, timedelta
from uuid import UUID
from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.schemas.booking import (
    WebSocketLocationUpdate, WebSocketTrackingSubscribe, WebSocketTrackingUnsubscribe,
    WebSocketLocationBroadcast, WebSocketConnectionStatus, WebSocketError, 
    WebSocketHeartbeat, CaregiverLocationUpdate, LiveTrackingResponse
)
from app.services.location import LocationService
from app.services.booking import BookingService
from app.db.models.caregiver import Caregiver
from app.db.models.booking import Booking, BookingStatus


log = logging.getLogger(__name__)


class WebSocketConnectionManager:
    """Manages WebSocket connections for real-time location tracking"""
    
    def __init__(self):
        # Active connections: user_id -> WebSocket
        self.caregiver_connections: Dict[str, WebSocket] = {}
        self.patient_connections: Dict[str, WebSocket] = {}
        
        # Subscription tracking: booking_id -> set of patient_user_ids
        self.tracking_subscriptions: Dict[str, Set[str]] = {}
        
        # Heartbeat tracking
        self.last_heartbeat: Dict[str, datetime] = {}
        
    async def connect_caregiver(self, websocket: WebSocket, user_id: str, subprotocol: Optional[str] = None):
        """Connect a caregiver for location updates"""
        await websocket.accept(subprotocol=subprotocol)
        self.caregiver_connections[user_id] = websocket
        self.last_heartbeat[f"caregiver_{user_id}"] = datetime.utcnow()
        
        # Send connection status
        status_message = WebSocketConnectionStatus(
            status="connected",
            message="Connected successfully. You can now send location updates."
        )
        await websocket.send_text(status_message.model_dump_json())
        
    async def connect_patient(self, websocket: WebSocket, user_id: str, subprotocol: Optional[str] = None):
        """Connect a patient for tracking updates"""
        await websocket.accept(subprotocol=subprotocol)
        self.patient_connections[user_id] = websocket
        self.last_heartbeat[f"patient_{user_id}"] = datetime.utcnow()
        
        # Send connection status
        status_message = WebSocketConnectionStatus(
            status="connected", 
            message="Connected successfully. You can now subscribe to booking tracking."
        )
        await websocket.send_text(status_message.model_dump_json())
        
    def disconnect_caregiver(self, user_id: str):
        """Disconnect a caregiver"""
        if user_id in self.caregiver_connections:
            del self.caregiver_connections[user_id]
        if f"caregiver_{user_id}" in self.last_heartbeat:
            del self.last_heartbeat[f"caregiver_{user_id}"]
            
    def disconnect_patient(self, user_id: str):
        """Disconnect a patient and cleanup subscriptions"""
        if user_id in self.patient_connections:
            del self.patient_connections[user_id]
        if f"patient_{user_id}" in self.last_heartbeat:
            del self.last_heartbeat[f"patient_{user_id}"]
            
        # Remove from all tracking subscriptions
        for booking_id, subscribers in self.tracking_subscriptions.items():
            subscribers.discard(user_id)
            
    async def handle_caregiver_message(self, websocket: WebSocket, user_id: str, 
                                     message: str, db: Session):
        """Handle incoming caregiver WebSocket messages"""
        try:
            data = json.loads(message)
            message_type = data.get("type")
            
            if message_type == "location_update":
                await self._handle_location_update(user_id, data, db)
                
            elif message_type == "heartbeat":
                self.last_heartbeat[f"caregiver_{user_id}"] = datetime.utcnow()
                heartbeat_response = WebSocketHeartbeat(timestamp=datetime.utcnow().isoformat())
                await websocket.send_text(heartbeat_response.model_dump_json())
                
        except json.JSONDecodeError:
            error_msg = WebSocketError(error="Invalid JSON format")
            await websocket.send_text(error_msg.model_dump_json())
        except Exception as e:
            error_msg = WebSocketError(error=str(e))
            await websocket.send_text(error_msg.model_dump_json())
            
    async def handle_patient_message(self, websocket: WebSocket, user_id: str, 
                                   message: str, db: Session):
        """Handle incoming patient WebSocket messages"""
        try:
            data = json.loads(message)
            message_type = data.get("type")
            
            if message_type == "tracking_subscribe":
                booking_id = data.get("booking_id")
                await self._handle_tracking_subscribe(user_id, booking_id, websocket, db)
                
            elif message_type == "tracking_unsubscribe":
                booking_id = data.get("booking_id")
                self._handle_tracking_unsubscribe(user_id, booking_id)
                
            elif message_type == "heartbeat":
                self.last_heartbeat[f"patient_{user_id}"] = datetime.utcnow()
                heartbeat_response = WebSocketHeartbeat(timestamp=datetime.utcnow().isoformat())
                await websocket.send_text(heartbeat_response.model_dump_json())
                
        except json.JSONDecodeError:
            error_msg = WebSocketError(error="Invalid JSON format")
            await websocket.send_text(error_msg.model_dump_json())
        except Exception as e:
            error_msg = WebSocketError(error=str(e))
            await websocket.send_text(error_msg.model_dump_json())
            
    async def _handle_location_update(self, caregiver_user_id: str, data: dict, db: Session):
        """Process caregiver location update and broadcast to subscribers"""
        try:
            # Validate location data
            location_data = CaregiverLocationUpdate(**data["data"])
            
            # Get caregiver record
            caregiver = db.query(Caregiver).filter(
                Caregiver.user_id == caregiver_user_id
            ).first()
            
            if not caregiver:
                return
                
            # Update location in database
            location_service = LocationService(db)
            location_service.update_caregiver_location(
                caregiver.id,
                location_data.latitude,
                location_data.longitude,
                accuracy=location_data.accuracy,
                heading=location_data.heading,
                speed=location_data.speed,
                is_available=location_data.is_available
            )
            
            # Find active bookings for this caregiver
            booking_service = BookingService(db)
            active_bookings = db.query(Booking).filter(
                Booking.caregiver_id == caregiver.id,
                Booking.status.in_(["accepted", "en_route", "in_progress"])
            ).all()
            
            # Broadcast to subscribers of active bookings
            for booking in active_bookings:
                await self._broadcast_location_update(booking.id, db)
                
        except Exception as e:
            print(f"Error handling location update: {e}")
            
    async def _handle_tracking_subscribe(self, patient_user_id: str, booking_id: str, 
                                       websocket: WebSocket, db: Session):
        """Subscribe patient to booking tracking"""
        try:
            try:
                booking_uuid = UUID(str(booking_id))
            except ValueError:
                error_msg = WebSocketError(error="Invalid booking id", code="INVALID_BOOKING_ID")
                await websocket.send_text(error_msg.model_dump_json())
                return

            # Same message for "not found" and "not yours", so ids can't be probed
            denied = WebSocketError(error="Access denied", code="UNAUTHORIZED")
            # populate_existing: this session lives as long as the socket; never trust a cached status
            booking = db.query(Booking).filter(Booking.id == booking_uuid).populate_existing().first()
            from app.db.models.patient import Patient
            patient = db.query(Patient).filter(Patient.user_id == patient_user_id).first()
            if not booking or not patient or booking.patient_id != patient.id:
                await websocket.send_text(denied.model_dump_json())
                return

            if booking.status not in (BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE, BookingStatus.IN_PROGRESS):
                error_msg = WebSocketError(
                    error="Tracking is only available for accepted or in-progress bookings",
                    code="TRACKING_NOT_AVAILABLE",
                )
                await websocket.send_text(error_msg.model_dump_json())
                return

            # Add to subscriptions
            booking_id = str(booking_uuid)
            if booking_id not in self.tracking_subscriptions:
                self.tracking_subscriptions[booking_id] = set()
            self.tracking_subscriptions[booking_id].add(patient_user_id)
            
            # Send initial tracking data if caregiver is assigned
            if booking.caregiver_id:
                location_service = LocationService(db)
                tracking_data = location_service.get_live_caregiver_tracking(booking.id)
                
                if tracking_data:
                    live_response = LiveTrackingResponse(**tracking_data)
                    broadcast_msg = WebSocketLocationBroadcast(
                        booking_id=booking_id,
                        data=live_response
                    )
                    await websocket.send_text(broadcast_msg.model_dump_json())
                    
        except Exception:
            log.exception("tracking_subscribe failed")
            error_msg = WebSocketError(error="Could not subscribe to tracking")
            await websocket.send_text(error_msg.model_dump_json())
            
    def _handle_tracking_unsubscribe(self, patient_user_id: str, booking_id: str):
        """Unsubscribe patient from booking tracking"""
        if booking_id in self.tracking_subscriptions:
            self.tracking_subscriptions[booking_id].discard(patient_user_id)
            
            # Clean up empty subscriptions
            if not self.tracking_subscriptions[booking_id]:
                del self.tracking_subscriptions[booking_id]
                
    async def _broadcast_location_update(self, booking_id: UUID, db: Session):
        """Broadcast location update to all subscribers of a booking"""
        if str(booking_id) not in self.tracking_subscriptions:
            return
            
        try:
            # Get latest tracking data
            location_service = LocationService(db)
            tracking_data = location_service.get_live_caregiver_tracking(booking_id)
            
            if not tracking_data:
                return
                
            live_response = LiveTrackingResponse(**tracking_data)
            broadcast_msg = WebSocketLocationBroadcast(
                booking_id=str(booking_id),
                data=live_response
            )
            
            # Send to all subscribers
            subscribers = self.tracking_subscriptions[str(booking_id)].copy()
            for patient_user_id in subscribers:
                if patient_user_id in self.patient_connections:
                    try:
                        websocket = self.patient_connections[patient_user_id]
                        await websocket.send_text(broadcast_msg.model_dump_json())
                    except Exception as e:
                        # Remove disconnected patient
                        print(f"Failed to send to patient {patient_user_id}: {e}")
                        self.disconnect_patient(patient_user_id)
                        
        except Exception as e:
            print(f"Error broadcasting location update: {e}")
            
    async def cleanup_stale_connections(self):
        """Remove stale connections that haven't sent heartbeat"""
        cutoff_time = datetime.utcnow() - timedelta(minutes=2)
        
        stale_caregivers = []
        stale_patients = []
        
        for key, last_heartbeat in self.last_heartbeat.items():
            if last_heartbeat < cutoff_time:
                if key.startswith("caregiver_"):
                    user_id = key.replace("caregiver_", "")
                    stale_caregivers.append(user_id)
                elif key.startswith("patient_"):
                    user_id = key.replace("patient_", "")
                    stale_patients.append(user_id)
                    
        for user_id in stale_caregivers:
            self.disconnect_caregiver(user_id)
            
        for user_id in stale_patients:
            self.disconnect_patient(user_id)
            
    def get_connection_stats(self) -> dict:
        """Get connection statistics"""
        return {
            "active_caregiver_connections": len(self.caregiver_connections),
            "active_patient_connections": len(self.patient_connections),
            "active_subscriptions": len(self.tracking_subscriptions),
            "total_subscribers": sum(len(subs) for subs in self.tracking_subscriptions.values())
        }


# Global connection manager instance
websocket_manager = WebSocketConnectionManager()