# Vitals API Frontend Integration Guide

This guide provides comprehensive documentation for integrating with Oaklycare's Vitals API in your frontend application.

## Overview

The Vitals API allows patients and authorized caregivers to manage patient health data including:
- Recording vital signs (blood pressure, heart rate, temperature, etc.)
- Viewing historical vital data with filtering
- Managing caregiver access permissions
- Exporting data as CSV
- Getting AI-powered health insights

## Base URL
All endpoints are prefixed with: `/api/v1/vitals`

## Authentication
All endpoints require Bearer token authentication:
```javascript
headers: {
  'Authorization': `Bearer ${accessToken}`
}
```

## Access Control

### Patient Access
- Patients can access **only their own** vital data
- Patients can grant/revoke caregiver access to their data

### Caregiver Access
Caregivers can access patient vitals if they have:
- **Active booking** with the patient (status: "active" or "in_progress"), OR
- **Explicit access** granted by the patient

## API Endpoints

### 1. Create Vital Record

**Endpoint:** `POST /api/v1/vitals/`

**Request Body:**
```json
{
  "patient_id": "123e4567-e89b-12d3-a456-426614174000",
  "vital_type": "blood_pressure",
  "value": "120/80",
  "unit": "mmHg",
  "notes": "Patient was resting",
  "recorded_at": "2024-01-15T10:30:00Z"
}
```

**Response:**
```json
{
  "id": "456e7890-e89b-12d3-a456-426614174111",
  "patient_id": "123e4567-e89b-12d3-a456-426614174000",
  "vital_type": "blood_pressure",
  "value": "120/80",
  "unit": "mmHg",
  "notes": "Patient was resting",
  "recorded_at": "2024-01-15T10:30:00Z",
  "created_at": "2024-01-15T10:30:00Z",
  "updated_at": "2024-01-15T10:30:00Z"
}
```

