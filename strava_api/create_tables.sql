-- Strava API Database Tables
-- Run these commands in your PostgreSQL database to create the required tables

-- Create the activities table
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

-- Create the sync metadata table
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

-- Grant permissions to your user (replace 'your_username' with your actual username)
-- GRANT ALL PRIVILEGES ON TABLE strava_activities TO your_username;
-- GRANT ALL PRIVILEGES ON TABLE sync_metadata TO your_username;
-- GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO your_username;

-- Verify tables were created
SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name IN ('strava_activities', 'sync_metadata'); 