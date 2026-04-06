"""Detect and kill processes listening on ports 8003 and 8004 (Windows).
Usage: run this script from project root or backend folder via the project venv Python.
"""
import subprocess
import sys
import re

ports = {8003, 8004}
try:
    out = subprocess.check_output(["netstat", "-ano"], text=True, stderr=subprocess.STDOUT)
except Exception as e:
    print("ERROR: failed to run netstat:", e)
    sys.exit(2)

pids = set()
for line in out.splitlines():
    parts = re.split(r"\s+", line.strip())
    if len(parts) < 5:
        continue
    proto = parts[0]
    local = parts[1]
    state = parts[3] if len(parts) >= 4 else ''
    pid = parts[-1]
    # local may be like 0.0.0.0:8003 or [::]:8003
    m = re.search(r":(\d+)$", local)
    if not m:
        continue
    port = int(m.group(1))
    if port in ports:
        # Capture any entry (LISTENING or ESTABLISHED)
        try:
            pids.add(int(pid))
        except Exception:
            pass

if not pids:
    print("No processes found listening on ports", sorted(list(ports)))
    sys.exit(0)

killed = []
failed = []
for pid in sorted(pids):
    try:
        subprocess.check_call(["taskkill", "/PID", str(pid), "/F"])
        killed.append(pid)
    except subprocess.CalledProcessError as e:
        failed.append((pid, e))

print("Found PIDs:", sorted(list(pids)))
if killed:
    print("Killed:", killed)
if failed:
    print("Failed to kill:")
    for pid, err in failed:
        print(pid, err)

# exit code: 0 if killed or nothing, 1 if failures
sys.exit(0 if not failed else 1)
