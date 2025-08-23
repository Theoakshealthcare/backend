# Frontend Coordinate Integration Guide

## 📍 **Coordinate System Overview**

### **Yes, ALL coordinates are sent from the frontend!** 
The backend does not collect GPS data automatically. Frontend apps must actively send location updates.

## 🎯 **Data Expected from Frontend**

### **1. Patient Coordinates (During Booking)**
**When:** Patient creates a new booking  
**Endpoint:** `POST /api/v1/booking/create`

```typescript
interface PatientBookingData {
  // ... other booking fields
  patient_address: string;        // Required: Full address
  patient_latitude: number;       // Required: GPS lat (-90 to 90)
  patient_longitude: number;      // Required: GPS lng (-180 to 180)
  patient_city: string;          // Required: City name
  patient_state: string;         // Required: State name
}
```

**Example Request:**
```json
{
  "service_type": "caregiver",
  "assistance_type": "Personal care",
  "duration_hours": 4,
  "scheduled_date": "2024-01-15T14:00:00Z",
  "patient_address": "123 Main Street, Victoria Island, Lagos",
  "patient_latitude": 6.4281,
  "patient_longitude": 3.4219,
  "patient_city": "Lagos",
  "patient_state": "Lagos State",
  "booking_for": "Myself"
}
```

### **2. Caregiver Coordinates (Real-time Updates)**
**When:** Continuously while caregiver app is active  
**Endpoint:** `POST /api/v1/booking/caregiver/location`

```typescript
interface CaregiverLocationUpdate {
  latitude: number;              // Required: GPS lat (-90 to 90)
  longitude: number;             // Required: GPS lng (-180 to 180)
  is_available: boolean;         // Required: Availability status
  accuracy?: number;             // Optional: GPS accuracy in meters
  heading?: number;              // Optional: Direction (0-360°)
  speed?: number;                // Optional: Speed in m/s
}
```

**Example Request:**
```json
{
  "latitude": 6.4281,
  "longitude": 3.4219,
  "accuracy": 10.5,
  "heading": 45.0,
  "speed": 2.3,
  "is_available": true
}
```

## 📱 **Frontend Implementation Requirements**

### **Patient App Implementation**

#### **Location Collection for Booking:**
```typescript
// Get patient location when creating booking
async function getCurrentLocation(): Promise<{lat: number, lng: number}> {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) {
      reject(new Error('Geolocation not supported'));
      return;
    }

    navigator.geolocation.getCurrentPosition(
      (position) => {
        resolve({
          lat: position.coords.latitude,
          lng: position.coords.longitude
        });
      },
      (error) => reject(error),
      {
        enableHighAccuracy: true,
        timeout: 10000,
        maximumAge: 60000 // Cache for 1 minute
      }
    );
  });
}

// Create booking with location
async function createBooking(bookingData: Omit<BookingData, 'patient_latitude' | 'patient_longitude'>) {
  try {
    const location = await getCurrentLocation();
    
    const fullBookingData = {
      ...bookingData,
      patient_latitude: location.lat,
      patient_longitude: location.lng
    };

    const response = await fetch('/api/v1/booking/create', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${patientToken}`
      },
      body: JSON.stringify(fullBookingData)
    });

    return response.json();
  } catch (error) {
    console.error('Error creating booking:', error);
    throw error;
  }
}
```

#### **Live Tracking for Patients:**
```typescript
// Get real-time caregiver tracking
async function getLiveTracking(bookingId: string) {
  const response = await fetch(`/api/v1/booking/${bookingId}/live-tracking`, {
    headers: {
      'Authorization': `Bearer ${patientToken}`
    }
  });
  
  if (!response.ok) throw new Error('Failed to get tracking data');
  return response.json();
}

