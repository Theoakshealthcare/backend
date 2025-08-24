# WebSocket Integration Guide - Scalable Real-time Location Tracking

## 🚀 **Why WebSocket Instead of Polling?**

### **Problem with REST Polling:**
```
❌ OLD APPROACH (REST Polling):
Every 10 seconds × 1000 caregivers = 100 API calls/second
Every 10 seconds × 5000 caregivers = 500 API calls/second  
= Massive server load + battery drain + network overhead
```

### **Solution with WebSocket:**
```
✅ NEW APPROACH (WebSocket):
1000 persistent connections = 1000 connections total
Send location only when changed = ~10-50 updates/second
= 90% less server load + better battery + real-time updates
```

## 📡 **WebSocket Architecture Overview**

```
┌─────────────────┐    WebSocket     ┌─────────────────┐
│   Caregiver     │ ←──────────────→ │                 │
│   Mobile App    │   Location       │   WebSocket     │
└─────────────────┘   Updates        │   Manager       │
                                     │                 │
┌─────────────────┐    WebSocket     │                 │    ┌─────────────────┐
│   Patient       │ ←──────────────→ │                 │ ←→ │   Database      │
│   Mobile App    │   Live Tracking  │                 │    │   Updates       │
└─────────────────┘                  └─────────────────┘    └─────────────────┘
```

## 🔌 **WebSocket Endpoints**

### **1. Caregiver Location Updates**
**Endpoint:** `wss://backend-y5r0.onrender.com/api/v1/booking/ws/caregiver/location?token=JWT_TOKEN`

### **2. Patient Live Tracking**  
**Endpoint:** `wss://backend-y5r0.onrender.com/api/v1/booking/ws/patient/tracking?token=JWT_TOKEN`

### **3. Connection Statistics (REST)**
**Endpoint:** `GET /api/v1/booking/ws/stats`

## 📱 **Frontend Implementation**

### **Caregiver App - Location Tracking**

```typescript
class CaregiverLocationWebSocket {
  private ws: WebSocket | null = null;
  private reconnectAttempts = 0;
  private maxReconnectAttempts = 5;
  private heartbeatInterval: number | null = null;
  private lastLocation: {lat: number, lng: number} | null = null;

  constructor(private token: string) {}

  connect() {
    const wsUrl = `wss://api.yourdomain.com/api/v1/booking/ws/caregiver/location?token=${this.token}`;
    this.ws = new WebSocket(wsUrl);
    
    this.ws.onopen = () => {
      console.log('Connected to location WebSocket');
      this.reconnectAttempts = 0;
      this.startHeartbeat();
    };
    
    this.ws.onmessage = (event) => {
      const message = JSON.parse(event.data);
      this.handleMessage(message);
    };
    
    this.ws.onclose = () => {
      console.log('WebSocket disconnected');
      this.stopHeartbeat();
      this.attemptReconnect();
    };
    
    this.ws.onerror = (error) => {
      console.error('WebSocket error:', error);
    };
  }

  private handleMessage(message: any) {
    switch(message.type) {
      case 'connection_status':
        console.log('Connection status:', message.message);
        break;
      case 'heartbeat':
        // Heartbeat received
        break;
      case 'error':
        console.error('WebSocket error:', message.error);
        break;
    }
  }

  private startHeartbeat() {
    this.heartbeatInterval = setInterval(() => {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.sendMessage({
          type: 'heartbeat',
          timestamp: new Date().toISOString()
        });
      }
    }, 30000); // Every 30 seconds
  }

  private stopHeartbeat() {
    if (this.heartbeatInterval) {
      clearInterval(this.heartbeatInterval);
      this.heartbeatInterval = null;
    }
  }

  private attemptReconnect() {
    if (this.reconnectAttempts < this.maxReconnectAttempts) {
      this.reconnectAttempts++;
      const delay = Math.pow(2, this.reconnectAttempts) * 1000; // Exponential backoff
      console.log(`Attempting reconnect ${this.reconnectAttempts}/${this.maxReconnectAttempts} in ${delay}ms`);
      
      setTimeout(() => {
        this.connect();
      }, delay);
    } else {
      console.error('Max reconnection attempts reached');
      // Fall back to REST API polling as backup
      this.fallbackToRestPolling();
    }
  }

  sendLocationUpdate(locationData: {
    latitude: number;
    longitude: number;
    accuracy?: number;
    heading?: number;
    speed?: number;
    is_available: boolean;
  }) {
    // Only send if location changed significantly (>10 meters)
    if (this.lastLocation) {
      const distance = this.calculateDistance(
        this.lastLocation.lat, this.lastLocation.lng,
        locationData.latitude, locationData.longitude
      );
      if (distance < 0.01) return; // Less than 10 meters, skip
    }

    this.sendMessage({
      type: 'location_update',
      data: locationData
    });

    this.lastLocation = {
      lat: locationData.latitude,
      lng: locationData.longitude
    };
  }

  private sendMessage(message: any) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(message));
    }
  }

  private calculateDistance(lat1: number, lng1: number, lat2: number, lng2: number): number {
    // Haversine formula for distance in km
    const R = 6371;
    const dLat = (lat2 - lat1) * Math.PI / 180;
    const dLng = (lng2 - lng1) * Math.PI / 180;
    const a = Math.sin(dLat/2) * Math.sin(dLat/2) +
              Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
              Math.sin(dLng/2) * Math.sin(dLng/2);
    const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
    return R * c;
  }

  private fallbackToRestPolling() {
    // Implement REST API polling as fallback
    console.log('Falling back to REST API polling...');
  }

  disconnect() {
    this.stopHeartbeat();
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }
}
```

### **Patient App - Live Tracking**

```typescript
class PatientTrackingWebSocket {
  private ws: WebSocket | null = null;
  private subscribedBookings = new Set<string>();
  private onLocationUpdate?: (data: any) => void;

