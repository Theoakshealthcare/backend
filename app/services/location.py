import math
from typing import List, Tuple, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import and_, func

from app.db.models.booking import CaregiverLocation
from app.db.models.caregiver import Caregiver


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
                             radius_km: float = 10, service_type: str = None) -> List[Dict[str, Any]]:
        """Find caregivers within specified radius"""
        
        # Base query for active caregivers with recent location data
        query = self.db.query(
            CaregiverLocation,
            Caregiver
        ).join(
            Caregiver, CaregiverLocation.caregiver_id == Caregiver.id
        ).filter(
            and_(
                CaregiverLocation.is_active == True,
                CaregiverLocation.is_available == True,
                Caregiver.profile_status == 'approved'
            )
        )

        # Filter by specialization if specified
        if service_type:
            query = query.filter(Caregiver.specialization.ilike(f'%{service_type}%'))

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
