import math
from typing import List, Tuple, Dict, Any, Optional
from uuid import UUID
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import and_, exists, func

from app.core.config import settings
from app.db.models.booking import CaregiverLocation, Booking
from app.db.models.caregiver import Caregiver, CaregiverSkill


class LocationService:
    def __init__(self, db: Session):
        self.db = db

    def calculate_distance(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate distance between two points using Haversine formula"""
        R = 6371  # Earth's radius in kilometers

        lat1_rad = math.radians(lat1)
        lon1_rad = math.radians(lon1)
        lat2_rad = math.radians(lat2)
        lon2_rad = math.radians(lon2)

        dlat = lat2_rad - lat1_rad
        dlon = lon2_rad - lon1_rad

        a = (math.sin(dlat / 2) ** 2 + 
             math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2) ** 2)
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

        return R * c

    def find_nearby_caregivers(self, patient_lat: float, patient_lon: float, 
                             radius_km: float = 10, service_type: str = None,
                             now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Find matchable caregivers within the radius.

        A caregiver is matchable when approved and not suspended, available, and their location was
        reported within LOCATION_FRESHNESS_MINUTES of ``now`` (UTC; injectable for tests). When
        ``service_type`` is given they must also hold a caregiver_skills row for it.
        """
        now = now or datetime.utcnow()
        fresh_after = now - timedelta(minutes=settings.LOCATION_FRESHNESS_MINUTES)

        query = self.db.query(
            CaregiverLocation,
            Caregiver
        ).join(
            Caregiver, CaregiverLocation.caregiver_id == Caregiver.id
        ).filter(
            and_(
                CaregiverLocation.is_active == True,
                CaregiverLocation.is_available == True,
                CaregiverLocation.updated_at >= fresh_after,
                Caregiver.profile_status == 'approved',
                Caregiver.suspended_at.is_(None),
            )
        )

        if service_type:
            query = query.filter(exists().where(and_(
                CaregiverSkill.caregiver_id == Caregiver.id,
                CaregiverSkill.service_type == service_type,
            )))

        locations = query.all()
        
        nearby_caregivers = []
        
        for location, caregiver in locations:
            distance = self.calculate_distance(
                patient_lat, patient_lon,
                location.latitude, location.longitude
            )
            
            if distance <= radius_km:
                nearby_caregivers.append({
                    'caregiver_id': caregiver.id,
                    'user_id': caregiver.user_id,
                    'full_name': caregiver.full_name,
                    'specialization': caregiver.specialization,
                    'years_of_experience': caregiver.years_of_experience,
                    'distance_km': round(distance, 2),
                    'latitude': location.latitude,
                    'longitude': location.longitude,
                    'is_available': location.is_available
                })
        
        # Sort by distance
        nearby_caregivers.sort(key=lambda x: x['distance_km'])
        
        return nearby_caregivers

    def update_caregiver_location(self, caregiver_id: str, latitude: float, 
                                longitude: float, **kwargs) -> CaregiverLocation:
        """Update caregiver's current location"""
        
        # Get existing location record or create new one
        location = self.db.query(CaregiverLocation).filter(
            CaregiverLocation.caregiver_id == caregiver_id
        ).first()
        
        if location:
            location.latitude = latitude
            location.longitude = longitude
            location.accuracy = kwargs.get('accuracy')
            location.heading = kwargs.get('heading')
            location.speed = kwargs.get('speed')
            location.is_available = kwargs.get('is_available', True)
            # onupdate only fires when a column value changes; a stationary caregiver re-sending the
            # same fix must still count as fresh for matching.
            location.updated_at = datetime.utcnow()
        else:
            location = CaregiverLocation(
                caregiver_id=caregiver_id,
                latitude=latitude,
                longitude=longitude,
                accuracy=kwargs.get('accuracy'),
                heading=kwargs.get('heading'),
                speed=kwargs.get('speed'),
                is_available=kwargs.get('is_available', True)
            )
            self.db.add(location)
        
        self.db.commit()
        self.db.refresh(location)
        return location

    def get_caregiver_location(self, caregiver_id: str) -> CaregiverLocation:
        """Get caregiver's current location"""
        return self.db.query(CaregiverLocation).filter(
            CaregiverLocation.caregiver_id == caregiver_id
        ).first()

    def set_caregiver_availability(self, caregiver_id: str, is_available: bool) -> bool:
        """Update caregiver availability status"""
        location = self.db.query(CaregiverLocation).filter(
            CaregiverLocation.caregiver_id == caregiver_id
        ).first()
        
        if location:
            location.is_available = is_available
            self.db.commit()
            return True
        
        return False

    def get_live_caregiver_tracking(self, booking_id: UUID) -> Optional[Dict[str, Any]]:
        """Get real-time caregiver location for active booking (Uber-like tracking)"""
        
        # Get booking details
        booking = self.db.query(Booking).filter(Booking.id == booking_id).first()
        if not booking or not booking.caregiver_id:
            return None
            
        # Get caregiver location
        location = self.get_caregiver_location(booking.caregiver_id)
        if not location:
            return None
        
        # Calculate live distance and ETA
        distance = self.calculate_distance(
            booking.patient_latitude, booking.patient_longitude,
            location.latitude, location.longitude
        )
        
        # Calculate ETA based on current conditions
        # Base speed: 25 km/h for urban areas, adjust based on time of day
        base_speed = 25  # km/h
        current_hour = datetime.now().hour
        
        # Rush hour adjustment (7-9 AM, 5-7 PM)
        if (7 <= current_hour <= 9) or (17 <= current_hour <= 19):
            base_speed = 15  # Slower during rush hour
        elif 22 <= current_hour or current_hour <= 6:
            base_speed = 35  # Faster during late night/early morning
        
        # Minimum 5 minutes for very close distances
        eta_minutes = max(5, int((distance / base_speed) * 60))
        
        # Determine movement status
        is_moving = location.speed and location.speed > 0.5  # Moving if speed > 0.5 m/s
        
        return {
            'booking_id': booking_id,
            'caregiver_id': booking.caregiver_id,
            'latitude': location.latitude,
            'longitude': location.longitude,
            'distance_km': round(distance, 2),
            'estimated_arrival_minutes': eta_minutes,
            'last_updated': location.updated_at,
            'is_moving': is_moving,
            'accuracy': location.accuracy,
            'heading': location.heading,
            'speed': location.speed
        }

    def get_nearby_active_caregivers_with_eta(self, patient_lat: float, patient_lon: float, 
                                            radius_km: float = 15, service_type: str = None,
                                            now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Enhanced nearby caregivers search with ETA calculation (Uber-like)"""
        
        # Get base nearby caregivers
        nearby_caregivers = self.find_nearby_caregivers(
            patient_lat, patient_lon, radius_km, service_type, now=now
        )
        
        # Enhance with real-time ETA and scoring
        enhanced_caregivers = []
        current_hour = datetime.now().hour
        
        # Determine base speed based on time
        base_speed = 25  # km/h
        if (7 <= current_hour <= 9) or (17 <= current_hour <= 19):
            base_speed = 15  # Rush hour
        elif 22 <= current_hour or current_hour <= 6:
            base_speed = 35  # Late night/early morning
        
        for caregiver in nearby_caregivers:
            # Calculate ETA
            eta_minutes = max(5, int((caregiver['distance_km'] / base_speed) * 60))
            
            # Calculate match score (Uber-like algorithm)
            score = 100  # Base score
            
            # Distance penalty (closer = higher score)
            distance_score = max(0, 50 - (caregiver['distance_km'] * 2))
            
            # Experience bonus
            experience_score = min(25, caregiver.get('years_of_experience', 0) * 2)
            
            # Availability bonus (recently active locations get higher score)
            availability_score = 15 if caregiver['is_available'] else 0
            
            # ETA bonus (faster arrival = higher score)
            eta_score = max(0, 30 - (eta_minutes // 2))
            
            total_score = score + distance_score + experience_score + availability_score + eta_score
            
            enhanced_caregiver = caregiver.copy()
            enhanced_caregiver.update({
                'estimated_arrival_minutes': eta_minutes,
                'match_score': round(total_score, 2),
                'base_speed_kmh': base_speed
            })
            
            enhanced_caregivers.append(enhanced_caregiver)
        
        # Sort by match score (highest first)
        enhanced_caregivers.sort(key=lambda x: x['match_score'], reverse=True)
        
        return enhanced_caregivers

    def track_caregiver_route_to_patient(self, booking_id: UUID) -> Optional[Dict[str, Any]]:
        """Track caregiver's route progress to patient location"""
        
        booking = self.db.query(Booking).filter(Booking.id == booking_id).first()
        if not booking or not booking.caregiver_id:
            return None
        
        # Get recent location history (last 10 minutes)
        recent_locations = self.db.query(CaregiverLocation).filter(
            and_(
                CaregiverLocation.caregiver_id == booking.caregiver_id,
                CaregiverLocation.updated_at >= datetime.utcnow() - timedelta(minutes=10)
            )
        ).order_by(CaregiverLocation.updated_at.desc()).limit(5).all()
        
        if not recent_locations:
            return None
        
        current_location = recent_locations[0]
        
        # Calculate current distance
        current_distance = self.calculate_distance(
            booking.patient_latitude, booking.patient_longitude,
            current_location.latitude, current_location.longitude
        )
        
        # Calculate movement pattern if we have historical data
        movement_data = {
            'current_distance_km': round(current_distance, 2),
            'is_approaching': False,
            'average_speed_kmh': 0,
            'estimated_arrival_minutes': max(5, int((current_distance / 25) * 60))
        }
        
        if len(recent_locations) >= 2:
            # Calculate if approaching (distance decreasing)
            prev_location = recent_locations[1]
            prev_distance = self.calculate_distance(
                booking.patient_latitude, booking.patient_longitude,
                prev_location.latitude, prev_location.longitude
            )
            
            movement_data['is_approaching'] = current_distance < prev_distance
            
            # Calculate average speed
            time_diff = (current_location.updated_at - prev_location.updated_at).total_seconds() / 3600  # hours
            distance_traveled = self.calculate_distance(
                prev_location.latitude, prev_location.longitude,
                current_location.latitude, current_location.longitude
            )
            
            if time_diff > 0:
                avg_speed = distance_traveled / time_diff
                movement_data['average_speed_kmh'] = round(avg_speed, 2)
                
                # Update ETA based on actual speed
                if avg_speed > 0:
                    movement_data['estimated_arrival_minutes'] = max(5, int((current_distance / avg_speed) * 60))
        
        return movement_data