**JavaScript Example:**
```javascript
const createVital = async (vitalData) => {
  try {
    const response = await fetch('/api/v1/vitals/', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${getAccessToken()}`
      },
      body: JSON.stringify(vitalData)
    });

    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }

    return await response.json();
  } catch (error) {
    console.error('Error creating vital:', error);
    throw error;
  }
};
```

### 2. Get Patient Vitals

**Endpoint:** `GET /api/v1/vitals/{patient_id}`

**Query Parameters:**
- `start` (optional): Start date filter (ISO format)
- `end` (optional): End date filter (ISO format)

**Example:** `/api/v1/vitals/123e4567-e89b-12d3-a456-426614174000?start=2024-01-01T00:00:00Z&end=2024-01-31T23:59:59Z`

**Response:**
```json
[
  {
    "id": "456e7890-e89b-12d3-a456-426614174111",
    "patient_id": "123e4567-e89b-12d3-a456-426614174000",
    "vital_type": "blood_pressure",
    "value": "120/80",
    "unit": "mmHg",
    "notes": "Morning reading",
    "recorded_at": "2024-01-15T10:30:00Z",
    "created_at": "2024-01-15T10:30:00Z",
    "updated_at": "2024-01-15T10:30:00Z"
  }
]
```

**JavaScript Example:**
```javascript
const getPatientVitals = async (patientId, startDate = null, endDate = null) => {
  try {
    let url = `/api/v1/vitals/${patientId}`;
    const params = new URLSearchParams();
    
    if (startDate) params.append('start', startDate);
    if (endDate) params.append('end', endDate);
    
    if (params.toString()) {
      url += `?${params.toString()}`;
    }

    const response = await fetch(url, {
      headers: {
        'Authorization': `Bearer ${getAccessToken()}`
      }
    });

    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }

    return await response.json();
  } catch (error) {
    console.error('Error fetching vitals:', error);
    throw error;
  }
};
```

### 3. Update Vital Record

**Endpoint:** `PUT /api/v1/vitals/{vital_id}`

**Request Body:**
```json
{
  "value": "125/82",
  "notes": "Updated reading after rest",
  "recorded_at": "2024-01-15T10:35:00Z"
}
```

**JavaScript Example:**
```javascript
const updateVital = async (vitalId, updateData) => {
  try {
    const response = await fetch(`/api/v1/vitals/${vitalId}`, {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${getAccessToken()}`
      },
      body: JSON.stringify(updateData)
    });

    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }

    return await response.json();
  } catch (error) {
    console.error('Error updating vital:', error);
    throw error;
  }
};
```

### 4. Delete Vital Record

**Endpoint:** `DELETE /api/v1/vitals/{vital_id}`

**Response:**
```json
{
  "ok": true
}
```

**JavaScript Example:**
```javascript
const deleteVital = async (vitalId) => {
  try {
    const response = await fetch(`/api/v1/vitals/${vitalId}`, {
      method: 'DELETE',
      headers: {
        'Authorization': `Bearer ${getAccessToken()}`
      }
    });

    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }

    return await response.json();
  } catch (error) {
    console.error('Error deleting vital:', error);
    throw error;
  }
};
```

### 5. Download Vitals as CSV

**Endpoint:** `GET /api/v1/vitals/{patient_id}/download`

**Response:** CSV file download

**JavaScript Example:**
```javascript
const downloadVitalsCSV = async (patientId) => {
  try {
    const response = await fetch(`/api/v1/vitals/${patientId}/download`, {
      headers: {
        'Authorization': `Bearer ${getAccessToken()}`
      }
    });

    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }

    // Create download link
    const blob = await response.blob();
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `vitals_${patientId}.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    window.URL.revokeObjectURL(url);
  } catch (error) {
    console.error('Error downloading vitals:', error);
    throw error;
  }
};
```

### 6. Get AI Insights

**Endpoint:** `GET /api/v1/vitals/{patient_id}/insights`

**Response:**
```json
{
  "insights": "Based on the vital signs data, the patient shows consistent blood pressure readings within normal range...",
  "recommendations": [
    "Continue current medication regimen",
    "Monitor blood pressure weekly",
    "Maintain current exercise routine"
  ],
  "trends": {
    "blood_pressure": "stable",
    "heart_rate": "improving"
  }
}
```

**JavaScript Example:**
```javascript
const getVitalsInsights = async (patientId) => {
  try {
    const response = await fetch(`/api/v1/vitals/${patientId}/insights`, {
      headers: {
        'Authorization': `Bearer ${getAccessToken()}`
      }
    });

    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }

    return await response.json();
  } catch (error) {
    console.error('Error getting insights:', error);
    throw error;
  }
};
```

## Access Management

### 7. Grant Caregiver Access

**Endpoint:** `POST /api/v1/vitals/access/grant`
**Note:** Only patients can grant access to their own data

**Request Body:**
```json
{
  "patient_id": "123e4567-e89b-12d3-a456-426614174000",
  "caregiver_id": "789e0123-e89b-12d3-a456-426614174222",
  "access_level": "read_write",
  "expires_at": "2024-06-01T00:00:00Z",
  "notes": "Access for regular monitoring"
}
```

**Response:**
```json
{
  "id": "abc12345-e89b-12d3-a456-426614174333",
  "patient_id": "123e4567-e89b-12d3-a456-426614174000",
  "caregiver_id": "789e0123-e89b-12d3-a456-426614174222",
  "access_level": "read_write",
  "expires_at": "2024-06-01T00:00:00Z",
  "notes": "Access for regular monitoring",
  "granted_at": "2024-01-15T10:30:00Z",
  "is_active": true
}
```

**JavaScript Example:**
```javascript
const grantCaregiverAccess = async (accessData) => {
  try {
    const response = await fetch('/api/v1/vitals/access/grant', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${getAccessToken()}`
      },
      body: JSON.stringify(accessData)
    });

    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }

    return await response.json();
  } catch (error) {
    console.error('Error granting access:', error);
    throw error;
  }
};
```

### 8. Revoke Caregiver Access

**Endpoint:** `POST /api/v1/vitals/access/revoke`

**Request Body:**
```json
{
  "patient_id": "123e4567-e89b-12d3-a456-426614174000",
  "caregiver_id": "789e0123-e89b-12d3-a456-426614174222"
}
```

**JavaScript Example:**
```javascript
const revokeCaregiverAccess = async (patientId, caregiverId) => {
  try {
    const response = await fetch('/api/v1/vitals/access/revoke', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${getAccessToken()}`
      },
      body: JSON.stringify({
        patient_id: patientId,
        caregiver_id: caregiverId
      })
    });

    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }

    return await response.json();
  } catch (error) {
    console.error('Error revoking access:', error);
    throw error;
  }
};
```

### 9. List Caregiver Access

**Endpoint:** `GET /api/v1/vitals/access/list`
**Note:** Only patients can list their granted access

**Response:**
```json
[
  {
    "id": "abc12345-e89b-12d3-a456-426614174333",
    "patient_id": "123e4567-e89b-12d3-a456-426614174000",
    "caregiver_id": "789e0123-e89b-12d3-a456-426614174222",
    "caregiver_name": "Dr. Smith",
    "access_level": "read_write",
    "expires_at": "2024-06-01T00:00:00Z",
    "granted_at": "2024-01-15T10:30:00Z",
    "is_active": true
  }
]
```

**JavaScript Example:**
```javascript
const listCaregiverAccess = async () => {
  try {
    const response = await fetch('/api/v1/vitals/access/list', {
      headers: {
        'Authorization': `Bearer ${getAccessToken()}`
      }
    });

    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }

    return await response.json();
  } catch (error) {
    console.error('Error listing access:', error);
    throw error;
  }
};
```

## Complete Vitals Service Class

```javascript
class VitalsService {
  constructor(baseURL = '/api/v1/vitals') {
    this.baseURL = baseURL;
  }