// Implement polling for live updates
function startLiveTracking(bookingId: string, callback: (data: any) => void) {
  const interval = setInterval(async () => {
    try {
      const trackingData = await getLiveTracking(bookingId);
      callback(trackingData);
    } catch (error) {
      console.error('Live tracking error:', error);
    }
  }, 15000); // Update every 15 seconds

  return () => clearInterval(interval); // Return cleanup function
}
```

### **Caregiver App Implementation**

#### **Background Location Tracking:**
```typescript
class CaregiverLocationService {
  private watchId: number | null = null;
  private updateInterval: number | null = null;
  private lastKnownLocation: {lat: number, lng: number} | null = null;

  async startLocationTracking() {
    // Request location permissions
    const permission = await this.requestLocationPermission();
    if (permission !== 'granted') {
      throw new Error('Location permission denied');
    }

    // Start watching position
    this.watchId = navigator.geolocation.watchPosition(
      (position) => {
        this.lastKnownLocation = {
          lat: position.coords.latitude,
          lng: position.coords.longitude
        };
        this.sendLocationUpdate(position);
      },
      (error) => {
        console.error('Location error:', error);
      },
      {
        enableHighAccuracy: true,
        maximumAge: 30000, // 30 seconds
        timeout: 15000
      }
    );

    // Set up periodic updates (fallback)
    this.updateInterval = setInterval(() => {
      if (this.lastKnownLocation) {
        this.sendPeriodicUpdate();
      }
    }, 30000); // Every 30 seconds
  }

  private async sendLocationUpdate(position: GeolocationPosition) {
    const locationData = {
      latitude: position.coords.latitude,
      longitude: position.coords.longitude,
      accuracy: position.coords.accuracy,
      heading: position.coords.heading || undefined,
      speed: position.coords.speed || undefined,
      is_available: true // Get from app state
    };

    try {
      await this.updateLocation(locationData);
    } catch (error) {
      console.error('Failed to update location:', error);
      // Implement retry logic with exponential backoff
    }
  }

  private async updateLocation(locationData: CaregiverLocationUpdate) {
    const response = await fetch('/api/v1/booking/caregiver/location', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${caregiverToken}`
      },
      body: JSON.stringify(locationData)
    });

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: ${response.statusText}`);
    }

    return response.json();
  }

  stopLocationTracking() {
    if (this.watchId !== null) {
      navigator.geolocation.clearWatch(this.watchId);
      this.watchId = null;
    }

    if (this.updateInterval !== null) {
      clearInterval(this.updateInterval);
      this.updateInterval = null;
    }

    // Send final update with is_available = false
    this.updateLocation({
      ...this.lastKnownLocation!,
      is_available: false
    });
  }
}
```

## ⚙️ **Update Frequency Recommendations**

### **Caregiver Location Updates:**

| Scenario | Frequency | Reason |
|----------|-----------|---------|
| **Active booking (on the way)** | Every 10-15 seconds | Real-time tracking for patient |
| **Available for bookings** | Every 30-60 seconds | Matching algorithm needs current location |
| **On break/unavailable** | Every 5 minutes | Minimal updates to maintain status |
| **Going offline** | Send final update with `is_available: false` | Clean status |

### **Battery Optimization:**
```typescript
// Adaptive update frequency based on booking status
function getUpdateFrequency(caregiverStatus: string, hasActiveBooking: boolean) {
  if (hasActiveBooking && caregiverStatus === 'en_route') {
    return 15000; // 15 seconds - high frequency
  } else if (caregiverStatus === 'available') {
    return 45000; // 45 seconds - medium frequency
  } else {
    return 300000; // 5 minutes - low frequency
  }
}
```

## 🔄 **Data Flow Diagram**

```
1. PATIENT CREATES BOOKING
   Patient App → [GPS] → patient_latitude/longitude → Backend

2. CAREGIVER GETS NOTIFIED
   Backend → [Smart Matching] → Nearby Caregivers

3. CAREGIVER ACCEPTS
   Caregiver App → Backend → Patient App (notification)

