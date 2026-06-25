#!/usr/bin/env python3
"""
Local Strava Sync Script

This script does the same thing as the Lambda function but runs locally.
It syncs Strava activities to both S3 and PostgreSQL.
"""

import json
import boto3
import requests
import time
from datetime import datetime, timedelta
import os
from typing import Dict, List, Optional
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv
import argparse
from src.postgres_utils import (
    get_pg_connection,
    create_activities_table_if_not_exists,
    insert_activities_to_postgres,
    create_sync_metadata_table_if_not_exists,
    get_last_sync_timestamp_from_postgres,
    update_sync_timestamp_in_postgres
)
import pandas as pd
import traceback

# Load environment variables
load_dotenv()

def lambda_handler(full_sync=False):
    """
    Local function to sync Strava activities to S3 and PostgreSQL
    """
    
    # Environment variables - set these in your .env file
    client_id = os.environ.get('STRAVA_CLIENT_ID')
    client_secret = os.environ.get('STRAVA_CLIENT_SECRET')
    refresh_token = os.environ.get('STRAVA_REFRESH_TOKEN')
    s3_bucket = os.environ.get('S3_BUCKET')
    
    # Check if required environment variables are set
    if not all([client_id, client_secret, refresh_token]):
        print("❌ Missing required Strava environment variables. Please check your .env file.")
        return
    
    if not s3_bucket:
        print("❌ Missing S3_BUCKET environment variable. Please check your .env file.")
        return
    
    # Initialize S3 client
    s3 = boto3.client('s3')
    
    try:
        # Get fresh access token
        access_token = refresh_strava_token(client_id, client_secret, refresh_token)
        
        if full_sync:
            print("🔄 Starting full historical sync from the beginning...")
            activities = get_all_activities_historical(access_token)
            print(f"✅ Found {len(activities)} total activities")
        else:
            # Get last sync timestamp from PostgreSQL
            conn = get_pg_connection()
            create_sync_metadata_table_if_not_exists(conn)
            last_sync = get_last_sync_timestamp_from_postgres(conn)
            conn.close()
            
            if last_sync:
                print(f"📅 Last sync: {datetime.fromtimestamp(last_sync)}")
                # Get activities since last sync
                activities = get_recent_activities(access_token, since_timestamp=last_sync)
                print(f"🔄 Getting activities since last sync...")
            else:
                # First run - get activities from last 30 days
                activities = get_recent_activities(access_token, days=30)
                print(f"🔄 Getting recent activities (first run)...")
            
            print(f"✅ Found {len(activities)} activities")
        
        if activities:
            # Upload to S3
            print("📤 Uploading activities to S3...")
            #upload_results = upload_activities_to_s3(s3, s3_bucket, activities)
            #print(f"✅ S3 upload complete: {upload_results['successful']} successful, {upload_results['failed']} failed")
            
            # Upload to PostgreSQL
            print("📤 Uploading activities to PostgreSQL...")
            try:
                conn = get_pg_connection()
                create_activities_table_if_not_exists(conn)
                activities_df = pd.DataFrame(activities)
                activities_processed = insert_activities_to_postgres(conn, activities_df)
                conn.close()
                print("✅ PostgreSQL upload complete")
                
                # Update sync timestamp in PostgreSQL
                conn = get_pg_connection()
                update_sync_timestamp_in_postgres(conn, activities_processed=activities_processed)
                conn.close()
                
            except Exception as db_e:
                print(f"❌ Error writing to PostgreSQL: {db_e}")
                # Update sync timestamp with error status
                try:
                    conn = get_pg_connection()
                    update_sync_timestamp_in_postgres(conn, sync_status='error', error_message=str(db_e))
                    conn.close()
                except:
                    pass
            
            print(f"\n🎉 Sync completed successfully!")
            print(f"   Activities processed: {len(activities)}")
            #print(f"   S3 uploads: {upload_results['successful']} successful, {upload_results['failed']} failed")
            
        else:
            print("ℹ️  No activities found to sync")
            # Update sync timestamp even if no activities
            conn = get_pg_connection()
            update_sync_timestamp_in_postgres(conn, activities_processed=0)
            conn.close()
        
    except Exception as e:
        print(f"❌ Error during sync: {str(e)}")
        traceback.print_exc()
        # Update sync timestamp with error status
        try:
            conn = get_pg_connection()
            update_sync_timestamp_in_postgres(conn, sync_status='error', error_message=str(e))
            conn.close()
        except Exception as db_ex:
            print(f"❌ Error updating sync status in PostgreSQL: {db_ex}")
            traceback.print_exc()
        raise

