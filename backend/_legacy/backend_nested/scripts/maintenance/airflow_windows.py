"""
Wrapper to run Airflow commands on Windows by mocking Unix-specific signals and functions.
Usage: python scripts/airflow_windows.py <command> <args>
Example: python scripts/airflow_windows.py dags test Main_Ingestion_DAG 2026-03-22
"""
import sys
import os
import signal
import logging

try:
    from dotenv import load_dotenv, find_dotenv
    # Attempt to load .env from standard project location first
    dotenv_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "agriconnect", ".env"))
    if os.path.exists(dotenv_path):
        success = load_dotenv(dotenv_path)
        if success:
            logging.info(f"Loaded environment variables from {dotenv_path}")
    else:
        # Fallback to search in CWD
        load_dotenv(find_dotenv(usecwd=True))
except ImportError:
    logging.warning("Please install python-dotenv to load environment variables from .env automaticaly.")

# --- Windows Compatibility Patches ---
if sys.platform == 'win32':
    # 1. Patch SIGALRM (Missing on Windows)
    if not hasattr(signal, 'SIGALRM'):
        # Define it so code referencing standard signal numbers via attribute works
        signal.SIGALRM = 14 

    # 2. Patch signal.signal to ignore SIGALRM handlers
    _original_signal = signal.signal
    def _safe_signal(sig, action):
        if sig == signal.SIGALRM:
            logging.debug("Ignored SIGALRM registration on Windows")
            return None
        return _original_signal(sig, action)
    signal.signal = _safe_signal

    # 3. Patch signal.alarm (Missing on Windows, used for timeouts)
    if not hasattr(signal, 'alarm'):
        def _alarm(seconds):
            logging.debug(f"Ignored signal.alarm({seconds}) on Windows")
            pass
        signal.alarm = _alarm

    # 3.1. Patch signal.setitimer/ITIMER_REAL (Also missing on Windows, used by newer Airflow)
    if not hasattr(signal, 'setitimer'):
        if not hasattr(signal, 'ITIMER_REAL'):
            signal.ITIMER_REAL = 0  # Arbitrary constant
        
        def _setitimer(which, seconds, interval=0):
            logging.debug(f"Ignored signal.setitimer({which}, {seconds}, {interval}) on Windows")
            pass
        signal.setitimer = _setitimer
 
    # 4. Patch fcntl (File locking, missing on Windows)
    try:
        import fcntl
    except ImportError:
        # Create a dummy fcntl module
        from types import ModuleType
        dummy_fcntl = ModuleType("fcntl")
        dummy_fcntl.LOCK_EX = 2
        dummy_fcntl.LOCK_SH = 1
        dummy_fcntl.LOCK_NB = 4
        dummy_fcntl.LOCK_UN = 8
        
        def _flock(fd, operation):
            pass
            
        def _fcntl(fd, op, arg=0):
            return 0
            
        dummy_fcntl.flock = _flock
        dummy_fcntl.fcntl = _fcntl
        sys.modules["fcntl"] = dummy_fcntl

    # 5. Patch os.fchmod
    if not hasattr(os, 'fchmod'):
        def _fchmod(fd, mode):
            pass
        os.fchmod = _fchmod
        
    # 6. Patch os.set_inheritable/sockets for Windows asyncio/spawn issues
    # Airflow 3.x uses socket passing which fails on Windows due to handle inheritance complexity
    # We attempt to wrap it, though this is deep system level.
    # The error was: OSError: [Errno 9] Bad file descriptor in os.set_inheritable
    
    # We can try to skip set_inheritable if it fails, or mock it?
    # Mocking it strictly might break things that rely on it.
    # But usually on Windows, sockets are not inheritable by default like FD in Unix.
    
    _orig_set_inheritable = os.set_inheritable
    def _safe_set_inheritable(fd, inheritable):
        try:
            _orig_set_inheritable(fd, inheritable)
        except OSError as e:
            if e.errno == 9: # Bad file descriptor
                logging.debug(f"Ignored os.set_inheritable({fd}, {inheritable}) - Bad FD")
            else:
                raise
    os.set_inheritable = _safe_set_inheritable

    # 7. Patch signal.set_wakeup_fd (Windows doesn't support this with sockets effectively/same way)
    if hasattr(signal, 'set_wakeup_fd'):
         _orig_set_wakeup_fd = signal.set_wakeup_fd
         def _safe_set_wakeup_fd(fd):
             try:
                 return _orig_set_wakeup_fd(fd)
             except ValueError:
                 return -1
         signal.set_wakeup_fd = _safe_set_wakeup_fd

# --- End Patches ---

from airflow.__main__ import main

if __name__ == "__main__":
    # Ensure local sources are in path for DAG imports
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src_path = os.path.join(repo_root, "src")
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    if src_path not in sys.path:
        sys.path.insert(0, src_path)

    sys.exit(main())