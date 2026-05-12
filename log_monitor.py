#!/usr/bin/env python3
"""
Log Monitor Script
Monitors log files for errors and sends notifications when issues are detected.

VGX Consulting - Log Monitoring Solution
Copyright (c) 2026 VGX Consulting. All rights reserved.

This is proprietary software. Unauthorized copying, modification, distribution,
or use of this software is strictly prohibited.
"""

import os
import re
import ssl
import sys
import html
import json
import time
import shutil
import smtplib
import tarfile
import argparse
import configparser
import urllib.request
import concurrent.futures
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import List, Dict, Any, Optional

__version__ = "1.5.1"


class LogMonitor:
    REPO = "VGXDigital/LogMon"
    LOG_PREFIX = "[LogMon]"

    error_patterns = [
        r'error', r'fail', r'exception', r'traceback', r'critical',
        r'fatal', r'warning', r'not found', r'permission denied',
        r'connection refused', r'timeout', r'unable to', r'could not',
        r'exit code[:\s]+[1-9]', r'returned non-zero', r'aborted', r'killed'
    ]

    def _log(self, message: str) -> None:
        """Print a message with the LogMon prefix so self-scanning can skip it."""
        for line in message.splitlines(keepends=False):
            print(f"{self.LOG_PREFIX} {line}")

    def __init__(self, debug: bool = False):
        """Initialize the LogMonitor."""
        self.debug = debug
        if self.debug:
            self._log("=" * 60)
            self._log("VGX Log Monitor - Debug Mode")
            self._log(f"Run started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            self._log("=" * 60)

        self._configure()
        self.compiled_pattern = re.compile('|'.join(self.error_patterns), re.IGNORECASE)

        if self.debug:
            self._print_debug_info()
            self.notification_file.parent.mkdir(parents=True, exist_ok=True)
            self.notification_file.touch(exist_ok=True)

    @staticmethod
    def _strip_quotes(value) -> Optional[str]:
        """Strip surrounding quotes from config values."""
        if value is None:
            return None
        value = str(value)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            return value[1:-1]
        return value

    def _configure(self) -> None:
        """Load configuration from file and environment.

        Config lookup order:
        1. Current working directory
        2. Directory where the binary/script lives
        """
        config = configparser.ConfigParser()
        config_file = Path.cwd() / 'log_monitor.conf'
        if not config_file.exists():
            # For PyInstaller binaries: check next to the executable
            binary_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
            config_file = binary_dir / 'log_monitor.conf'
        if config_file.exists():
            config.read(config_file)
            if self.debug:
                self._log(f"Config file: {config_file}")

        def cfg(section: str, key: str, fallback: Any = None) -> Optional[str]:
            return self._strip_quotes(config.get(section, key, fallback=fallback))

        # Path settings
        self.log_dir = Path(os.getenv('VGX_LM_LOG_DIR') or cfg('Paths', 'log_dir', fallback=Path.home() / 'logs'))
        script_dir = Path(__file__).parent.resolve()
        writable_dir = self._get_writable_directory(script_dir)
        self.notification_file = Path(cfg('Paths', 'notification_file', fallback=writable_dir / 'notifications.log'))
        self.last_check_file = Path(cfg('Paths', 'last_check_file', fallback=writable_dir / '.last_check'))
        self.file_offsets_path = writable_dir / '.file_offsets'

        # SMTP settings
        self.smtp_server = os.getenv('VGX_LM_SMTP_SERVER') or cfg('SMTP', 'server')
        self.smtp_port = int(os.getenv('VGX_LM_SMTP_PORT') or cfg('SMTP', 'port', fallback='465'))
        self.smtp_username = os.getenv('VGX_LM_SMTP_USERNAME') or cfg('SMTP', 'username')
        self.smtp_password = os.getenv('VGX_LM_SMTP_PASSWORD') or cfg('SMTP', 'password')
        self.smtp_from_email = os.getenv('VGX_LM_SMTP_FROM') or cfg('SMTP', 'from_email')
        self.smtp_to_email = os.getenv('VGX_LM_SMTP_TO') or cfg('SMTP', 'to_email')

        # Auto-update settings
        self.auto_update = config.getboolean('Settings', 'auto_update', fallback=True)

    def _print_debug_info(self) -> None:
        """Print debug information."""
        self._log("Monitoring with combined regex pattern")
        self._log(f"Log directory: {self.log_dir}")
        self._log(f"State directory: {self.last_check_file.parent}")
        self._log(f"Notification file: {self.notification_file}")
        self._log(f"Last check file: {self.last_check_file}")
        self._log(f"SMTP Server: {self.smtp_server}:{self.smtp_port}")
        self._log(f"SMTP From: {self.smtp_from_email}")
        self._log(f"SMTP To: {self.smtp_to_email}")
        self._log(f"SMTP Username: {self.smtp_username}")

    def _get_writable_directory(self, preferred_dir: Path) -> Path:
        """Determine writable directory for state files."""
        test_file = preferred_dir / '.write_test'
        try:
            test_file.write_text('test')
            test_file.unlink()
            return preferred_dir
        except (OSError, IOError):
            fallback_dir = Path('/tmp/vgx.logmonitor')
            fallback_dir.mkdir(exist_ok=True)
            return fallback_dir

    # ── Self-update ────────────────────────────────────────────

    def _update_check_file(self) -> Path:
        return self.last_check_file.parent / '.last_update_check'

    def _should_check_update(self) -> bool:
        """True if at least 24 hours since last update check."""
        uc = self._update_check_file()
        if uc.exists():
            try:
                if time.time() - float(uc.read_text().strip()) < 86400:
                    return False
            except (ValueError, OSError):
                pass
        return True

    def check_for_update(self, force: bool = False) -> bool:
        """Check GitHub for a newer release and self-update. Returns True if updated."""
        if not getattr(sys, 'frozen', False):
            if self.debug:
                self._log("Auto-update skipped: running from source (use git pull)")
            return False

        if not force and not self._should_check_update():
            if self.debug:
                self._log("Update check skipped: already checked today")
            return False

        if self.debug:
            self._log(f"\nChecking for updates (current: v{__version__})...")

        try:
            api_url = f"https://api.github.com/repos/{self.REPO}/releases/latest"
            req = urllib.request.Request(api_url, headers={"Accept": "application/vnd.github.v3+json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                release = json.loads(resp.read())

            # Record that we checked (even if no update)
            try:
                self._update_check_file().write_text(str(time.time()))
            except OSError:
                pass

            latest_version = release.get("tag_name", "").lstrip("v")
            if not latest_version:
                return False

            current = tuple(int(x) for x in __version__.split('.'))
            latest = tuple(int(x) for x in latest_version.split('.'))

            if latest <= current:
                if self.debug:
                    self._log(f"Already up to date: v{__version__}")
                return False

            if self.debug:
                self._log(f"New version available: v{latest_version}")

            # Find the linux tarball asset
            tarball_url = None
            for asset in release.get("assets", []):
                if asset["name"].endswith("-linux-x86_64.tar.gz"):
                    tarball_url = asset["browser_download_url"]
                    break

            if not tarball_url:
                if self.debug:
                    self._log("No compatible binary found in release")
                return False

            return self._download_and_install(tarball_url, latest_version)

        except Exception as e:
            if self.debug:
                self._log(f"Update check failed: {e}")
            return False

    def _download_and_install(self, url: str, version: str) -> bool:
        """Download release tarball and replace current binary."""
        binary_path = Path(sys.executable)
        tmp_dir = None

        if self.debug:
            self._log(f"Downloading v{version}...")

        try:
            import tempfile
            tmp_dir = tempfile.mkdtemp(prefix="logmon_update_")
            tarball_path = os.path.join(tmp_dir, "release.tar.gz")

            urllib.request.urlretrieve(url, tarball_path)

            with tarfile.open(tarball_path, 'r:gz') as tar:
                try:
                    tar.extractall(tmp_dir, filter='data')
                except TypeError:
                    tar.extractall(tmp_dir)

            new_binary = Path(tmp_dir) / "log_monitor"
            if not new_binary.exists():
                if self.debug:
                    self._log("Binary not found in archive")
                return False

            # Backup current binary
            backup_path = binary_path.with_suffix('.bak')
            shutil.copy2(str(binary_path), str(backup_path))

            # Replace binary (atomic on same filesystem)
            shutil.move(str(new_binary), str(binary_path))
            os.chmod(str(binary_path), 0o755)

            if self.debug:
                self._log(f"Updated to v{version} (backup: {backup_path})")
                self._log("New version will be active on next run")

            return True

        except Exception as e:
            if self.debug:
                self._log(f"Update failed: {e}")
            return False
        finally:
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)

    # ── Log truncation ────────────────────────────────────────

    def truncate_logs(self) -> None:
        """Truncate all monitored log files to zero bytes."""
        log_files = self.get_log_files()
        if not log_files:
            if self.debug:
                self._log("No log files found to truncate.")
            return

        for log_file in log_files:
            try:
                log_file.write_text('')
                if self.debug:
                    self._log(f"  Truncated: {log_file}")
            except (OSError, IOError) as e:
                self._log(f"  Failed to truncate {log_file}: {e}")

        self._log(f"Truncated {len(log_files)} log file(s).")
        # Reset scan offsets since all files are now empty
        self._save_file_offsets({})

    # ── Log scanning ─────────────────────────────────────────

    def get_log_files(self) -> List[Path]:
        """Get all .log files recursively in log_dir, excluding notification file."""
        if not self.log_dir.exists():
            if self.debug:
                self._log(f"WARNING: Log directory does not exist: {self.log_dir}")
            return []

        log_files = list(self.log_dir.rglob('*.log'))
        # Exclude the notification file to avoid scanning itself
        filtered_files = [f for f in log_files if f.resolve() != self.notification_file.resolve()]

        if self.debug:
            self._log(f"\nFound {len(filtered_files)} log file(s) to monitor:")
            for f in filtered_files:
                self._log(f"  - {f}")

        return filtered_files

    def get_last_check_time(self) -> float:
        """Get the timestamp of the last check from file."""
        if self.last_check_file.exists():
            try:
                return float(self.last_check_file.read_text().strip())
            except ValueError:
                return 0
        return 0

    def set_last_check_time(self, timestamp: float) -> None:
        """Save the timestamp of the current check."""
        self.last_check_file.write_text(str(timestamp))

    def _load_file_offsets(self) -> Dict[str, Dict[str, int]]:
        """Load per-file scan positions from state file.

        Returns dict of {filepath: {"byte": N, "line": N}}.
        """
        if self.file_offsets_path.exists():
            try:
                data = json.loads(self.file_offsets_path.read_text())
                # Migrate from old format (flat int offsets) if needed
                migrated: Dict[str, Dict[str, int]] = {}
                for k, v in data.items():
                    if isinstance(v, int):
                        migrated[k] = {"byte": v, "line": 0}
                    else:
                        migrated[k] = v
                return migrated
            except (json.JSONDecodeError, ValueError):
                return {}
        return {}

    def _save_file_offsets(self, offsets: Dict[str, Dict[str, int]]) -> None:
        """Save per-file scan positions to state file."""
        self.file_offsets_path.write_text(json.dumps(offsets))

    def find_errors_in_file(self, filepath: Path, since_timestamp: float,
                            last_byte: int, last_line: int) -> tuple:
        """Find errors in a specific file since the last scanned position.

        Returns (errors_list, new_byte_offset, new_line_number).
        """
        try:
            stat = filepath.stat()
            file_size = stat.st_size
            if stat.st_mtime < since_timestamp:
                return [], last_byte, last_line
        except OSError:
            return [], 0, 0

        # Log rotation detected: file is smaller than our saved offset
        if file_size < last_byte:
            last_byte = 0
            last_line = 0
            if self.debug:
                self._log(f"  Log rotation detected for {filepath}, scanning from start")

        errors = []
        line_number = last_line
        new_byte = last_byte
        try:
            with filepath.open('r', encoding='utf-8', errors='ignore') as f:
                f.seek(last_byte)
                for line in f:
                    line_number += 1
                    if self.LOG_PREFIX in line:
                        continue
                    if self.compiled_pattern.search(line):
                        errors.append({
                            'file': str(filepath),
                            'line_number': line_number,
                            'line_content': line.strip(),
                            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        })
                new_byte = f.tell()
        except Exception as e:
            if self.debug:
                self.log_notification(f"Error reading file {filepath}: {e}")

        return errors, new_byte, line_number

    def scan_all_logs(self) -> List[Dict[str, Any]]:
        """Scan all log files for errors using a thread pool."""
        current_time = time.time()
        last_check = self.get_last_check_time()
        file_offsets = self._load_file_offsets()

        if last_check == 0:
            last_check = current_time - 3600
            if self.debug:
                self._log("\nFirst run detected - scanning last hour of logs")

        if self.debug:
            last_check_time = datetime.fromtimestamp(last_check).strftime('%Y-%m-%d %H:%M:%S')
            self._log(f"\nLast check: {last_check_time}")
            self._log("\nScanning log files for errors...")

        all_errors: List[Dict[str, Any]] = []
        log_files = self.get_log_files()
        new_offsets: Dict[str, Dict[str, int]] = {}

        with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count()) as executor:
            future_to_file = {}
            for log_file in log_files:
                pos = file_offsets.get(str(log_file), {"byte": 0, "line": 0})
                future = executor.submit(
                    self.find_errors_in_file, log_file, last_check,
                    pos["byte"], pos["line"]
                )
                future_to_file[future] = log_file

            for future in concurrent.futures.as_completed(future_to_file):
                log_file = future_to_file[future]
                try:
                    errors, byte_offset, line_num = future.result()
                    new_offsets[str(log_file)] = {"byte": byte_offset, "line": line_num}
                    if errors:
                        if self.debug:
                            self._log(f"    Found {len(errors)} error(s) in {log_file}")
                        all_errors.extend(errors)
                except Exception as exc:
                    if self.debug:
                        self._log(f"{log_file} generated an exception: {exc}")

        self.set_last_check_time(current_time)
        self._save_file_offsets(new_offsets)

        if self.debug:
            self._log(f"\nTotal errors found: {len(all_errors)}")

        return all_errors

    def log_notification(self, message: str) -> None:
        """Log notification to file (only in debug mode)."""
        if not self.debug:
            return

        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        log_entry = f"[{timestamp}] {message}\n"

        try:
            with self.notification_file.open('a') as f:
                f.write(log_entry)
        except (OSError, IOError) as e:
            self._log(f"Warning: Could not write to notification file: {e}")

    def send_notification(self, errors: List[Dict[str, Any]]) -> None:
        """Send notifications about detected errors."""
        if not errors:
            return

        error_count = len(errors)
        
        # Log to notification file
        self.log_notification(f"Detected {error_count} errors")

        # Send email notification
        self.send_email_notification(errors)

    def _create_html_email(self, errors: List[Dict[str, Any]]) -> str:
        """Create an HTML formatted email body."""
        error_count = len(errors)
        
        body = f"""
        <html>
            <head>
                <style>
                    body {{ font-family: sans-serif; }}
                    h1 {{ color: #d9534f; }}
                    table {{ border-collapse: collapse; width: 100%; }}
                    th, td {{ border: 1px solid #ddd; padding: 8px; }}
                    th {{ background-color: #f2f2f2; }}
                </style>
            </head>
            <body>
                <h1>Log Monitor Alert: {error_count} error(s) detected</h1>
                <p><strong>Time:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
                <table>
                    <tr>
                        <th>File</th>
                        <th>Line</th>
                        <th>Content</th>
                        <th>Timestamp</th>
                    </tr>
        """

        for error in errors:
            body += f"""
                    <tr>
                        <td>{html.escape(error['file'])}</td>
                        <td>{error['line_number']}</td>
                        <td><code>{html.escape(error['line_content'])}</code></td>
                        <td>{error['timestamp']}</td>
                    </tr>
            """

        body += f"""
                </table>
                <hr style="border: none; border-top: 1px solid #ddd; margin: 30px 0 15px;">
                <p style="font-size: 12px; color: #888; text-align: center;">
                    Generated by LogMon v{__version__} from
                    <a href="https://vgx.digital" style="color: #337ab7; text-decoration: none;">VGX Consulting</a>
                </p>
            </body>
        </html>
        """
        return body

    def send_email_notification(self, errors: List[Dict[str, Any]]) -> None:
        """Send email notification via SMTP."""
        error_count = len(errors)
        try:
            if self.debug:
                self._log("\nAttempting to send email notification...")
                self._log(f"  SMTP Server: {self.smtp_server}:{self.smtp_port}")
                self._log(f"  From: {self.smtp_from_email}")
                self._log(f"  To: {self.smtp_to_email}")
                self._log(f"  Username: {self.smtp_username}")

            if not all([self.smtp_server, self.smtp_username, self.smtp_password, self.smtp_from_email, self.smtp_to_email]):
                raise ValueError("Missing SMTP configuration. Please check your config file or environment variables.")

            msg = MIMEMultipart()
            msg['From'] = self.smtp_from_email
            msg['To'] = self.smtp_to_email
            msg['Subject'] = f"Log Monitor Alert: {error_count} errors detected"
            
            html_body = self._create_html_email(errors)
            msg.attach(MIMEText(html_body, 'html'))

            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(self.smtp_server, self.smtp_port, context=context) as server:
                server.login(self.smtp_username, self.smtp_password)
                server.sendmail(self.smtp_from_email, self.smtp_to_email, msg.as_string())

            if self.debug:
                self._log(f"  Email sent successfully to {self.smtp_to_email}")
            self.log_notification(f"Email sent to {self.smtp_to_email}")

        except Exception as e:
            error_msg = f"Failed to send email: {e}"
            if self.debug:
                self._log(f"  {error_msg}")
            self.log_notification(error_msg)

    def run(self) -> None:
        """Main method to run the log monitor."""
        # Auto-update before scanning
        if self.auto_update:
            self.check_for_update()

        if self.debug:
            self._log("\n" + "=" * 60)
            self._log("Starting log scan...")
            self._log("=" * 60)

        errors = self.scan_all_logs()

        if errors:
            self.send_notification(errors)
        elif self.debug:
            self._log("\nNo errors found - all clear!")

        if self.debug:
            self._log("\n" + "=" * 60)
            self._log("Log monitor completed")
            self._log(f"Run finished at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            self._log("=" * 60)


def main():
    """Main function to run the log monitor."""
    parser = argparse.ArgumentParser(
        description=f'VGX LogMon v{__version__} — Monitor log files for errors and send notifications',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('--debug', action='store_true',
                        help='Enable debug mode with detailed logging to notification file')
    parser.add_argument('--version', action='version',
                        version=f'%(prog)s {__version__}')
    parser.add_argument('--truncate-logs', action='store_true',
                        help='Truncate all monitored log files to zero bytes, then exit')
    parser.add_argument('--update', action='store_true',
                        help='Check for and install the latest version, then exit')

    args = parser.parse_args()

    try:
        if args.truncate_logs:
            monitor = LogMonitor(debug=args.debug)
            monitor.truncate_logs()
            return

        if args.update:
            monitor = LogMonitor(debug=args.debug)
            monitor._log(f"Checking for updates (current: v{__version__})...")
            if monitor.check_for_update(force=True):
                monitor._log("Updated successfully. New version active on next run.")
            else:
                monitor._log(f"Already at latest version: v{__version__}")
            return

        monitor = LogMonitor(debug=args.debug)
        monitor.run()
    except Exception as e:
        print(f"{LogMonitor.LOG_PREFIX} An unexpected error occurred: {e}")
        if args.debug:
            import traceback
            for line in traceback.format_exc().splitlines():
                print(f"{LogMonitor.LOG_PREFIX} {line}")


if __name__ == "__main__":
    main()
