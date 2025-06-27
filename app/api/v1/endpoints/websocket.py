from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends
from sqlalchemy.orm import Session
import json

from app.api.deps import get_db
from app.services.websocket import manager
from app.services.user import UserService
from app.core.security import decode_websocket_token

router = APIRouter()

@router.websocket("/ws/{token}")
async def websocket_endpoint(websocket: WebSocket, token: str, db: Session = Depends(get_db)):
    try:
        # Decode token to get user info
        user_data = decode_websocket_token(token)
        if not user_data:
            await websocket.close(code=4001, reason="Invalid token")
            return
        
        user_id = user_data.get("user_id")
        user_type = user_data.get("user_type")
        
        if not user_id or not user_type:
            await websocket.close(code=4002, reason="Invalid user data")
            return
        
        # Connect user
        await manager.connect(websocket, user_id, user_type)
        
        try:
            while True:
                # Listen for messages from client
                data = await websocket.receive_text()
                message = json.loads(data)
                
                # Handle different message types
                if message.get("type") == "join_booking":
                    booking_id = message.get("booking_id")
                    if booking_id:
                        manager.add_to_booking(booking_id, user_id)
                
                elif message.get("type") == "leave_booking":
                    booking_id = message.get("booking_id")
                    if booking_id:
                        manager.remove_from_booking(booking_id, user_id)
                
                elif message.get("type") == "chat_message":
                    # Broadcast chat message to booking participants
                    booking_id = message.get("booking_id")
                    if booking_id:
                        chat_data = {
                            "type": "chat_message",
                            "booking_id": booking_id,
                            "sender_id": user_id,
                            "message": message.get("message"),
                            "timestamp": message.get("timestamp")
                        }
                        await manager.send_booking_message(chat_data, booking_id)
                
                elif message.get("type") == "location_update":
                    # Handle real-time location updates
                    if user_type == "caregiver":
                        location_data = {
                            "type": "caregiver_location",
                            "caregiver_id": user_id,
                            "latitude": message.get("latitude"),
                            "longitude": message.get("longitude"),
                            "timestamp": message.get("timestamp")
                        }
                        
                        # Send to patients tracking this caregiver
                        booking_id = message.get("booking_id")
                        if booking_id:
                            await manager.send_booking_message(location_data, booking_id)
                
        except WebSocketDisconnect:
            manager.disconnect(user_id)
            
    except Exception as e:
        print(f"WebSocket error: {e}")
        await websocket.close(code=4000, reason="Server error")