4. REAL-TIME TRACKING STARTS
   Caregiver App → [GPS every 15s] → Backend → Patient App (polling/websocket)

5. PATIENT SEES LIVE UPDATES
   Patient App → /live-tracking → Real-time ETA & Location
```

## 📊 **Enhanced Response Data**

### **What Frontend Gets Back (Patient Bookings):**
```typescript
interface EnhancedBookingResponse {
  id: string;
  booking_code: string;
  // ... standard booking fields
  
  caregiver_details?: {
    caregiver_id: string;
    full_name: string;
    specialization?: string;
    // NEW LOCATION DATA:
    current_latitude?: number;      // Real-time caregiver location
    current_longitude?: number;     // Real-time caregiver location
    distance_from_patient?: number; // Distance in km
    estimated_arrival_time?: number; // ETA in minutes
    last_location_update?: string;  // ISO timestamp
    is_online?: boolean;           // Current online status
  };
}
```

### **What Frontend Gets Back (Live Tracking):**
```typescript
interface LiveTrackingData {
  booking_id: string;
  caregiver_id: string;
  caregiver_name: string;
  current_latitude: number;        // Current GPS coordinates
  current_longitude: number;       // Current GPS coordinates
  distance_km: number;            // Live distance calculation
  estimated_arrival_minutes: number; // Dynamic ETA
  last_updated: string;           // Last location update timestamp
  is_moving: boolean;             // Movement detection
  booking_status: string;         // Booking status
}
```

## ⚠️ **Important Considerations**

### **GPS Permissions:**
```typescript
// Handle permission requests gracefully
async function requestLocationPermission() {
  if ('permissions' in navigator) {
    const result = await navigator.permissions.query({name: 'geolocation'});
    if (result.state === 'denied') {
      // Show UI to guide user to enable permissions
      throw new Error('Location permission is required for this feature');
    }
  }
  
  return new Promise((resolve) => {
    navigator.geolocation.getCurrentPosition(
      () => resolve('granted'),
      () => resolve('denied')
    );
  });
}
```

### **Error Handling:**
```typescript
// Implement retry logic for failed location updates
class LocationUpdateService {
  private retryCount = 0;
  private maxRetries = 3;

  async updateLocationWithRetry(locationData: CaregiverLocationUpdate) {
    try {
      await this.updateLocation(locationData);
      this.retryCount = 0; // Reset on success
    } catch (error) {
      if (this.retryCount < this.maxRetries) {
        this.retryCount++;
        const delay = Math.pow(2, this.retryCount) * 1000; // Exponential backoff
        setTimeout(() => this.updateLocationWithRetry(locationData), delay);
      } else {
        console.error('Max retries reached for location update');
        this.retryCount = 0;
      }
    }
  }
}
```

### **Offline Handling:**
```typescript
// Cache location updates when offline
class OfflineLocationCache {
  private pendingUpdates: CaregiverLocationUpdate[] = [];

  cacheLocationUpdate(locationData: CaregiverLocationUpdate) {
    this.pendingUpdates.push({
      ...locationData,
      timestamp: new Date().toISOString()
    });
  }

  async syncPendingUpdates() {
    while (this.pendingUpdates.length > 0) {
      const update = this.pendingUpdates.shift()!;
      try {
        await this.updateLocation(update);
      } catch (error) {
        // Put it back if it fails
        this.pendingUpdates.unshift(update);
        throw error;
      }
    }
  }
}
```

## 🚀 **Testing Your Implementation**

### **Test Checklist:**
- [ ] Patient can create booking with current location
- [ ] Caregiver location updates are sent regularly
- [ ] Live tracking shows real-time caregiver position
- [ ] ETA updates as caregiver moves
- [ ] App handles GPS permission requests
- [ ] App works with location services disabled (graceful degradation)
- [ ] Battery usage is optimized
- [ ] Offline scenarios are handled

This comprehensive guide should give your frontend team everything they need to implement the coordinate system correctly!