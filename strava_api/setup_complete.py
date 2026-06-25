#!/usr/bin/env python3
"""
Complete Environment Setup Script

This script helps you set up ALL environment variables needed for the Strava API project:
- Strava API credentials
- PostgreSQL database credentials  
- AWS S3 bucket configuration
"""

import os
import webbrowser
from urllib.parse import urlencode

def print_setup_instructions():
    """Print detailed setup instructions."""
    print("🚴 Complete Environment Setup")
    print("=" * 50)
    print()
    print("This script will help you set up all environment variables needed for:")
    print("✅ Strava API access")
    print("✅ PostgreSQL database connection")
    print("✅ AWS S3 bucket for data storage")
    print()

def get_strava_credentials():
    """Get Strava API credentials."""
    print("\n🔐 Strava API Setup")
    print("=" * 30)
    print()
    print("1. Go to https://www.strava.com/settings/api")
    print("2. Create a new application with these settings:")
    print("   - Application Name: Your app name (e.g., 'My Strava Analysis')")
    print("   - Category: Analytics")
    print("   - Website: http://localhost")
    print("   - Authorization Callback Domain: localhost")
    print()
    
    client_id = input("Client ID: ").strip()
    client_secret = input("Client Secret: ").strip()
    
    if not client_id or not client_secret:
        print("❌ Client ID and Client Secret are required.")
        return None, None
    
    return client_id, client_secret

def get_strava_tokens(client_id, client_secret):
    """Get Strava access and refresh tokens."""
    print("\n🔄 Getting Strava Access Token")
    print("=" * 35)
    
    # Generate authorization URL
    auth_url = f"https://www.strava.com/oauth/authorize?client_id={client_id}&redirect_uri=http://localhost&response_type=code&scope=read,activity:read_all,profile:read_all"
    
    print("1. Click the link below to authorize your application:")
    print(f"   {auth_url}")
    print()
    
    # Try to open the URL in browser
    try:
        webbrowser.open(auth_url)
        print("✅ Opened authorization URL in your browser")
    except:
        print("⚠️  Could not open browser automatically. Please copy and paste the URL above.")
    
    print()
    print("2. After authorizing, you'll be redirected to a URL like:")
    print("   http://localhost?state=&code=YOUR_AUTHORIZATION_CODE")
    print()
    print("3. Copy the 'code' parameter from the URL (everything after 'code=')")
    print()
    
    authorization_code = input("Authorization Code: ").strip()
    
    if not authorization_code:
        print("❌ Authorization code is required.")
        return None, None
    
    # Exchange authorization code for tokens
    print("\n🔄 Exchanging authorization code for tokens...")
    
    import requests
    
    token_url = "https://www.strava.com/oauth/token"
    data = {
        'client_id': client_id,
        'client_secret': client_secret,
        'code': authorization_code,
        'grant_type': 'authorization_code'
    }
    
    try:
        response = requests.post(token_url, data=data)
        if response.status_code == 200:
            token_data = response.json()
            
            access_token = token_data.get('access_token')
            refresh_token = token_data.get('refresh_token')
            
            if access_token and refresh_token:
                print("✅ Successfully obtained Strava tokens!")
                return access_token, refresh_token
            else:
                print("❌ Failed to get tokens from response.")
                return None, None
        else:
            print(f"❌ Failed to exchange authorization code: {response.status_code}")
            print(f"Response: {response.text}")
            return None, None
            
    except Exception as e:
        print(f"❌ Error exchanging authorization code: {e}")
        return None, None

def get_postgres_credentials():
    """Get PostgreSQL database credentials."""
    print("\n🗄️  PostgreSQL Database Setup")
    print("=" * 35)
    print()
    print("Enter your PostgreSQL database credentials:")
    print("(For AWS RDS, this would be your RDS endpoint)")
    print()
    
    pg_host = input("Database Host (e.g., your-db.amazonaws.com): ").strip()
    pg_port = input("Database Port (default: 5432): ").strip() or "5432"
    pg_database = input("Database Name: ").strip()
    pg_user = input("Database Username: ").strip()
    pg_password = input("Database Password: ").strip()
    
    if not all([pg_host, pg_database, pg_user, pg_password]):
        print("❌ All PostgreSQL credentials are required.")
        return None, None, None, None, None
    
    return pg_host, pg_port, pg_database, pg_user, pg_password

