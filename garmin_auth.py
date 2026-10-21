#!/usr/bin/env python3
"""
One-time Garmin Connect authentication.
Run this once from your terminal — it saves a token so you never need to enter
your password again.

    python garmin_auth.py
"""
import getpass
import os
import garth

TOKEN_DIR = os.path.expanduser("~/.garth")

email    = input("Garmin email: ").strip()
password = getpass.getpass("Garmin password: ")

print("Logging in…")
try:
    garth.login(email, password)
except Exception as e:
    if "MFA" in str(e) or "mfa" in str(e) or "factor" in str(e).lower():
        code = input("2FA code (check your email): ").strip()
        garth.resume_mfa(code)
    else:
        raise

garth.save(TOKEN_DIR)
print(f"✓ Tokens saved to {TOKEN_DIR}")
print("You can now run push_garmin_workout.py")
