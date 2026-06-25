import os
import requests
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import pandas as pd
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

class StravaClient:
    """
    A client for interacting with the Strava API.
    Handles authentication and data retrieval.
    """
    
    def __init__(self):
        self.client_id = os.getenv('STRAVA_CLIENT_ID')
        self.client_secret = os.getenv('STRAVA_CLIENT_SECRET')
        self.refresh_token = os.getenv('STRAVA_REFRESH_TOKEN')
        self.access_token = os.getenv('STRAVA_ACCESS_TOKEN')
        
        if not all([self.client_id, self.client_secret, self.refresh_token]):
            raise ValueError("Missing required environment variables. Please check your .env file.")
        
        self.base_url = "https://www.strava.com/api/v3"
        self.session = requests.Session()
        
        # Refresh access token if needed
        self._refresh_access_token()
    
    def _refresh_access_token(self) -> None:
        """Refresh the access token using the refresh token."""
        url = "https://www.strava.com/oauth/token"
        data = {
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'grant_type': 'refresh_token',
            'refresh_token': self.refresh_token
        }
        
        response = requests.post(url, data=data)
        if response.status_code == 200:
            token_data = response.json()
            self.access_token = token_data['access_token']
            self.refresh_token = token_data['refresh_token']
            
            # Update environment variables
            os.environ['STRAVA_ACCESS_TOKEN'] = self.access_token
            os.environ['STRAVA_REFRESH_TOKEN'] = self.refresh_token
            
            print("Access token refreshed successfully")
        else:
            raise Exception(f"Failed to refresh access token: {response.text}")
    
    def _make_request(self, endpoint: str, params: Optional[Dict] = None) -> Dict:
        """Make an authenticated request to the Strava API."""
        headers = {
            'Authorization': f'Bearer {self.access_token}'
        }
        
        url = f"{self.base_url}/{endpoint}"
        response = self.session.get(url, headers=headers, params=params)
        
        if response.status_code == 401:
            # Token expired, refresh and retry
            self._refresh_access_token()
            headers['Authorization'] = f'Bearer {self.access_token}'
            response = self.session.get(url, headers=headers, params=params)
        
        if response.status_code != 200:
            raise Exception(f"API request failed: {response.status_code} - {response.text}")
        
        return response.json()
    
    def get_athlete_profile(self) -> Dict:
        """Get the current athlete's profile information."""
        return self._make_request('athlete')
    
    def get_activities(self, page: int = 1, per_page: int = 30, after: Optional[int] = None) -> List[Dict]:
        """
        Get activities for the authenticated athlete.
        
        Args:
            page: Page number for pagination
            per_page: Number of activities per page (max 200)
            after: Unix timestamp to get activities after this date
        """
        params = {
            'page': page,
            'per_page': min(per_page, 200)
        }
        
        if after:
            params['after'] = after
        
        return self._make_request('athlete/activities', params=params)
    
    def get_activity_by_id(self, activity_id: int) -> Dict:
        """Get detailed information about a specific activity."""
        return self._make_request(f'activities/{activity_id}')
    
    def get_activity_streams(self, activity_id: int, keys: Optional[List[str]] = None) -> Dict:
        """
        Get activity streams (GPS data, heart rate, etc.).
        
        Args:
            activity_id: The ID of the activity
            keys: List of stream types to retrieve (e.g., ['time', 'latlng', 'distance', 'altitude', 'velocity_smooth', 'heartrate', 'cadence', 'watts', 'temp', 'moving', 'grade_smooth'])
        """
        if keys is None:
            keys = ['time', 'latlng', 'distance', 'altitude', 'velocity_smooth', 'heartrate']
        
        params = {
            'keys': ','.join(keys),
            'key_by_type': 'true'
        }
        
        return self._make_request(f'activities/{activity_id}/streams', params=params)
    
    def get_athlete_stats(self, athlete_id: Optional[int] = None) -> Dict:
        """Get athlete statistics."""
        if athlete_id is None:
            # Get current athlete's stats
            return self._make_request('athlete')
        else:
            return self._make_request(f'athletes/{athlete_id}/stats')
    
    def get_recent_activities(self, days: int = 30) -> List[Dict]:
        """Get activities from the last N days."""
        after_date = datetime.now() - timedelta(days=days)
        after_timestamp = int(after_date.timestamp())
        
        activities = []
        page = 1
        
        while True:
            batch = self.get_activities(page=page, per_page=200, after=after_timestamp)
            if not batch:
                break
            
            activities.extend(batch)
            page += 1
            
            # Stop if we get fewer results than requested (end of data)
            if len(batch) < 200:
                break
        
        return activities
    
    def activities_to_dataframe(self, activities: List[Dict]) -> pd.DataFrame:
        """Convert activities list to a pandas DataFrame."""
        if not activities:
            return pd.DataFrame()
        
        # Extract relevant fields
        df_data = []
        for activity in activities:
            df_data.append({
                'id': activity.get('id'),
                'name': activity.get('name'),
                'type': activity.get('type'),
                'distance': activity.get('distance', 0) / 1000,  # Convert to km
                'moving_time': activity.get('moving_time', 0) / 60,  # Convert to minutes
                'elapsed_time': activity.get('elapsed_time', 0) / 60,  # Convert to minutes
                'total_elevation_gain': activity.get('total_elevation_gain', 0),
                'average_speed': activity.get('average_speed', 0) * 3.6,  # Convert to km/h
                'max_speed': activity.get('max_speed', 0) * 3.6,  # Convert to km/h
                'average_heartrate': activity.get('average_heartrate'),
                'max_heartrate': activity.get('max_heartrate'),
                'average_watts': activity.get('average_watts'),
                'max_watts': activity.get('max_watts'),
                'start_date': activity.get('start_date'),
                'start_date_local': activity.get('start_date_local'),
                'location_country': activity.get('location_country'),
                'location_state': activity.get('location_state'),
                'location_city': activity.get('location_city'),
                'kudos_count': activity.get('kudos_count', 0),
                'comment_count': activity.get('comment_count', 0),
                'athlete_count': activity.get('athlete_count', 1),
                'trainer': activity.get('trainer', False),
                'commute': activity.get('commute', False),
                'manual': activity.get('manual', False),
                'private': activity.get('private', False),
                'visibility': activity.get('visibility'),
                'flagged': activity.get('flagged', False),
                'workout_type': activity.get('workout_type'),
                'upload_id': activity.get('upload_id'),
                'external_id': activity.get('external_id'),
                'average_cadence': activity.get('average_cadence'),
                'has_kudoed': activity.get('has_kudoed', False),
                'description': activity.get('description'),
                'calories': activity.get('calories'),
                'segment_efforts': len(activity.get('segment_efforts', [])),
                'splits_metric': len(activity.get('splits_metric', [])),
                'laps': len(activity.get('laps', [])),
                'gear_id': activity.get('gear_id'),
                'gear_name': activity.get('gear', {}).get('name') if activity.get('gear') else None,
                'gear_distance': activity.get('gear', {}).get('distance', 0) / 1000 if activity.get('gear') else 0,
                'gear_primary': activity.get('gear', {}).get('primary', False) if activity.get('gear') else False,
                'gear_brand_name': activity.get('gear', {}).get('brand_name') if activity.get('gear') else None,
                'gear_model_name': activity.get('gear', {}).get('model_name') if activity.get('gear') else None,
                'gear_frame_type': activity.get('gear', {}).get('frame_type') if activity.get('gear') else None,
                'gear_description': activity.get('gear', {}).get('description') if activity.get('gear') else None,
                'gear_distance': activity.get('gear', {}).get('distance', 0) / 1000 if activity.get('gear') else 0,
                'gear_primary': activity.get('gear', {}).get('primary', False) if activity.get('gear') else False,
                'gear_brand_name': activity.get('gear', {}).get('brand_name') if activity.get('gear') else None,
                'gear_model_name': activity.get('gear', {}).get('model_name') if activity.get('gear') else None,
                'gear_frame_type': activity.get('gear', {}).get('frame_type') if activity.get('gear') else None,
                'gear_description': activity.get('gear', {}).get('description') if activity.get('gear') else None,
            })
        
        df = pd.DataFrame(df_data)
        
        # Convert date columns
        if 'start_date' in df.columns:
            df['start_date'] = pd.to_datetime(df['start_date'])
        if 'start_date_local' in df.columns:
            df['start_date_local'] = pd.to_datetime(df['start_date_local'])
        
        return df 