# Strava API Data Analysis

> **Note:** This directory is an earlier, standalone exploration of Strava API access (AWS Lambda + Postgres). It predates and is independent of the main FormForward app one level up, which syncs Strava data into local SQLite via `strava_import.py` instead. Kept here for reference; not part of the live app's data path.

A Python application to connect to the Strava API and pull your activity data for analysis and visualization.

## Features

- 🔐 **Secure Authentication**: OAuth2 flow with automatic token refresh
- 📊 **Activity Analysis**: Comprehensive analysis of your Strava activities
- 📈 **Data Visualization**: Generate charts and graphs of your activity data
- 🏃 **Multiple Data Types**: Support for activities, streams, athlete stats, and more
- 📱 **Easy Setup**: Interactive setup script to get you started quickly

## Prerequisites

- Python 3.7 or higher
- A Strava account
- Strava API credentials (Client ID and Client Secret)

## Quick Start

### 1. Clone or Download the Project

```bash
git clone <repository-url>
cd strava_api
```

### 2. Run the Setup Script

The easiest way to get started is to run the interactive setup script:

```bash
python setup_strava.py
```

This script will:
- Guide you through creating a Strava application
- Help you get your API credentials
- Set up the authentication flow
- Install required dependencies
- Create your `.env` file

### 3. Run the Main Application

```bash
python src/main.py
```

## Manual Setup

If you prefer to set up manually:

### 1. Create a Strava Application

1. Go to [Strava API Settings](https://www.strava.com/settings/api)
2. Create a new application with these settings:
   - **Application Name**: Your app name (e.g., "My Strava Analysis")
   - **Category**: Analytics
   - **Website**: `http://localhost`
   - **Authorization Callback Domain**: `localhost`
3. Note your **Client ID** and **Client Secret**

### 2. Get Access Token

1. Visit this URL (replace `YOUR_CLIENT_ID` with your actual Client ID):
   ```
   https://www.strava.com/oauth/authorize?client_id=YOUR_CLIENT_ID&redirect_uri=http://localhost&response_type=code&scope=read,activity:read_all,profile:read_all
   ```

2. Authorize your application

3. You'll be redirected to a URL like:
   ```
   http://localhost?state=&code=YOUR_AUTHORIZATION_CODE
   ```

4. Copy the authorization code (everything after `code=`)

5. Exchange the authorization code for tokens:
   ```bash
   curl -X POST https://www.strava.com/oauth/token \
     -F client_id=YOUR_CLIENT_ID \
     -F client_secret=YOUR_CLIENT_SECRET \
     -F code=YOUR_AUTHORIZATION_CODE \
     -F grant_type=authorization_code
   ```

### 3. Create Environment File

Create a `.env` file in the project root:

```env
STRAVA_CLIENT_ID=your_client_id_here
STRAVA_CLIENT_SECRET=your_client_secret_here
STRAVA_REFRESH_TOKEN=your_refresh_token_here
STRAVA_ACCESS_TOKEN=your_access_token_here
```

### 4. Install Dependencies

```bash
pip install -r requirements.txt
```

## Usage

### Basic Usage

Run the main script to get an overview of your recent activities:

```bash
python src/main.py
```

This will:
- Display your athlete profile
- Show recent activities (last 30 days)
- Provide summary statistics
- Create visualizations
- Offer detailed analysis of specific activities

### Advanced Usage

You can also use the `StravaClient` class directly in your own scripts:

```python
from src.strava_client import StravaClient

# Initialize client
client = StravaClient()

# Get athlete profile
profile = client.get_athlete_profile()
print(f"Hello, {profile['firstname']}!")

# Get recent activities
activities = client.get_recent_activities(days=7)
print(f"You have {len(activities)} activities in the last week")

# Convert to DataFrame for analysis
df = client.activities_to_dataframe(activities)
print(f"Total distance: {df['distance'].sum():.1f} km")

# Get detailed activity information
activity_id = activities[0]['id']
detailed_activity = client.get_activity_by_id(activity_id)
streams = client.get_activity_streams(activity_id)
```

## API Features

### Available Methods

- `get_athlete_profile()` - Get current athlete's profile
- `get_activities()` - Get paginated list of activities
- `get_recent_activities(days)` - Get activities from last N days
- `get_activity_by_id(id)` - Get detailed activity information
- `get_activity_streams(id)` - Get GPS, heart rate, and other stream data
- `get_athlete_stats()` - Get athlete statistics
- `activities_to_dataframe()` - Convert activities to pandas DataFrame

### Data Types

The application handles various types of Strava data:

- **Activities**: Runs, rides, swims, etc.
- **Streams**: GPS coordinates, heart rate, power, cadence
- **Athlete Stats**: Lifetime and year-to-date statistics
- **Gear**: Bikes, shoes, and other equipment

## Output

The application generates:

1. **Console Output**: Summary statistics and analysis
2. **Visualizations**: Charts saved as `strava_analysis.png`
3. **DataFrames**: Structured data for further analysis

## File Structure

```
strava_api/
├── src/
│   ├── __init__.py
│   ├── main.py              # Main application
│   └── strava_client.py     # Strava API client
├── setup_strava.py          # Interactive setup script
├── requirements.txt          # Python dependencies
├── env_example.txt          # Environment variables template
├── README.md               # This file
└── .gitignore              # Git ignore rules
```

## Troubleshooting

### Common Issues

1. **"Missing required environment variables"**
   - Make sure your `.env` file exists and contains all required variables
   - Check that the file is in the project root directory

2. **"Failed to refresh access token"**
   - Your refresh token may have expired
   - Re-run the setup script to get new tokens

3. **"API request failed: 401"**
   - Your access token has expired
   - The client should automatically refresh, but you may need to restart

4. **Import errors for pandas/matplotlib**
   - Install dependencies: `pip install -r requirements.txt`
   - Or install manually: `pip install pandas matplotlib seaborn`

### Getting Help

- Check the [Strava API Documentation](https://developers.strava.com/)
- Review the error messages for specific API issues
- Ensure your Strava application has the correct permissions

## Security Notes

- Keep your `.env` file secure and never commit it to version control
- The `.env` file is already in `.gitignore` to prevent accidental commits
- Your refresh token provides long-term access - keep it secure
- Consider using environment variables in production deployments

## Contributing

Feel free to submit issues, feature requests, or pull requests to improve this project.

## License

This project is open source and available under the MIT License. 