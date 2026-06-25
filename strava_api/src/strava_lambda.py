import json
import boto3
import requests
import time
from datetime import datetime, timedelta
import os
from typing import Dict, List, Optional


def lambda_handler(event, context):
    """
    Lambda function to sync Strava activities to S3
    """
    
    # Environment variables - set these in Lambda configuration
    client_id = os.environ.get('STRAVA_CLIENT_ID')
    client_secret = os.environ.get('STRAVA_CLIENT_SECRET')
    refresh_token = os.environ.get('STRAVA_REFRESH_TOKEN')
    s3_bucket = os.environ.get('S3_BUCKET')

    if not all([client_id, client_secret, refresh_token, s3_bucket]):
        raise ValueError("Missing one or more required environment variables: STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN, S3_BUCKET")
    
    # Initialize S3 client
    s3 = boto3.client('s3')
    
    try:
        # Get fresh access token
        access_token = refresh_strava_token(client_id, client_secret, refresh_token)
        
        # Determine sync strategy
        sync_mode = event.get('sync_mode', 'incremental')  # 'full' or 'incremental'
        
        if sync_mode == 'full':
            # Full sync - get all activities
            activities = get_all_activities(access_token)
        else:
            # Incremental sync - get activities since last sync
            last_sync = get_last_sync_timestamp(s3, s3_bucket)
            activities = get_recent_activities(access_token, last_sync)
        
        # Upload to S3
        upload_results = upload_activities_to_s3(s3, s3_bucket, activities)
        
        # Update last sync timestamp
        update_last_sync_timestamp(s3, s3_bucket)
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Sync completed successfully',
                'activities_processed': len(activities),
                'upload_results': upload_results
            })
        }
        
    except Exception as e:
        print(f"Error: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps({
                'error': str(e)
            })
        }

def refresh_strava_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    """Refresh Strava access token"""
    
    url = "https://www.strava.com/oauth/token"
    payload = {
        'client_id': client_id,
        'client_secret': client_secret,
        'refresh_token': refresh_token,
        'grant_type': 'refresh_token'
    }
    
    response = requests.post(url, data=payload)
    response.raise_for_status()
    
    token_data = response.json()
    return token_data['access_token']

def get_all_activities(access_token: str) -> List[Dict]:
    """Get all activities from Strava API with pagination"""
    
    activities = []
    page = 1
    per_page = 200  # Max allowed by Strava
    
    while True:
        batch = get_activities_page(access_token, page, per_page)
        
        if not batch:
            break
            
        activities.extend(batch)
        
        # If we got less than per_page, we're done
        if len(batch) < per_page:
            break
            
        page += 1
        
        # Rate limiting: 100 requests per 15 minutes
        time.sleep(0.1)  # Small delay to be respectful
    
    return activities

def get_recent_activities(access_token: str, since_timestamp: Optional[int]) -> List[Dict]:
    """Get activities since a specific timestamp"""
    
    activities = []
    page = 1
    per_page = 200
    
    while True:
        params = {
            'page': page,
            'per_page': per_page
        }
        
        if since_timestamp:
            params['after'] = since_timestamp
            
        batch = get_activities_page(access_token, page, per_page, params)
        
        if not batch:
            break
            
        activities.extend(batch)
        
        if len(batch) < per_page:
            break
            
        page += 1
        time.sleep(0.1)
    
    return activities

def get_activities_page(access_token: str, page: int, per_page: int, extra_params: Dict = None) -> List[Dict]:
    """Get a single page of activities"""
    
    url = "https://www.strava.com/api/v3/athlete/activities"
    
    headers = {
        'Authorization': f'Bearer {access_token}'
    }
    
    params = {
        'page': page,
        'per_page': per_page
    }
    
    if extra_params:
        params.update(extra_params)
    
    response = requests.get(url, headers=headers, params=params)
    response.raise_for_status()
    
    return response.json()

def upload_activities_to_s3(s3_client, bucket: str, activities: List[Dict]) -> Dict:
    """Upload activities to S3 with date-based partitioning"""
    
    upload_results = {
        'successful': 0,
        'failed': 0,
        'errors': []
    }
    
    for activity in activities:
        try:
            # Extract date for partitioning
            start_date = activity['start_date'][:10]  # YYYY-MM-DD format
            year, month, day = start_date.split('-')
            
            # Create S3 key with partitioning
            s3_key = f"strava/activities/year={year}/month={month}/day={day}/activity_{activity['id']}.json"
            
            # Upload to S3
            s3_client.put_object(
                Bucket=bucket,
                Key=s3_key,
                Body=json.dumps(activity, default=str),
                ContentType='application/json'
            )
            
            upload_results['successful'] += 1
            
        except Exception as e:
            upload_results['failed'] += 1
            upload_results['errors'].append({
                'activity_id': activity.get('id', 'unknown'),
                'error': str(e)
            })
    
    return upload_results

def get_last_sync_timestamp(s3_client, bucket: str) -> Optional[int]:
    """Get the last sync timestamp from S3"""
    
    try:
        response = s3_client.get_object(
            Bucket=bucket,
            Key='strava/metadata/last_sync.json'
        )
        
        sync_data = json.loads(response['Body'].read())
        return sync_data.get('last_sync_timestamp')
        
    except s3_client.exceptions.NoSuchKey:
        # First run, no previous sync
        return None
    except Exception as e:
        print(f"Error getting last sync timestamp: {e}")
        return None

def update_last_sync_timestamp(s3_client, bucket: str):
    """Update the last sync timestamp in S3"""
    
    sync_data = {
        'last_sync_timestamp': int(time.time()),
        'last_sync_date': datetime.utcnow().isoformat()
    }
    
    s3_client.put_object(
        Bucket=bucket,
        Key='strava/metadata/last_sync.json',
        Body=json.dumps(sync_data),
        ContentType='application/json'
    )

# Optional: Function to get detailed activity data
def get_activity_details(access_token: str, activity_id: str) -> Dict:
    """Get detailed data for a specific activity (includes streams, segments, etc.)"""
    
    url = f"https://www.strava.com/api/v3/activities/{activity_id}"
    
    headers = {
        'Authorization': f'Bearer {access_token}'
    }
    
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    
    return response.json()