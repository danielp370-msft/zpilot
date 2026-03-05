#!/usr/bin/env python3
"""zpilot monitored shell — wraps bash with output logging and FIFO-based command injection.

This script is launched inside a Zellij pane to provide:
  - All terminal output logged to /tmp/zpilot/logs/<session>--main.log
  - Command injection via /tmp/zpilot/fifos/<session>.fifo

Usage:
    python3 -m zpilot.shell_wrapper <session-name> [command...]
"""

import os
import pty
import select
import sys
import threading
import time
from pathlib import Path

LOG_DIR = Path("/tmp/zpilot/logs")
FIFO_DIR = Path("/tmp/zpilot/fifos")


def main():
    session = sys.argv[1] if len(sys.argv) > 1 else "default"
    shell_cmd = sys.argv[2:] if len(sys.argv) > 2 else ["bash", "-i"]

    log_path = LOG_DIR / f"{session}--main.log"
    fifo_path = FIFO_DIR / f"{session}.fifo"

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    FIFO_DIR.mkdir(parents=True, exist_ok=True)

    # Create FIFO for command injection
    if fifo_path.exists():
        fifo_path.unlink()
    os.mkfifo(str(fifo_path))

    # Open log file
    log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)

    # Fork a PTY with the shell command
    child_pid, master_fd = pty.fork()

    if child_pid == 0:
        # Child: exec the shell/command
        os.environ["ZPILOT_SESSION"] = session
        os.execvp(shell_cmd[0], shell_cmd)
        sys.exit(1)

    # Parent: relay I/O, log output, and inject from FIFO

    def fifo_reader():
        """Read commands from FIFO and write to master (child's stdin)."""
        while True:
            try:
                fd = os.open(str(fifo_path), os.O_RDONLY)
                while True:
                    data = os.read(fd, 4096)
                    if not data:
                        break
                    os.write(master_fd, data)
                os.close(fd)
            except OSError:
                time.sleep(0.5)

    fifo_thread = threading.Thread(target=fifo_reader, daemon=True)
    fifo_thread.start()

    # Main loop: relay between master PTY ↔ stdin/stdout, and log all output
    try:
        while True:
            fds = [master_fd]
            if sys.stdin.isatty():
                fds.append(sys.stdin.fileno())
            try:
                rfds, _, _ = select.select(fds, [], [], 1.0)
            except (ValueError, OSError):
                break

            for fd in rfds:
                if fd == master_fd:
                    try:
                        data = os.read(master_fd, 4096)
                    except OSError:
                        data = b""
                    if not data:
                        break
                    os.write(sys.stdout.fileno(), data)
                    os.write(log_fd, data)
                    os.fsync(log_fd)
                elif fd == sys.stdin.fileno():
                    data = os.read(sys.stdin.fileno(), 4096)
                    if not data:
                        break
                    os.write(master_fd, data)
            else:
                continue
            break
    except KeyboardInterrupt:
        pass
    finally:
        os.close(log_fd)
        os.close(master_fd)
        try:
            os.waitpid(child_pid, 0)
        except ChildProcessError:
            pass
        try:
            fifo_path.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
