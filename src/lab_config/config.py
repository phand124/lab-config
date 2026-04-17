import os
try:
    from dotenv import load_dotenv
    from pathlib import Path
    
    load_dotenv(Path.cwd() / ".env")
except ImportError:
    pass

NETBOX_URL        = os.environ.get("NETBOX_URL")
NETBOX_TOKEN      = os.environ.get("NETBOX_TOKEN")
NETBOX_VERIFY_SSL = os.environ.get("NETBOX_VERIFY_SSL", "true").lower() not in ("false", "0", "no")

def require_netbox_config():
    missing = [k for k, v in [("NETBOX_URL", NETBOX_URL), ("NETBOX_TOKEN", NETBOX_TOKEN)] if not v]
    if missing:
        raise EnvironmentError(f"Missing required env var(s): {', '.join(missing)}")
    return NETBOX_URL, NETBOX_TOKEN, NETBOX_VERIFY_SSL
