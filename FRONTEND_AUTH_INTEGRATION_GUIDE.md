# Frontend Authentication Integration Guide

This guide explains how to integrate with Oaklycare's new refresh token authentication system in your frontend application.

## Overview

The authentication system now uses:
- **Access Tokens**: Short-lived (60 minutes) for API requests
- **Refresh Tokens**: Long-lived (7 days) for obtaining new access tokens
- **Token Rotation**: New refresh tokens are issued with each refresh

## API Endpoints

### Authentication Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/auth/login` | POST | OAuth2 compatible login |
| `/api/v1/auth/login/mobile` | POST | Mobile app login |
| `/api/v1/auth/refresh` | POST | Refresh access token |

## Implementation Guide

### 1. Login Flow

#### Request
```javascript
// Web/OAuth2 Login
const loginResponse = await fetch('/api/v1/auth/login', {
  method: 'POST',
  headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
  body: new URLSearchParams({
    username: 'user@example.com',
    password: 'password123'
  })
});

// Mobile Login (Alternative)
const mobileLoginResponse = await fetch('/api/v1/auth/login/mobile', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    email_or_phone: 'user@example.com',
    password: 'password123'
  })
});
```

#### Response
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer"
}
```

### 2. Token Storage

Store tokens securely in your frontend:

```javascript
// Secure token storage
class TokenManager {
  static setTokens(accessToken, refreshToken) {
    // Use secure storage (localStorage, sessionStorage, or secure cookie)
    localStorage.setItem('access_token', accessToken);
    localStorage.setItem('refresh_token', refreshToken);
  }
  
  static getAccessToken() {
    return localStorage.getItem('access_token');
  }
  
  static getRefreshToken() {
    return localStorage.getItem('refresh_token');
  }
  
  static clearTokens() {
    localStorage.removeItem('access_token');
    localStorage.removeItem('refresh_token');
  }
}
```

### 3. API Request Interceptor

Automatically attach access tokens to requests:

```javascript
// Axios interceptor example
axios.interceptors.request.use((config) => {
  const accessToken = TokenManager.getAccessToken();
  if (accessToken) {
    config.headers.Authorization = `Bearer ${accessToken}`;
  }
  return config;
});
```

### 4. Token Refresh Implementation

#### Automatic Token Refresh
```javascript
class AuthService {
  static async refreshToken() {
    const refreshToken = TokenManager.getRefreshToken();
    if (!refreshToken) {
      throw new Error('No refresh token available');
    }

    try {
      const response = await fetch('/api/v1/auth/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          refresh_token: refreshToken
        })
      });

      if (!response.ok) {
        throw new Error('Token refresh failed');
      }

      const data = await response.json();
      
      // Store new tokens (both access and refresh rotate!)
      TokenManager.setTokens(data.access_token, data.refresh_token);
      
      return data.access_token;
    } catch (error) {
      // Refresh failed - redirect to login
      TokenManager.clearTokens();
      window.location.href = '/login';
      throw error;
    }
  }
}
```

#### Response Interceptor for Auto-Refresh
```javascript
// Axios response interceptor for handling 401 errors
axios.interceptors.response.use(
  (response) => response,
  async (error) => {
    const originalRequest = error.config;
    
    if (error.response?.status === 401 && !originalRequest._retry) {
      originalRequest._retry = true;
      
      try {
        const newAccessToken = await AuthService.refreshToken();
        originalRequest.headers.Authorization = `Bearer ${newAccessToken}`;
        return axios(originalRequest);
      } catch (refreshError) {
        // Refresh failed, redirect to login
        return Promise.reject(refreshError);
      }
    }
    
    return Promise.reject(error);
  }
);
```

### 5. Complete Authentication Service

```javascript
class AuthService {
  static async login(credentials) {
    const response = await fetch('/api/v1/auth/login/mobile', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(credentials)
    });

    if (!response.ok) {
      throw new Error('Login failed');
    }

    const data = await response.json();
    TokenManager.setTokens(data.access_token, data.refresh_token);
    return data;
  }

  static async logout() {
    TokenManager.clearTokens();
    // Optional: Call logout endpoint if available
    // await fetch('/api/v1/auth/logout', { method: 'POST' });
  }

  static isAuthenticated() {
    return !!TokenManager.getAccessToken();
  }

  static async getCurrentUser() {
    const response = await fetch('/api/v1/auth/me', {
      headers: {
        'Authorization': `Bearer ${TokenManager.getAccessToken()}`
      }
    });
    return response.json();
  }
}
```

### 6. React Hook Example

```javascript
import { useState, useEffect, createContext, useContext } from 'react';

const AuthContext = createContext();