  async request(endpoint, options = {}) {
    const url = `${this.baseURL}${endpoint}`;
    const config = {
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${getAccessToken()}`,
        ...options.headers
      },
      ...options
    };

    const response = await fetch(url, config);

    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `HTTP error! status: ${response.status}`);
    }

    // Handle CSV downloads
    if (response.headers.get('content-type')?.includes('text/csv')) {
      return response.blob();
    }

    return response.json();
  }

  // Vital Records
  async createVital(vitalData) {
    return this.request('/', {
      method: 'POST',
      body: JSON.stringify(vitalData)
    });
  }

  async getPatientVitals(patientId, filters = {}) {
    const params = new URLSearchParams();
    if (filters.start) params.append('start', filters.start);
    if (filters.end) params.append('end', filters.end);
    
    const query = params.toString() ? `?${params.toString()}` : '';
    return this.request(`/${patientId}${query}`);
  }

  async updateVital(vitalId, updateData) {
    return this.request(`/${vitalId}`, {
      method: 'PUT',
      body: JSON.stringify(updateData)
    });
  }

  async deleteVital(vitalId) {
    return this.request(`/${vitalId}`, {
      method: 'DELETE'
    });
  }

  // Data Export & Insights
  async downloadVitalsCSV(patientId) {
    const blob = await this.request(`/${patientId}/download`);
    
    // Create download
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `vitals_${patientId}.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    window.URL.revokeObjectURL(url);
  }

  async getVitalsInsights(patientId) {
    return this.request(`/${patientId}/insights`);
  }

  // Access Management
  async grantCaregiverAccess(accessData) {
    return this.request('/access/grant', {
      method: 'POST',
      body: JSON.stringify(accessData)
    });
  }

  async revokeCaregiverAccess(patientId, caregiverId) {
    return this.request('/access/revoke', {
      method: 'POST',
      body: JSON.stringify({
        patient_id: patientId,
        caregiver_id: caregiverId
      })
    });
  }

  async listCaregiverAccess() {
    return this.request('/access/list');
  }
}

// Usage
const vitalsService = new VitalsService();
```

## React Hook Example

```javascript
import { useState, useEffect } from 'react';

const useVitals = (patientId) => {
  const [vitals, setVitals] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const vitalsService = new VitalsService();

  const loadVitals = async (filters = {}) => {
    setLoading(true);
    setError(null);
    try {
      const data = await vitalsService.getPatientVitals(patientId, filters);
      setVitals(data);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const createVital = async (vitalData) => {
    try {
      const newVital = await vitalsService.createVital(vitalData);
      setVitals(prev => [newVital, ...prev]);
      return newVital;
    } catch (err) {
      setError(err.message);
      throw err;
    }
  };

  const updateVital = async (vitalId, updateData) => {
    try {
      const updatedVital = await vitalsService.updateVital(vitalId, updateData);
      setVitals(prev => prev.map(v => v.id === vitalId ? updatedVital : v));
      return updatedVital;
    } catch (err) {
      setError(err.message);
      throw err;
    }
  };

  const deleteVital = async (vitalId) => {
    try {
      await vitalsService.deleteVital(vitalId);
      setVitals(prev => prev.filter(v => v.id !== vitalId));
    } catch (err) {
      setError(err.message);
      throw err;
    }
  };

  useEffect(() => {
    if (patientId) {
      loadVitals();
    }
  }, [patientId]);

  return {
    vitals,
    loading,
    error,
    loadVitals,
    createVital,
    updateVital,
    deleteVital
  };
};

// Component usage
const VitalsComponent = ({ patientId }) => {
  const { vitals, loading, error, createVital } = useVitals(patientId);

  if (loading) return <div>Loading vitals...</div>;
  if (error) return <div>Error: {error}</div>;

  return (
    <div>
      <h2>Patient Vitals</h2>
      {vitals.map(vital => (
        <div key={vital.id}>
          {vital.vital_type}: {vital.value} {vital.unit}
        </div>
      ))}
    </div>
  );
};
```

## Common Vital Types

```javascript
const VITAL_TYPES = {
  BLOOD_PRESSURE: 'blood_pressure',
  HEART_RATE: 'heart_rate',
  TEMPERATURE: 'temperature',
  WEIGHT: 'weight',
  HEIGHT: 'height',
  OXYGEN_SATURATION: 'oxygen_saturation',
  BLOOD_GLUCOSE: 'blood_glucose',
  RESPIRATORY_RATE: 'respiratory_rate'
};

const VITAL_UNITS = {
  [VITAL_TYPES.BLOOD_PRESSURE]: 'mmHg',
  [VITAL_TYPES.HEART_RATE]: 'bpm',
  [VITAL_TYPES.TEMPERATURE]: '°F',
  [VITAL_TYPES.WEIGHT]: 'lbs',
  [VITAL_TYPES.HEIGHT]: 'ft/in',
  [VITAL_TYPES.OXYGEN_SATURATION]: '%',
  [VITAL_TYPES.BLOOD_GLUCOSE]: 'mg/dL',
  [VITAL_TYPES.RESPIRATORY_RATE]: 'breaths/min'
};
```

## Error Handling

```javascript
const handleVitalsError = (error) => {
  switch (error.status) {
    case 401:
      // Unauthorized - token expired
      redirectToLogin();
      break;
    case 403:
      // Forbidden - no access to this patient's data
      showError('You do not have permission to access this patient\'s vitals');
      break;
    case 404:
      // Not found
      showError('Vital record not found');
      break;
    case 422:
      // Validation error
      showError('Invalid data provided');
      break;
    default:
      showError('An unexpected error occurred');
  }
};
```

## Form Validation

```javascript
const validateVitalData = (vitalData) => {
  const errors = {};

  if (!vitalData.patient_id) {
    errors.patient_id = 'Patient ID is required';
  }

  if (!vitalData.vital_type) {
    errors.vital_type = 'Vital type is required';
  }

  if (!vitalData.value) {
    errors.value = 'Value is required';
  }

  // Validate specific vital types
  if (vitalData.vital_type === 'blood_pressure') {
    const bpPattern = /^\d{2,3}\/\d{2,3}$/;
    if (!bpPattern.test(vitalData.value)) {
      errors.value = 'Blood pressure must be in format: 120/80';
    }
  }

  if (vitalData.vital_type === 'heart_rate') {
    const rate = parseInt(vitalData.value);
    if (isNaN(rate) || rate < 30 || rate > 250) {
      errors.value = 'Heart rate must be between 30 and 250 bpm';
    }
  }

  return {
    isValid: Object.keys(errors).length === 0,
    errors
  };
};
```

## Testing

```javascript
// Jest test example
describe('VitalsService', () => {
  let vitalsService;

  beforeEach(() => {
    vitalsService = new VitalsService();
    global.fetch = jest.fn();
  });

  test('should create vital successfully', async () => {
    const mockVital = {
      id: '123',
      vital_type: 'blood_pressure',
      value: '120/80',
      unit: 'mmHg'
    };

    global.fetch.mockResolvedValueOnce({
      ok: true,
      json: jest.fn().mockResolvedValueOnce(mockVital)
    });

    const result = await vitalsService.createVital({
      patient_id: '456',
      vital_type: 'blood_pressure',
      value: '120/80',
      unit: 'mmHg'
    });

    expect(result).toEqual(mockVital);
    expect(global.fetch).toHaveBeenCalledWith(
      '/api/v1/vitals/',
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({
          'Content-Type': 'application/json',
          'Authorization': expect.stringContaining('Bearer')
        })
      })
    );
  });
});
```

## Best Practices

1. **Always validate data** before sending to API
2. **Handle all error cases** appropriately
3. **Use loading states** for better UX
4. **Cache vitals data** when appropriate
5. **Implement offline support** for critical features
6. **Sanitize user input** before display
7. **Use proper date formatting** (ISO 8601)
8. **Implement retry logic** for failed requests

## Troubleshooting

### Common Issues

1. **403 Forbidden**: User doesn't have access to patient data
   - Solution: Verify user has proper permissions or active booking

2. **422 Validation Error**: Invalid data format
   - Solution: Check data format matches expected schema

3. **Token Expired**: Authentication failed
   - Solution: Implement automatic token refresh

4. **CORS Errors**: Cross-origin request blocked
   - Solution: Ensure proper CORS configuration on backend

### Debug Logging

```javascript
const debugVitalsAPI = (operation, data) => {
  if (process.env.NODE_ENV === 'development') {
    console.group(`Vitals API: ${operation}`);
    console.log('Data:', data);
    console.log('Timestamp:', new Date().toISOString());
    console.groupEnd();
  }
};
```

This guide provides everything needed to successfully integrate with the Vitals API, including comprehensive examples, error handling, and best practices.