#!/usr/bin/env python3
"""
Strava API Data Retrieval and Analysis

This script demonstrates how to connect to the Strava API and pull your activity data.
Make sure to set up your environment variables before running this script.
"""

import os
import json
from datetime import datetime, timedelta
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import execute_values

# Import our Strava client
from strava_client import StravaClient
from postgres_utils import (
    get_pg_connection, 
    create_activities_table_if_not_exists, 
    insert_activities_to_postgres,
    create_sync_metadata_table_if_not_exists,
    get_last_sync_timestamp_from_postgres,
    update_sync_timestamp_in_postgres
)

# Load environment variables
load_dotenv()

def setup_environment():
    """Check if required environment variables are set."""
    required_vars = ['STRAVA_CLIENT_ID', 'STRAVA_CLIENT_SECRET', 'STRAVA_REFRESH_TOKEN']
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    
    if missing_vars:
        print("❌ Missing required environment variables:")
        for var in missing_vars:
            print(f"   - {var}")
        print("\nPlease create a .env file with your Strava API credentials.")
        print("You can copy from env_example.txt and fill in your values.")
        return False
    
    print("✅ Environment variables are properly configured.")
    return True

def get_strava_data():
    """Initialize Strava client and get basic data."""
    try:
        # Initialize the Strava client
        client = StravaClient()
        
        # Get athlete profile
        print("\n📊 Getting athlete profile...")
        profile = client.get_athlete_profile()
        print(f"   Name: {profile.get('firstname', '')} {profile.get('lastname', '')}")
        print(f"   Location: {profile.get('city', '')}, {profile.get('state', '')}, {profile.get('country', '')}")
        print(f"   Followers: {profile.get('follower_count', 0)}")
        print(f"   Following: {profile.get('friend_count', 0)}")
        
        # Get recent activities
        print("\n🏃 Getting recent activities...")
        activities = client.get_recent_activities(days=30)
        print(f"   Found {len(activities)} activities in the last 30 days")
        
        if activities:
            # Convert to DataFrame for analysis
            df = client.activities_to_dataframe(activities)
            
            # Display summary statistics
            print("\n📈 Activity Summary (Last 30 Days):")
            print(f"   Total Distance: {df['distance'].sum():.1f} km")
            print(f"   Total Moving Time: {df['moving_time'].sum():.1f} minutes")
            print(f"   Total Elevation Gain: {df['total_elevation_gain'].sum():.0f} m")
            print(f"   Average Speed: {df['average_speed'].mean():.1f} km/h")
            
            # Activity type breakdown
            activity_types = df['type'].value_counts()
            print("\n🏃‍♂️ Activity Types:")
            for activity_type, count in activity_types.items():
                print(f"   {activity_type}: {count} activities")
            
            return client, df, activities
        else:
            print("   No activities found in the last 30 days.")
            return client, pd.DataFrame(), []
            
    except Exception as e:
        print(f"❌ Error connecting to Strava API: {e}")
        return None, pd.DataFrame(), []

def analyze_activities(df):
    """Perform basic analysis on activities data."""
    if df.empty:
        print("No data to analyze.")
        return
    
    print("\n📊 Activity Analysis:")
    
    # Time-based analysis
    df['date'] = pd.to_datetime(df['start_date_local']).dt.date
    daily_stats = df.groupby('date').agg({
        'distance': 'sum',
        'moving_time': 'sum',
        'total_elevation_gain': 'sum',
        'average_speed': 'mean'
    }).reset_index()
    
    print(f"   Most active day: {daily_stats.loc[daily_stats['distance'].idxmax(), 'date']}")
    print(f"   Longest activity: {df.loc[df['moving_time'].idxmax(), 'name']} ({df['moving_time'].max():.1f} minutes)")
    print(f"   Fastest average speed: {df['average_speed'].max():.1f} km/h")
    
    # Gear analysis
    if 'gear_name' in df.columns and df['gear_name'].notna().any():
        gear_stats = df.groupby('gear_name').agg({
            'distance': 'sum',
            'moving_time': 'sum',
            'average_speed': 'mean'
        }).sort_values('distance', ascending=False)
        
        print("\n🚴 Gear Usage:")
        for gear, stats in gear_stats.head(3).iterrows():
            print(f"   {gear}: {stats['distance']:.1f} km, {stats['moving_time']:.1f} min")

