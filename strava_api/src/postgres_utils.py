import os
import psycopg2
from psycopg2.extras import execute_values
from datetime import datetime
import numpy as np
import pandas as pd

def get_pg_connection():
    """Establish a connection to the PostgreSQL database using environment variables."""
    conn = psycopg2.connect(
        host=os.getenv('PG_HOST'),
        port=os.getenv('PG_PORT', 5432),
        dbname=os.getenv('PG_DATABASE'),
        user=os.getenv('PG_USER'),
        password=os.getenv('PG_PASSWORD')
    )
    return conn

def check_database_permissions(conn):
    """Check if the user has necessary permissions and provide helpful error messages."""
    try:
        with conn.cursor() as cur:
            # Check if we can create tables
            cur.execute("""
                SELECT has_table_privilege(current_user, 'information_schema.tables', 'SELECT')
            """)
            can_select = cur.fetchone()[0]
            
            # Check if we can create tables in public schema
            cur.execute("""
                SELECT has_schema_privilege(current_user, 'public', 'CREATE')
            """)
            can_create = cur.fetchone()[0]
            
            if not can_create:
                print("⚠️  Warning: Your database user doesn't have CREATE permission on the public schema.")
                print("   This is common with AWS RDS or managed PostgreSQL instances.")
                print("   You may need to:")
                print("   1. Ask your database administrator to grant CREATE permission")
                print("   2. Use a different schema that you have access to")
                print("   3. Create the tables manually with proper permissions")
                return False
            
            return True
            
    except Exception as e:
        print(f"⚠️  Could not check database permissions: {e}")
        return True  # Assume it's okay if we can't check

def create_activities_table_if_not_exists(conn):
    """Create the activities table if it doesn't exist."""
    try:
        create_table_sql = '''
        CREATE TABLE IF NOT EXISTS strava_activities (
            id BIGINT PRIMARY KEY,
            name TEXT,
            type TEXT,
            distance DOUBLE PRECISION,
            moving_time DOUBLE PRECISION,
            elapsed_time DOUBLE PRECISION,
            total_elevation_gain DOUBLE PRECISION,
            average_speed DOUBLE PRECISION,
            max_speed DOUBLE PRECISION,
            average_heartrate DOUBLE PRECISION,
            max_heartrate DOUBLE PRECISION,
            average_watts DOUBLE PRECISION,
            max_watts DOUBLE PRECISION,
            start_date TIMESTAMP,
            start_date_local TIMESTAMP,
            location_country TEXT,
            location_state TEXT,
            location_city TEXT,
            kudos_count INTEGER,
            comment_count INTEGER,
            athlete_count INTEGER,
            trainer BOOLEAN,
            commute BOOLEAN,
            manual BOOLEAN,
            private BOOLEAN,
            visibility TEXT,
            flagged BOOLEAN,
            workout_type INTEGER,
            upload_id BIGINT,
            external_id TEXT,
            average_cadence DOUBLE PRECISION,
            has_kudoed BOOLEAN,
            description TEXT,
            calories DOUBLE PRECISION,
            segment_efforts INTEGER,
            splits_metric INTEGER,
            laps INTEGER,
            gear_id TEXT,
            gear_name TEXT,
            gear_distance DOUBLE PRECISION,
            gear_primary BOOLEAN,
            gear_brand_name TEXT,
            gear_model_name TEXT,
            gear_frame_type INTEGER,
            gear_description TEXT
        );
        '''
        with conn.cursor() as cur:
            cur.execute(create_table_sql)
            conn.commit()
            print("✅ Created strava_activities table successfully")
            
    except psycopg2.errors.InsufficientPrivilege as e:
        print(f"❌ Permission denied: {e}")
        print("\n🔧 Solutions:")
        print("1. Grant CREATE permission to your user:")
        print("   GRANT CREATE ON SCHEMA public TO your_username;")
        print("2. Or create the table manually with proper permissions")
        print("3. Or use a different schema that you have access to")
        raise
    except Exception as e:
        print(f"❌ Error creating table: {e}")
        raise

