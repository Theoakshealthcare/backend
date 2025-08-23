# Booking Creation API Documentation

## Overview
The booking creation endpoint allows patients to book various healthcare services including nurses, doctors, physiotherapists, caregivers, and nutritionists.

**Endpoint:** `POST /api/v1/booking/create`  
**Authentication:** Required (Patient token)  
**Content-Type:** `application/json`

## Request Schema

### Required Fields

| Field | Type | Description | Validation |
|-------|------|-------------|------------|
| `service_type` | string | Type of healthcare service | One of: "nurse", "doctor", "physiotherapist", "caregiver", "nutritionist" |
| `assistance_type` | string | Specific type of assistance needed | Free text |
| `duration_hours` | integer | Duration of service in hours | Between 1 and 24 hours |
| `scheduled_date` | string | When service should be provided | ISO 8601 datetime format |
| `patient_address` | string | Full address where service will be provided | Full street address |
| `patient_latitude` | number | Latitude coordinate of service location | Valid latitude (-90 to 90) |
| `patient_longitude` | number | Longitude coordinate of service location | Valid longitude (-180 to 180) |
| `patient_city` | string | City where service will be provided | City name |
| `patient_state` | string | State where service will be provided | State name |
| `booking_for` | string | Who the booking is for | Free text |

### Optional Fields

| Field | Type | Description | Default |
|-------|------|-------------|---------|
| `condition` | string | Medical condition or specific needs | null |
| `special_instructions` | string | Additional instructions for caregiver | null |

## Request Example

```json
{
  "service_type": "caregiver",
  "assistance_type": "Personal care and medication assistance",
  "condition": "Diabetes management",
  "duration_hours": 4,
  "scheduled_date": "2024-01-15T14:00:00Z",
  "patient_address": "123 Main Street, Lagos, Nigeria",
  "patient_latitude": 6.5244,
  "patient_longitude": 3.3792,
  "patient_city": "Lagos",
  "patient_state": "Lagos State",
  "special_instructions": "Patient prefers female caregiver. Needs help with insulin injection at 3 PM.",
  "booking_for": "Myself"
}
```

## Response Schema

### Success Response (200 OK)

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "booking_code": "BK-2024-001234",
  "patient_id": "123e4567-e89b-12d3-a456-426614174000",
  "caregiver_id": null,
  "service_type": "caregiver",
  "assistance_type": "Personal care and medication assistance",
  "condition": "Diabetes management",
  "duration_hours": 4,
  "scheduled_date": "2024-01-15T14:00:00Z",
  "scheduled_end_date": "2024-01-15T18:00:00Z",
  "patient_address": "123 Main Street, Lagos, Nigeria",
  "patient_latitude": 6.5244,
  "patient_longitude": 3.3792,
  "patient_city": "Lagos",
  "patient_state": "Lagos State",
  "status": "pending",
  "payment_status": "pending",
  "total_amount": 15000.0,
  "service_fee": 12000.0,
  "platform_fee": 3000.0,
  "special_instructions": "Patient prefers female caregiver. Needs help with insulin injection at 3 PM.",
  "booking_for": "Myself",
  "created_at": "2024-01-14T10:30:00Z",
  "updated_at": "2024-01-14T10:30:00Z"
}
```

### Error Responses

#### 404 - Patient Profile Not Found
```json
{
  "detail": "Patient profile not found"
}
```

#### 422 - Validation Error
```json
{
  "detail": [
    {
      "loc": ["body", "duration_hours"],
      "msg": "Duration must be between 1 and 24 hours",
      "type": "value_error"
    }
  ]
}
```

#### 500 - Internal Server Error
```json
{
  "detail": "Error creating booking"
}
```

## Frontend Integration Guide

### JavaScript/TypeScript Example

```typescript
interface BookingCreateRequest {
  service_type: 'nurse' | 'doctor' | 'physiotherapist' | 'caregiver' | 'nutritionist';
  assistance_type: string;
  condition?: string;
  duration_hours: number;
  scheduled_date: string; // ISO 8601 format
  patient_address: string;
  patient_latitude: number;
  patient_longitude: number;
  patient_city: string;
  patient_state: string;
  special_instructions?: string;
  booking_for: string;
}

interface BookingResponse {
  id: string;
  booking_code: string;
  patient_id: string;
  caregiver_id: string | null;
  service_type: string;
  assistance_type: string;
  condition: string | null;
  duration_hours: number;
  scheduled_date: string;
  scheduled_end_date: string;
  patient_address: string;
  patient_latitude: number;
  patient_longitude: number;
  patient_city: string;
  patient_state: string;
  status: string;
  payment_status: string;
  total_amount: number;
  service_fee: number;
  platform_fee: number;
  special_instructions: string | null;
  booking_for: string;
  created_at: string;
  updated_at: string;
}

async function createBooking(bookingData: BookingCreateRequest): Promise<BookingResponse> {
  const response = await fetch('/api/v1/booking/create', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${patientToken}`, // Replace with actual token
    },
    body: JSON.stringify(bookingData),
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to create booking');
  }

  return response.json();
}
```

### React Hook Example

```typescript
import { useState } from 'react';

interface UseCreateBookingReturn {
  createBooking: (data: BookingCreateRequest) => Promise<void>;
  loading: boolean;
  error: string | null;
  booking: BookingResponse | null;
}

export function useCreateBooking(): UseCreateBookingReturn {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [booking, setBooking] = useState<BookingResponse | null>(null);

  const createBooking = async (data: BookingCreateRequest) => {
    setLoading(true);
    setError(null);
    
    try {
      const result = await fetch('/api/v1/booking/create', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${getAuthToken()}`,
        },
        body: JSON.stringify(data),
      });

      if (!result.ok) {
        const errorData = await result.json();
        throw new Error(errorData.detail || 'Failed to create booking');
      }

      const bookingData = await result.json();
      setBooking(bookingData);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error occurred');
    } finally {
      setLoading(false);
    }
  };

  return { createBooking, loading, error, booking };
}
```

## Important Notes

1. **Authentication**: All requests must include a valid patient authentication token in the Authorization header.

2. **Date Format**: Use ISO 8601 format for `scheduled_date` (e.g., "2024-01-15T14:00:00Z").

3. **Coordinates**: Ensure latitude and longitude are accurate as they're used to find nearby caregivers.

4. **Duration Validation**: Duration must be between 1 and 24 hours.

5. **Service Types**: Only the specified service types are allowed: "nurse", "doctor", "physiotherapist", "caregiver", "nutritionist".

6. **Automatic Processes**: After booking creation:
   - Nearby caregivers are automatically notified
   - Pricing is calculated based on service type and duration
   - A unique booking code is generated

7. **Status Flow**: New bookings start with "pending" status and "pending" payment status.

## Testing

### cURL Example

```bash
curl -X POST "https://your-api-domain.com/api/v1/booking/create" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_PATIENT_TOKEN" \
  -d '{
    "service_type": "caregiver",
    "assistance_type": "Personal care",
    "duration_hours": 4,
    "scheduled_date": "2024-01-15T14:00:00Z",
    "patient_address": "123 Main Street, Lagos, Nigeria",
    "patient_latitude": 6.5244,
    "patient_longitude": 3.3792,
    "patient_city": "Lagos",
    "patient_state": "Lagos State",
    "booking_for": "Myself"
  }'
```

## Error Handling Best Practices

1. Always check response status codes
2. Handle network errors gracefully
3. Display user-friendly error messages
4. Validate form data before sending requests
5. Implement retry logic for network failures
6. Show loading states during API calls