def create_visualizations(df):
    """Create basic visualizations of the activity data."""
    if df.empty:
        print("No data to visualize.")
        return
    
    # Set up the plotting style
    plt.style.use('seaborn-v0_8')
    fig, axes = plt.subplots(2, 2, figsize=(15, 10))
    fig.suptitle('Strava Activity Analysis', fontsize=16, fontweight='bold')
    
    # 1. Distance by activity type
    activity_distance = df.groupby('type')['distance'].sum().sort_values(ascending=True)
    axes[0, 0].barh(activity_distance.index, activity_distance.values)
    axes[0, 0].set_title('Total Distance by Activity Type')
    axes[0, 0].set_xlabel('Distance (km)')
    
    # 2. Daily distance over time
    df['date'] = pd.to_datetime(df['start_date_local']).dt.date
    daily_distance = df.groupby('date')['distance'].sum()
    axes[0, 1].plot(daily_distance.index, daily_distance.values, marker='o')
    axes[0, 1].set_title('Daily Distance')
    axes[0, 1].set_xlabel('Date')
    axes[0, 1].set_ylabel('Distance (km)')
    axes[0, 1].tick_params(axis='x', rotation=45)
    
    # 3. Speed vs Distance scatter plot
    axes[1, 0].scatter(df['distance'], df['average_speed'], alpha=0.6)
    axes[1, 0].set_title('Speed vs Distance')
    axes[1, 0].set_xlabel('Distance (km)')
    axes[1, 0].set_ylabel('Average Speed (km/h)')
    
    # 4. Activity duration distribution
    axes[1, 1].hist(df['moving_time'], bins=20, alpha=0.7, edgecolor='black')
    axes[1, 1].set_title('Activity Duration Distribution')
    axes[1, 1].set_xlabel('Moving Time (minutes)')
    axes[1, 1].set_ylabel('Frequency')
    
    plt.tight_layout()
    plt.savefig('strava_analysis.png', dpi=300, bbox_inches='tight')
    print("\n📊 Visualizations saved as 'strava_analysis.png'")

def get_detailed_activity(client, activity_id):
    """Get detailed information about a specific activity."""
    try:
        print(f"\n🔍 Getting detailed information for activity {activity_id}...")
        
        # Get basic activity info
        activity = client.get_activity_by_id(activity_id)
        print(f"   Activity: {activity.get('name', 'Unknown')}")
        print(f"   Type: {activity.get('type', 'Unknown')}")
        print(f"   Distance: {activity.get('distance', 0) / 1000:.2f} km")
        print(f"   Duration: {activity.get('moving_time', 0) / 60:.1f} minutes")
        print(f"   Elevation Gain: {activity.get('total_elevation_gain', 0)} m")
        
        # Get activity streams (GPS data, heart rate, etc.)
        streams = client.get_activity_streams(activity_id)
        if streams:
            print(f"   Available streams: {list(streams.keys())}")
            
            # Display heart rate data if available
            if 'heartrate' in streams:
                hr_data = streams['heartrate']['data']
                if hr_data:
                    avg_hr = sum(hr_data) / len(hr_data)
                    max_hr = max(hr_data)
                    print(f"   Average Heart Rate: {avg_hr:.0f} bpm")
                    print(f"   Max Heart Rate: {max_hr} bpm")
        
        return activity, streams
        
    except Exception as e:
        print(f"❌ Error getting activity details: {e}")
        return None, None

def main():
    """Main function to run the Strava data analysis."""
    print("🚴 Strava API Data Analysis")
    print("=" * 40)
    
    # Check environment setup
    if not setup_environment():
        return
    
    # Get Strava data
    client, df, activities = get_strava_data()
    
    if client is None:
        return
    
    # Analyze activities
    analyze_activities(df)
    
    # Create visualizations
    create_visualizations(df)
    
    # Insert activities into PostgreSQL
    try:
        conn = get_pg_connection()
        create_activities_table_if_not_exists(conn)
        create_sync_metadata_table_if_not_exists(conn)
        
        # Get last sync timestamp from PostgreSQL
        last_sync = get_last_sync_timestamp_from_postgres(conn)
        if last_sync:
            print(f"📅 Last sync: {datetime.fromtimestamp(last_sync)}")
        
        # Insert activities and get count
        activities_processed = insert_activities_to_postgres(conn, df)
        
        # Update sync timestamp in PostgreSQL
        update_sync_timestamp_in_postgres(conn, activities_processed=activities_processed)
        
        conn.close()
    except Exception as e:
        print(f"❌ Error writing to PostgreSQL: {e}")
        # Update sync timestamp with error status
        try:
            conn = get_pg_connection()
            update_sync_timestamp_in_postgres(conn, sync_status='error', error_message=str(e))
            conn.close()
        except:
            pass
    
    # Example: Get detailed info for the most recent activity
    if activities:
        latest_activity = activities[0]
        activity_id = latest_activity['id']
        activity_name = latest_activity['name']
        
        print(f"\n🔍 Latest Activity: {activity_name} (ID: {activity_id})")
        
        # Ask user if they want detailed info
        response = input("Would you like detailed information about this activity? (y/n): ")
        if response.lower() in ['y', 'yes']:
            get_detailed_activity(client, activity_id)
    
    print("\n✅ Analysis complete!")

if __name__ == "__main__":
    main()
