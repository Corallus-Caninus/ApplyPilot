#!/usr/bin/env python3
"""
Credentials manager — stores and retrieves site-specific login credentials
for the apply pipeline. Each entry tracks (site_domain, email, password).

Usage:
  python3 credentials_manager.py save <site> <email> <password>
  python3 credentials_manager.py get <site>
  python3 credentials_manager.py list
  python3 credentials_manager.py update-password <site> <new_password>
  python3 credentials_manager.py update-from-email <site>    # extract new pw from email
"""
import json, os, sys, re, subprocess

CRED_FILE = os.path.expanduser("~/.applypilot/credentials.json")

def _load():
    if os.path.exists(CRED_FILE):
        with open(CRED_FILE) as f:
            return json.load(f)
    return {}

def _save(data):
    with open(CRED_FILE, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(CRED_FILE, 0o600)  # owner read/write only

def save(site, email, password):
    data = _load()
    data[site.lower()] = {"email": email, "password": password}
    _save(data)
    print(f"Saved credentials for {site}")

def get(site):
    data = _load()
    entry = data.get(site.lower())
    if entry:
        print(json.dumps(entry))
    else:
        print("null")
        return None
    return entry

def list_all():
    data = _load()
    if not data:
        print("No saved credentials")
        return
    for site, entry in sorted(data.items()):
        print(f"{site:30} {entry['email']:35} password={entry['password']}")

def update_password(site, new_password):
    data = _load()
    if site.lower() not in data:
        print(f"No credentials found for {site}")
        return False
    data[site.lower()]["password"] = new_password
    _save(data)
    print(f"Updated password for {site}")
    return True

def update_from_email(site, email_password=None):
    """Search email for a 'forgot password' / 'password reset' link,
    extract the link, and return it so the agent can navigate there.
    Prints the reset URL for the agent to use."""
    result = subprocess.run(
        [sys.executable, os.path.expanduser("~/.applypilot/email_verifier.py"),
         "search", "subject:(reset OR password OR forgot)"],
        capture_output=True, text=True, timeout=15
    )
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__.strip())
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "save":
        if len(sys.argv) != 5:
            print("Usage: credentials_manager.py save <site> <email> <password>")
            sys.exit(1)
        save(sys.argv[2], sys.argv[3], sys.argv[4])
    elif cmd == "get":
        if len(sys.argv) != 3:
            print("Usage: credentials_manager.py get <site>")
            sys.exit(1)
        get(sys.argv[2])
    elif cmd == "list":
        list_all()
    elif cmd == "update-password":
        if len(sys.argv) != 4:
            print("Usage: credentials_manager.py update-password <site> <new_password>")
            sys.exit(1)
        update_password(sys.argv[2], sys.argv[3])
    elif cmd == "update-from-email":
        if len(sys.argv) != 3:
            print("Usage: credentials_manager.py update-from-email <site>")
            sys.exit(1)
        update_from_email(sys.argv[2])
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
