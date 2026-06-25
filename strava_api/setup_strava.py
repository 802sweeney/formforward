#!/usr/bin/env python3
"""
Strava API Setup Script

This script helps you set up your Strava API credentials and get started with the API.
"""

import os
import webbrowser
from urllib.parse import urlencode

def print_setup_instructions():
    """Print detailed setup instructions."""
    print("🚴 Strava API Setup Instructions")
    print("=" * 50)
    print()
    print("To use the Strava API, you need to create a Strava application and get your credentials.")
    print()
    print("1. Go to https://www.strava.com/settings/api")
    print("2. Create a new application with the following settings:")
    print("   - Application Name: Your app name (e.g., 'My Strava Analysis')")
    print("   - Category: Analytics")
    print("   - Website: http://localhost")
    print("   - Authorization Callback Domain: localhost")
    print()
    print("3. After creating the application, you'll get:")
    print("   - Client ID")
    print("   - Client Secret")
    print()
    print("4. Use the authorization URL below to get your access token:")
    print()

def generate_auth_url(client_id):
    """Generate the Strava authorization URL."""
    base_url = "https://www.strava.com/oauth/authorize"
    params = {
        'client_id': client_id,
        'redirect_uri': 'http://localhost',
        'response_type': 'code',
        'scope': 'read,activity:read_all,profile:read_all'
    }
    return f"{base_url}?{urlencode(params)}"

def get_credentials():
    """Interactive credential collection."""
    print("Enter your Strava API credentials:")
    print()
    
    client_id = input("Client ID: ").strip()
    client_secret = input("Client Secret: ").strip()
    
    if not client_id or not client_secret:
        print("❌ Client ID and Client Secret are required.")
        return None, None
    
    return client_id, client_secret

def get_access_token(client_id, client_secret):
    """Guide user through getting access token."""
    print("\n🔐 Getting Access Token")
    print("=" * 30)
    print()
    
    # Generate authorization URL
    auth_url = generate_auth_url(client_id)
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
                print("✅ Successfully obtained tokens!")
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

def create_env_file(client_id, client_secret, access_token, refresh_token):
    """Create the .env file with the credentials."""
    env_content = f"""# Strava API Configuration
STRAVA_CLIENT_ID={client_id}
STRAVA_CLIENT_SECRET={client_secret}
STRAVA_REFRESH_TOKEN={refresh_token}
STRAVA_ACCESS_TOKEN={access_token}
"""
    
    try:
        with open('.env', 'w') as f:
            f.write(env_content)
        print("✅ Created .env file with your credentials")
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
            'pandas==2.1.4',
            'matplotlib==3.8.2',
            'seaborn==0.13.0'
        ]
        
        for package in packages:
            print(f"Installing {package}...")
            subprocess.check_call([sys.executable, '-m', 'pip', 'install', package])
        
        print("✅ All dependencies installed successfully!")
        return True
        
    except Exception as e:
        print(f"❌ Error installing dependencies: {e}")
        print("Please install the packages manually:")
        print("pip install requests python-dotenv pandas matplotlib seaborn")
        return False

def main():
    """Main setup function."""
    print_setup_instructions()
    
    # Get credentials
    client_id, client_secret = get_credentials()
    if not client_id or not client_secret:
        return
    
    # Get access token
    access_token, refresh_token = get_access_token(client_id, client_secret)
    if not access_token or not refresh_token:
        return
    
    # Create .env file
    if create_env_file(client_id, client_secret, access_token, refresh_token):
        print("\n🎉 Setup complete!")
        print()
        print("You can now run the main script:")
        print("python src/main.py")
        print()
        print("Your credentials are saved in the .env file.")
        print("⚠️  Keep this file secure and don't share it publicly.")
        
        # Install dependencies
        install_dependencies()
    else:
        print("\n❌ Setup failed. Please try again.")

if __name__ == "__main__":
    main() 