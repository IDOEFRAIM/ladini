
import sys
import os
from pathlib import Path

# --- Windows Compatibility Patch ---
if sys.platform == 'win32':
    import unittest.mock
    fcntl = unittest.mock.Mock()
    # fcntl.ioctl should return bytes for TIOCGWINSZ
    # Struct "HHHH" is 8 bytes
    fcntl.ioctl.return_value = b'\x00' * 8
    sys.modules['fcntl'] = fcntl
    sys.modules['termios'] = unittest.mock.Mock()
    sys.modules['tty'] = unittest.mock.Mock()

# Set AIRFLOW_HOME locally to avoid polluting user profile (and permissions)
# Using 'backend/airflow_home' inside the current dir
airflow_home = Path(__file__).resolve().parent.parent / "airflow_home"
if not airflow_home.exists():
    airflow_home.mkdir(parents=True, exist_ok=True)
os.environ["AIRFLOW_HOME"] = str(airflow_home)

print(f"Using AIRFLOW_HOME={airflow_home}")

# Now import airflow
try:
    import airflow.utils.db
    from contextlib import contextmanager

    # Patch timeout_with_traceback which uses SIGALRM (unavailable on Windows)
    @contextmanager
    def dummy_timeout(seconds, error_message=None):
        yield

    airflow.utils.db.timeout_with_traceback = dummy_timeout
    
    from airflow.utils.db import upgradedb
except ImportError:
    try:
        from airflow.utils.db import initdb as upgradedb
    except ImportError:
        print("Could not import upgradedb or initdb from airflow.utils.db")
        sys.exit(1)

if __name__ == "__main__":
    print(f"Initializing Airflow DB...")
    try:
        upgradedb()
        print("Airflow DB initialized successfully.")
    except Exception as e:
        print(f"Error initializing DB: {e}")
        import traceback
        traceback.print_exc()