def get_s3_credentials():
    """Get AWS S3 bucket information."""
    print("\n☁️  AWS S3 Setup")
    print("=" * 20)
    print()
    print("Enter your AWS S3 bucket information:")
    print("(You'll need to create an S3 bucket first)")
    print()
    
    s3_bucket = input("S3 Bucket Name: ").strip()
    
    if not s3_bucket:
        print("❌ S3 bucket name is required.")
        return None
    
    return s3_bucket

def create_env_file(stravavars, pgvars, s3_bucket):
    """Create the .env file with all credentials."""
    client_id, client_secret, access_token, refresh_token = stravavars
    pg_host, pg_port, pg_database, pg_user, pg_password = pgvars
    
    env_content = f"""# Strava API Configuration
STRAVA_CLIENT_ID={client_id}
STRAVA_CLIENT_SECRET={client_secret}
STRAVA_REFRESH_TOKEN={refresh_token}
STRAVA_ACCESS_TOKEN={access_token}

# PostgreSQL Database Configuration
PG_HOST={pg_host}
PG_PORT={pg_port}
PG_DATABASE={pg_database}
PG_USER={pg_user}
PG_PASSWORD={pg_password}

# AWS S3 Configuration
S3_BUCKET={s3_bucket}
"""
    
    try:
        with open('.env', 'w') as f:
            f.write(env_content)
        print("✅ Created .env file with all your credentials")
        return True
    except Exception as e:
        print(f"❌ Error creating .env file: {e}")
        return False

def install_dependencies():
    """Install required Python packages."""
    print("\n📦 Installing dependencies...")
    
    try:
        import subprocess
        import sys
        
        # Install packages
        packages = [
            'requests==2.31.0',
            'python-dotenv==1.0.0',
            'pandas>=2.2.0',
            'matplotlib>=3.8.0',
            'seaborn>=0.13.0',
            'psycopg2-binary>=2.9.0',
            'boto3>=1.34.0'
        ]
        
        for package in packages:
            print(f"Installing {package}...")
            subprocess.check_call([sys.executable, '-m', 'pip', 'install', package])
        
        print("✅ All dependencies installed successfully!")
        return True
        
    except Exception as e:
        print(f"❌ Error installing dependencies: {e}")
        print("Please install the packages manually:")
        print("pip install -r requirements.txt")
        return False

def main():
    """Main setup function."""
    print_setup_instructions()
    
    # Get Strava credentials
    client_id, client_secret = get_strava_credentials()
    if not client_id or not client_secret:
        return
    
    # Get Strava tokens
    access_token, refresh_token = get_strava_tokens(client_id, client_secret)
    if not access_token or not refresh_token:
        return
    
    # Get PostgreSQL credentials
    pg_host, pg_port, pg_database, pg_user, pg_password = get_postgres_credentials()
    if not pg_host:
        return
    
    # Get S3 bucket
    s3_bucket = get_s3_credentials()
    if not s3_bucket:
        return
    
    # Create .env file
    stravavars = (client_id, client_secret, access_token, refresh_token)
    pgvars = (pg_host, pg_port, pg_database, pg_user, pg_password)
    
    if create_env_file(stravavars, pgvars, s3_bucket):
        print("\n🎉 Setup complete!")
        print()
        print("You can now run the scripts:")
        print("  python src/main.py          # Analysis and visualization")
        print("  python strava_sync.py       # Sync to S3 and PostgreSQL")
        print("  python strava_sync.py --full-sync  # Full historical sync")
        print()
        print("Your credentials are saved in the .env file.")
        print("⚠️  Keep this file secure and don't share it publicly.")
        
        # Install dependencies
        install_dependencies()
    else:
        print("\n❌ Setup failed. Please try again.")

if __name__ == "__main__":
    main() 