def get_all_activities_historical(access_token: str) -> List[Dict]:
    """Get ALL activities from Strava API, working backwards from most recent"""
    
    print("🔄 Fetching all historical activities (this may take a while)...")
    
    activities = []
    page = 1
    per_page = 200  # Max allowed by Strava
    total_processed = 0
    
    while True:
        print(f"   Processing page {page}...")
        
        batch = get_activities_page(access_token, page, per_page)
        
        if not batch:
            print("   No more activities found")
            break
            
        activities.extend(batch)
        total_processed += len(batch)
        
        print(f"   Found {len(batch)} activities on this page (total: {total_processed})")
        
        # If we got less than per_page, we're done
        if len(batch) < per_page:
            print("   Reached the end of activities")
            break
            
        page += 1
        
        # Rate limiting: 100 requests per 15 minutes
        time.sleep(0.1)  # Small delay to be respectful
        
        # Optional: Add a progress indicator every 10 pages
        if page % 10 == 0:
            print(f"   Progress: {total_processed} activities processed so far...")
    
    print(f"✅ Historical sync complete! Total activities: {len(activities)}")
    return activities

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

def get_recent_activities(access_token: str, days: int = 30, since_timestamp: Optional[int] = None) -> List[Dict]:
    """Get activities from the last N days or since a specific timestamp"""
    
    if since_timestamp:
        # Use the provided timestamp
        since_timestamp_param = since_timestamp
    else:
        # Calculate timestamp for N days ago
        since_date = datetime.now() - timedelta(days=days)
        since_timestamp_param = int(since_date.timestamp())
    
    activities = []
    page = 1
    per_page = 200
    
    while True:
        params = {
            'page': page,
            'per_page': per_page,
            'after': since_timestamp_param
        }
        
        batch = get_activities_page(access_token, page, per_page, params)
        
        if not batch:
            break
            
        activities.extend(batch)
        
        if len(batch) < per_page:
            break
            
        page += 1
        time.sleep(0.1)  # Rate limiting
    
    return activities

def get_activities_page(access_token: str, page: int, per_page: int, extra_params: Optional[Dict] = None) -> List[Dict]:
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
            print(f"✅ Activity succesfully uploaded to S3: {activity['id']}")
            
        except Exception as e:
            print(f"❌ Error uploading to S3: {str(e)}")
            upload_results['failed'] += 1
            upload_results['errors'].append({
                'activity_id': activity.get('id', 'unknown'),
                'error': str(e)
            })
    
    return upload_results

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Sync Strava activities to S3 and PostgreSQL')
    parser.add_argument('--full-sync', action='store_true', 
                       help='Perform a full historical sync from the beginning of your Strava activities')
    
    args = parser.parse_args()
    
    print("🚴 Strava Sync Script")
    print("=" * 40)
    
    if args.full_sync:
        print("📋 Mode: Full Historical Sync")
        print("   This will fetch ALL your Strava activities from the beginning")
        print("   This may take a while depending on how many activities you have")
        print()
        
        # Ask for confirmation
        response = input("Are you sure you want to perform a full historical sync? (y/N): ")
        if response.lower() not in ['y', 'yes']:
            print("Sync cancelled.")
            exit(0)
    
    lambda_handler(full_sync=args.full_sync) 