def create_sync_metadata_table_if_not_exists(conn):
    """Create the sync metadata table if it doesn't exist."""
    try:
        create_table_sql = '''
        CREATE TABLE IF NOT EXISTS sync_metadata (
            id SERIAL PRIMARY KEY,
            sync_type VARCHAR(50) NOT NULL,
            last_sync_timestamp BIGINT,
            last_sync_date TIMESTAMP,
            activities_processed INTEGER DEFAULT 0,
            sync_status VARCHAR(20) DEFAULT 'success',
            error_message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        '''
        with conn.cursor() as cur:
            cur.execute(create_table_sql)
            conn.commit()
            print("✅ Created sync_metadata table successfully")
            
    except psycopg2.errors.InsufficientPrivilege as e:
        print(f"❌ Permission denied: {e}")
        print("\n🔧 Solutions:")
        print("1. Grant CREATE permission to your user:")
        print("   GRANT CREATE ON SCHEMA public TO your_username;")
        print("2. Or create the table manually with proper permissions")
        print("3. Or use a different schema that you have access to")
        raise
    except Exception as e:
        print(f"❌ Error creating table: {e}")
        raise

def get_last_sync_timestamp_from_postgres(conn, sync_type='strava_activities'):
    """Get the last sync timestamp from PostgreSQL"""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT last_sync_timestamp 
                FROM sync_metadata 
                WHERE sync_type = %s 
                ORDER BY updated_at DESC 
                LIMIT 1
            """, (sync_type,))
            result = cur.fetchone()
            return result[0] if result else None
    except psycopg2.errors.UndefinedTable:
        print("ℹ️  sync_metadata table doesn't exist yet (first run)")
        return None
    except Exception as e:
        print(f"Error getting last sync timestamp from PostgreSQL: {e}")
        return None

def update_sync_timestamp_in_postgres(conn, sync_type='strava_activities', activities_processed=0, sync_status='success', error_message=None):
    """Update the sync timestamp in PostgreSQL"""
    try:
        current_timestamp = int(datetime.now().timestamp())
        current_date = datetime.now()
        
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO sync_metadata 
                (sync_type, last_sync_timestamp, last_sync_date, activities_processed, sync_status, error_message)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (sync_type, current_timestamp, current_date, activities_processed, sync_status, error_message))
            conn.commit()
            print(f"✅ Updated sync timestamp in PostgreSQL: {current_date}")
    except psycopg2.errors.UndefinedTable:
        print("ℹ️  sync_metadata table doesn't exist, skipping timestamp update")
    except Exception as e:
        print(f"Error updating sync timestamp in PostgreSQL: {e}")

def insert_activities_to_postgres(conn, df):
    """Insert activities DataFrame into PostgreSQL."""
    if df.empty:
        print("No activities to insert into PostgreSQL.")
        return 0
    
    # Fill NaN with 0 for numeric columns only
    import numpy as np
    numeric_cols = df.select_dtypes(include=[np.number]).columns
    df[numeric_cols] = df[numeric_cols].fillna(0)
    
    cols = [
        'id', 'name', 'type', 'distance', 'moving_time', 'elapsed_time', 'total_elevation_gain',
        'average_speed', 'max_speed', 'average_heartrate', 'max_heartrate', 'average_watts', 'max_watts',
        'start_date', 'start_date_local', 'location_country', 'location_state', 'location_city',
        'kudos_count', 'comment_count', 'athlete_count', 'trainer', 'commute', 'manual', 'private',
        'visibility', 'flagged', 'workout_type', 'upload_id', 'external_id', 'average_cadence',
        'has_kudoed', 'description', 'calories', 'segment_efforts', 'splits_metric', 'laps',
        'gear_id', 'gear_name', 'gear_distance', 'gear_primary', 'gear_brand_name', 'gear_model_name',
        'gear_frame_type', 'gear_description'
    ]
    values = [tuple(row[col] if col in row else None for col in cols) for row in df.to_dict('records')]
    insert_sql = f"""
        INSERT INTO strava_activities ({', '.join(cols)})
        VALUES %s
        ON CONFLICT (id) DO UPDATE SET
        {', '.join([f'{col}=EXCLUDED.{col}' for col in cols if col != 'id'])}
    """
    try:
        with conn.cursor() as cur:
            execute_values(cur, insert_sql, values)
            conn.commit()
        print(f"✅ Inserted/updated {len(values)} activities into PostgreSQL.")
        return len(values)
    except psycopg2.errors.UndefinedTable:
        print("❌ strava_activities table doesn't exist. Please create it first.")
        print(f"Failed SQL: {insert_sql}")
        raise
    except Exception as e:
        print(f"❌ Error inserting activities: {e}")
        print(f"Failed SQL: {insert_sql}")
        if values:
            print(f"First row of values: {values[0]}")
        raise 