  constructor(private token: string) {}

  connect() {
    const wsUrl = `wss://api.yourdomain.com/api/v1/booking/ws/patient/tracking?token=${this.token}`;
    this.ws = new WebSocket(wsUrl);
    
    this.ws.onopen = () => {
      console.log('Connected to tracking WebSocket');
    };
    
    this.ws.onmessage = (event) => {
      const message = JSON.parse(event.data);
      this.handleMessage(message);
    };
    
    this.ws.onclose = () => {
      console.log('Tracking WebSocket disconnected');
      // Attempt reconnection
      setTimeout(() => this.connect(), 5000);
    };
    
    this.ws.onerror = (error) => {
      console.error('Tracking WebSocket error:', error);
    };
  }

  private handleMessage(message: any) {
    switch(message.type) {
      case 'connection_status':
        console.log('Connection status:', message.message);
        break;
        
      case 'location_broadcast':
        // Real-time location update received!
        if (this.onLocationUpdate) {
          this.onLocationUpdate(message.data);
        }
        break;
        
      case 'error':
        console.error('Tracking error:', message.error);
        break;
    }
  }

  subscribeToBooking(bookingId: string, callback: (data: any) => void) {
    this.onLocationUpdate = callback;
    this.subscribedBookings.add(bookingId);
    
    this.sendMessage({
      type: 'tracking_subscribe',
      booking_id: bookingId
    });
    
    console.log(`Subscribed to tracking for booking: ${bookingId}`);
  }

  unsubscribeFromBooking(bookingId: string) {
    this.subscribedBookings.delete(bookingId);
    
    this.sendMessage({
      type: 'tracking_unsubscribe', 
      booking_id: bookingId
    });
    
    console.log(`Unsubscribed from tracking for booking: ${bookingId}`);
  }

  private sendMessage(message: any) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(message));
    }
  }

  disconnect() {
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
    this.subscribedBookings.clear();
  }
}
```

### **React Hook for Easy Integration**

```typescript
import { useEffect, useRef, useState } from 'react';

