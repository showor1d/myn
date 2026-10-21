"""Lifecycle helpers for MYN-owned local speech workers."""
import os
import subprocess


def terminate(process):
    """Stop a live worker and its Windows launcher children; ignore exited workers."""
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        # uv's venv launcher spawns the managed Python as a child process.
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
    else:
        process.kill()