export const AuthProvider = ({ children }) => {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const initAuth = async () => {
      if (TokenManager.getAccessToken()) {
        try {
          const userData = await AuthService.getCurrentUser();
          setUser(userData);
        } catch (error) {
          TokenManager.clearTokens();
        }
      }
      setLoading(false);
    };

    initAuth();
  }, []);

  const login = async (credentials) => {
    const data = await AuthService.login(credentials);
    const userData = await AuthService.getCurrentUser();
    setUser(userData);
    return data;
  };

  const logout = () => {
    AuthService.logout();
    setUser(null);
  };

  return (
    <AuthContext.Provider value={{ user, login, logout, loading }}>
      {children}
    </AuthContext.Provider>
  );
};

export const useAuth = () => {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within AuthProvider');
  }
  return context;
};
```

## Security Best Practices

### 1. Secure Token Storage
```javascript
// For web apps - consider httpOnly cookies for enhanced security
// For mobile apps - use secure keychain/keystore

// Example with js-cookie
import Cookies from 'js-cookie';

class SecureTokenManager {
  static setTokens(accessToken, refreshToken) {
    // Store access token in memory or short-lived cookie
    sessionStorage.setItem('access_token', accessToken);
    
    // Store refresh token in httpOnly cookie (if possible)
    Cookies.set('refresh_token', refreshToken, {
      httpOnly: true,
      secure: true,
      sameSite: 'strict',
      expires: 7 // 7 days
    });
  }
}
```

### 2. Token Validation
```javascript
// Decode and validate token expiry
function isTokenExpired(token) {
  try {
    const payload = JSON.parse(atob(token.split('.')[1]));
    return payload.exp * 1000 < Date.now();
  } catch {
    return true;
  }
}

// Check before making requests
if (isTokenExpired(TokenManager.getAccessToken())) {
  await AuthService.refreshToken();
}
```

### 3. Error Handling
```javascript
const handleApiError = (error) => {
  if (error.response?.status === 401) {
    // Token expired or invalid
    AuthService.logout();
    window.location.href = '/login';
  } else if (error.response?.status === 403) {
    // Insufficient permissions
    console.error('Access denied');
  }
  // Handle other errors...
};
```

## Mobile App Integration

### React Native Example
```javascript
import AsyncStorage from '@react-native-async-storage/async-storage';

class MobileTokenManager {
  static async setTokens(accessToken, refreshToken) {
    await AsyncStorage.multiSet([
      ['access_token', accessToken],
      ['refresh_token', refreshToken]
    ]);
  }
  
  static async getAccessToken() {
    return await AsyncStorage.getItem('access_token');
  }
  
  static async getRefreshToken() {
    return await AsyncStorage.getItem('refresh_token');
  }
  
  static async clearTokens() {
    await AsyncStorage.multiRemove(['access_token', 'refresh_token']);
  }
}
```

## Testing

### Test Token Refresh Flow
```javascript
// Test automatic token refresh
test('should refresh token on 401 response', async () => {
  // Mock 401 response
  mockAxios.onGet('/api/v1/auth/me').replyOnce(401);
  
  // Mock successful refresh
  mockAxios.onPost('/api/v1/auth/refresh').replyOnce(200, {
    access_token: 'new_access_token',
    refresh_token: 'new_refresh_token',
    token_type: 'bearer'
  });
  
  // Mock successful retry
  mockAxios.onGet('/api/v1/auth/me').replyOnce(200, { user: 'data' });
  
  const result = await AuthService.getCurrentUser();
  expect(result.user).toBe('data');
});
```

## Troubleshooting

### Common Issues

1. **Token not rotating**: Ensure you store the new `refresh_token` from each refresh response
2. **Infinite refresh loops**: Check for proper error handling in refresh logic
3. **CORS issues**: Configure backend CORS settings for your frontend domain
4. **Token storage**: Use appropriate storage for your platform (localStorage, AsyncStorage, etc.)

### Debug Logging
```javascript
// Add logging to track token lifecycle
class DebugAuthService extends AuthService {
  static async refreshToken() {
    console.log('Attempting token refresh...');
    try {
      const result = await super.refreshToken();
      console.log('Token refresh successful');
      return result;
    } catch (error) {
      console.error('Token refresh failed:', error);
      throw error;
    }
  }
}
```

## Migration from Old System

If migrating from the old 8-day token system:

1. Update login handlers to store both tokens
2. Implement refresh logic before token expiry
3. Update error handling for 401 responses
4. Test thoroughly in development environment

## Support

For issues with authentication integration:
- Check API response status codes
- Verify token format and expiry
- Review network requests in browser dev tools
- Contact backend team for API-specific issues