export function useLiveTracking(bookingId: string, token: string) {
  const [trackingData, setTrackingData] = useState<any>(null);
  const [isConnected, setIsConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const wsRef = useRef<PatientTrackingWebSocket | null>(null);

  useEffect(() => {
    if (!bookingId || !token) return;

    // Initialize WebSocket connection
    wsRef.current = new PatientTrackingWebSocket(token);
    wsRef.current.connect();
    
    // Subscribe to booking tracking
    wsRef.current.subscribeToBooking(bookingId, (data) => {
      setTrackingData(data);
      setError(null);
    });

    // Cleanup on unmount
    return () => {
      if (wsRef.current) {
        wsRef.current.unsubscribeFromBooking(bookingId);
        wsRef.current.disconnect();
      }
    };
  }, [bookingId, token]);

  return {
    trackingData,
    isConnected,
    error,
    subscribe: (id: string) => wsRef.current?.subscribeToBooking(id, setTrackingData),
    unsubscribe: (id: string) => wsRef.current?.unsubscribeFromBooking(id)
  };
}

// Usage in component
function BookingTrackingMap({ bookingId }: { bookingId: string }) {
  const { trackingData, isConnected } = useLiveTracking(bookingId, userToken);

  useEffect(() => {
    if (trackingData) {
      // Update map with real-time location
      updateMapMarker(trackingData.current_latitude, trackingData.current_longitude);
      updateETA(trackingData.estimated_arrival_minutes);
    }
  }, [trackingData]);

  return (
    <div>
      <div>Status: {isConnected ? '🟢 Connected' : '🔴 Disconnected'}</div>
      {trackingData && (
        <div>
          <p>Caregiver: {trackingData.caregiver_name}</p>
          <p>Distance: {trackingData.distance_km} km</p>
          <p>ETA: {trackingData.estimated_arrival_minutes} minutes</p>
          <p>Moving: {trackingData.is_moving ? '🚗 On the way' : '⏸️ Stopped'}</p>
        </div>
      )}
    </div>
  );
}
```

## ⚙️ **Message Protocol**

### **Caregiver → Server Messages**

#### **Location Update:**
```json
{
  "type": "location_update",
  "data": {
    "latitude": 6.5244,
    "longitude": 3.3792,
    "accuracy": 10.5,
    "heading": 45.0,
    "speed": 2.3,
    "is_available": true
  }
}
```

#### **Heartbeat:**
```json
{
  "type": "heartbeat",
  "timestamp": "2024-01-15T10:30:00Z"
}
```

### **Patient → Server Messages**

#### **Subscribe to Tracking:**
```json
{
  "type": "tracking_subscribe",
  "booking_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

#### **Unsubscribe from Tracking:**
```json
{
  "type": "tracking_unsubscribe",
  "booking_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

### **Server → Client Messages**

#### **Connection Status:**
```json
{
  "type": "connection_status",
  "status": "connected",
  "message": "Connected successfully"
}
```

#### **Location Broadcast (to patients):**
```json
{
  "type": "location_broadcast",
  "booking_id": "550e8400-e29b-41d4-a716-446655440000",
  "data": {
    "booking_id": "550e8400-e29b-41d4-a716-446655440000",
    "caregiver_id": "123e4567-e89b-12d3-a456-426614174000",
    "caregiver_name": "Dr. John Smith",
    "current_latitude": 6.5244,
    "current_longitude": 3.3792,
    "distance_km": 2.5,
    "estimated_arrival_minutes": 15,
    "last_updated": "2024-01-15T10:30:00Z",
    "is_moving": true,
    "booking_status": "accepted"
  }
}
```

#### **Error Message:**
```json
{
  "type": "error",
  "error": "Authentication failed",
  "code": "AUTH_ERROR"
}
```

## 🔒 **Authentication**

### **WebSocket Authentication:**
- **Query Parameter**: `?token=JWT_TOKEN`
- **Alternative**: Send token in first message after connection
- **Token Validation**: Standard JWT validation on server side

```javascript
// Method 1: Query Parameter
const ws = new WebSocket(`wss://api.example.com/ws/caregiver/location?token=${jwtToken}`);

// Method 2: First Message (if needed)
ws.onopen = () => {
  ws.send(JSON.stringify({
    type: 'authenticate',
    token: jwtToken
  }));
};
```

## 📊 **Performance & Scalability**

### **Connection Management:**
- **Heartbeat**: 30 seconds to detect stale connections
- **Automatic cleanup**: Remove connections after 2 minutes without heartbeat
- **Graceful reconnection**: Exponential backoff on disconnection

### **Scalability Features:**
- **Connection pooling**: Efficiently manage thousands of connections
- **Memory optimization**: Clean up unused subscriptions automatically  
- **Load balancing ready**: Stateless message handling

### **Performance Metrics:**
```typescript
// Monitor connection health
const stats = await fetch('/api/v1/booking/ws/stats');
const data = await stats.json();

console.log({
  activeCaregiversConnected: data.active_caregiver_connections,
  activePatientsConnected: data.active_patient_connections, 
  activeTrackingSubscriptions: data.active_subscriptions,
  totalSubscribers: data.total_subscribers
});
```

## 🔄 **Fallback Strategy**

### **Hybrid Approach:**
```typescript
class HybridLocationService {
  private websocketService: CaregiverLocationWebSocket;
  private restService: RestLocationService;
  private useWebSocket = true;

  async sendLocationUpdate(location: LocationData) {
    if (this.useWebSocket && this.websocketService.isConnected()) {
      // Primary: WebSocket
      this.websocketService.sendLocationUpdate(location);
    } else {
      // Fallback: REST API
      await this.restService.updateLocation(location);
      
      // Try to reconnect WebSocket
      if (!this.websocketService.isConnected()) {
        this.websocketService.connect();
      }
    }
  }
}
```

## 🧪 **Testing WebSocket Integration**

### **Test WebSocket Connections:**
```javascript
// Test caregiver connection
const testCaregiverWS = () => {
  const ws = new WebSocket('wss://api.example.com/ws/caregiver/location?token=test_token');
  
  ws.onopen = () => {
    console.log('✅ Caregiver WebSocket connected');
    
    // Test location update
    ws.send(JSON.stringify({
      type: 'location_update',
      data: {
        latitude: 6.5244,
        longitude: 3.3792,
        is_available: true
      }
    }));
  };
  
  ws.onmessage = (event) => {
    console.log('📨 Received:', JSON.parse(event.data));
  };
};

// Test patient tracking
const testPatientWS = () => {
  const ws = new WebSocket('wss://api.example.com/ws/patient/tracking?token=test_token');
  
  ws.onopen = () => {
    console.log('✅ Patient WebSocket connected');
    
    // Subscribe to booking
    ws.send(JSON.stringify({
      type: 'tracking_subscribe',
      booking_id: 'test-booking-id'
    }));
  };
  
  ws.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.type === 'location_broadcast') {
      console.log('📍 Live location update:', message.data);
    }
  };
};
```

## 📈 **Migration Strategy**

### **Phase 1: Dual Support**
- Keep existing REST endpoints for backward compatibility
- Add WebSocket endpoints for new features
- Gradual migration of mobile apps

### **Phase 2: WebSocket Primary**
- Use WebSocket as primary method
- REST as fallback for connection issues
- Monitor performance improvements

### **Phase 3: WebSocket Only**
- Deprecate REST polling endpoints
- Full WebSocket implementation
- Significant resource savings

## 🎯 **Benefits Summary**

| Aspect | REST Polling | WebSocket |
|--------|-------------|-----------|
| **Server Load** | High (constant requests) | Low (persistent connections) |
| **Battery Usage** | High (frequent polling) | Low (push notifications) |
| **Real-time Updates** | Delayed (polling interval) | Instant (push) |
| **Network Efficiency** | Poor (HTTP overhead) | Excellent (binary frames) |
| **Scalability** | Limited | Excellent |
| **Implementation** | Simple | Moderate |

This WebSocket implementation provides a scalable, real-time solution that's perfect for your growing user base while maintaining excellent user experience!