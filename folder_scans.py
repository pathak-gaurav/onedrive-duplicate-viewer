"""Run a read-only Rclone scan for one OneDrive folder, apart from the main report."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time


def normalize_folder(path, allow_root=False):
    if not isinstance(path, str) or len(path) > 2048 or any(char in path for char in ('\\', ':', '\0', '\r', '\n')):
        raise ValueError('Choose a OneDrive folder from the folder picker.')
    if not path and allow_root:
        return ''
    if not path or any(part in ('', '.', '..') for part in path.split('/')):
        raise ValueError('Choose a specific folder inside OneDrive.')
    if path.split('/')[0].casefold() == 'personal vault':
        raise ValueError('Personal Vault is not available through this connection.')
    return path


class FolderScans:
    def __init__(self, onedrive, directory, report_factory, popen=None, main_report=None):
        self.onedrive = onedrive
        self.directory = Path(directory)
        self.report_factory = report_factory
        self.popen = popen or subprocess.Popen
        self.lock = threading.Lock()
        self.process = None
        self.cancelled = threading.Event()
        self.active = False
        self.pending_path = None
        self.main_report = main_report
        self.report = None
        self.scope = None
        self.info = {'status': 'idle', 'path': '', 'message': 'Choose a folder, then start a scan.'}
        if main_report is not None:
            self.report = main_report
            self.info = {'status': 'idle', 'path': '', 'message': 'Showing the saved OneDrive results.'}
            return
        try:
            saved = json.loads((self.directory / 'last.json').read_text(encoding='utf-8'))
            self.scope = normalize_folder(saved['path'])
            self.report = self.report_factory(self.report_path(self.scope), prefix=self.scope)
            self.report.load()
            self.info = {'status': 'completed', 'path': self.scope, 'message': 'Showing your last completed folder scan.'}
        except (OSError, ValueError, KeyError):
            self.report = None
            self.scope = None

    def report_path(self, path):
        if self.main_report is not None:
            return self.main_report.path
        return self.directory / hashlib.sha256(path.encode('utf-8')).hexdigest() / 'report.txt'

    def results(self):
        with self.lock:
            report, scope = self.report, self.scope
        return {**report.load(), 'scope': scope} if report else {'groups': [], 'revision': None, 'scannedAt': None, 'scope': None}

    def status(self):
        with self.lock:
            info = dict(self.info)
            if self.pending_path is not None:
                info.update(status='running', path=self.pending_path, message='A fresh scan is queued to run after the current scan finishes.')
        if info.get('started'):
            info['elapsed'] = int(time.monotonic() - info['started'])
        info.pop('started', None)
        return info

    def validate_scope(self, path):
        if self.main_report is not None:
            if path != '':
                raise ValueError('The OneDrive scan must start at the drive root.')
            return ''
        return normalize_folder(path)

    def set_running(self, path):
        label = 'OneDrive' if self.main_report is not None else 'this folder and its subfolders'
        self.info = {'status': 'running', 'path': path, 'message': f'Scanning {label}. Original files stay in OneDrive.', 'started': time.monotonic()}

    def start(self, path):
        path = self.validate_scope(path)
        with self.lock:
            if self.active:
                raise ValueError('A folder scan is already running. Wait for it or cancel it first.')
            self.cancelled.clear()
            self.active = True
            self.set_running(path)
            self.thread = threading.Thread(target=self.run, args=(path,), daemon=True)
            self.thread.start()
        return self.status()

    def request_refresh(self, path):
        path = self.validate_scope(path)
        with self.lock:
            if self.active:
                self.pending_path = path
                return {**self.info, 'status': 'running', 'path': path, 'message': 'A fresh scan is queued to run after the current scan finishes.'}
        return self.start(path)

    def cancel(self):
        with self.lock:
            if self.active:
                self.cancelled.set()
                self.pending_path = None
                if self.process and self.process.poll() is None:
                    self.process.terminate()
        return self.status()

    def run(self, path):
        while True:
            self.run_once(path)
            with self.lock:
                if self.cancelled.is_set():
                    self.pending_path = None
                if self.pending_path is None:
                    self.active = False
                    return
                path, self.pending_path = self.pending_path, None
                self.set_running(path)

    def run_once(self, path):
        try:
            if path:
                self.onedrive.check_folder(path)
            if self.cancelled.is_set():
                return
            destination = self.report_path(path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix('.part')
            rclone = Path(os.environ['LOCALAPPDATA']) / 'Microsoft/WinGet/Links/rclone.exe'
            command = [str(rclone), 'dedupe', '--by-hash', '--dedupe-mode', 'list', '--dry-run', '--tpslimit', '2', '--tpslimit-burst', '1', '--checkers', '2', '--retries', '1', '--low-level-retries', '3']
            # Delta is efficient for a whole-drive scan, but would list the whole drive for a folder too.
            command += ['--onedrive-delta=false', '--fast-list=false'] if path else ['--onedrive-delta', '--fast-list', '--exclude', '/Personal Vault/**']
            command.append('onedrive:' + path)
            with temporary.open('wb') as output, destination.with_suffix('.log').open('wb') as errors:
                with self.lock:
                    if self.cancelled.is_set():
                        return
                    self.process = self.popen(command, stdout=output, stderr=errors, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    process = self.process
                code = process.wait()
            if self.cancelled.is_set():
                return
            if code:
                raise ValueError('OneDrive could not finish this folder scan. Try again later. Your previous results are still available.')
            self.report_factory(temporary, prefix=path).load()
            temporary.replace(destination)
            new_report = self.report_factory(destination, prefix=path)
            new_report.load()
            if self.main_report is None:
                pointer = self.directory / 'last.tmp'
                pointer.write_text(json.dumps({'path': path}, ensure_ascii=False), encoding='utf-8')
                pointer.replace(self.directory / 'last.json')
            with self.lock:
                self.report, self.scope = new_report, path
                message = 'Folder scan complete. Results include this folder and its subfolders.' if path else 'OneDrive scan complete. Results were refreshed from the cloud.'
                self.info = {'status': 'completed', 'path': path, 'message': message}
        except Exception as error:
            with self.lock:
                self.info = {'status': 'failed', 'path': path, 'message': str(error) if isinstance(error, ValueError) or hasattr(error, 'status') else 'The folder scan could not finish. Check your connection and try again.'}
        finally:
            with self.lock:
                self.process = None
                if self.cancelled.is_set():
                    self.info = {'status': 'cancelled', 'path': path, 'message': 'Scan cancelled. Your previous results are still available.'}
