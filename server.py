"""Local duplicate viewer with explicit, recoverable OneDrive deletion. Standard library only."""
from __future__ import annotations

import argparse
from collections import OrderedDict
import configparser
import hashlib
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from folder_scans import FolderScans, normalize_folder

APP_DIR = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'OneDrive Duplicate Scanner'
HERE = Path(__file__).resolve().parent
IMAGE_EXTS = {'.jpg', '.jpeg', '.heic', '.heif', '.png', '.gif', '.webp', '.bmp', '.tif', '.tiff', '.avif', '.dng', '.raw'}
VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.m4v', '.wmv', '.webm', '.3gp'}
DOCUMENT_EXTS = {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.txt', '.rtf', '.csv'}


def file_type(path):
    extension = Path(path).suffix.lower()
    return 'image' if extension in IMAGE_EXTS else 'video' if extension in VIDEO_EXTS else 'document' if extension in DOCUMENT_EXTS else 'other'


def parse_report(text, prefix=''):
    groups = []
    current = None
    for line in text.splitlines():
        if re.match(r'^\d{4}/\d\d/\d\d \d\d:\d\d:\d\d (ERROR|CRITICAL)', line):
            raise ValueError('The last scan reported errors. Run a successful scan before using this report.')
        header = re.fullmatch(r'([0-9a-f]+): (\d+) duplicates', line)
        entry = re.fullmatch(r'\s+\d+:\s+(\d+) bytes, ([^,]+), (.*)', line)
        if header:
            current = {'id': header[1], 'expected': int(header[2]), 'files': []}
            groups.append(current)
        elif entry and current is not None:
            path = entry[3]
            if prefix:
                if path.startswith('/') or '\\' in path or any(part in ('', '.', '..') for part in path.split('/')):
                    raise ValueError('The folder report contains a path outside the selected folder.')
                path = prefix + '/' + path
            current['files'].append({'id': hashlib.sha256(path.encode('utf-8')).hexdigest(), 'path': path, 'name': path.rsplit('/', 1)[-1], 'size': int(entry[1]), 'modified': entry[2], 'type': file_type(path)})
        elif line.strip() and not re.match(r'^\d{4}/\d\d/\d\d \d\d:\d\d:\d\d ', line):
            raise ValueError('The scan report has an unexpected format. Run a new scan, then reload.')
    for group in groups:
        if len(group['files']) != group.pop('expected') or len(group['files']) < 2:
            raise ValueError('The scan report is incomplete. Wait for the scan to finish, then reload.')
        sizes = [file['size'] for file in group['files']]
        group.update(size=max(sizes), savings=sum(sizes) - max(sizes), sameSize=min(sizes) == max(sizes))
    if len({group['id'] for group in groups}) != len(groups):
        raise ValueError('The scan report contains repeated groups. Run a fresh scan.')
    return groups


class Report:
    def __init__(self, path, prefix=''):
        self.path = Path(path)
        self.prefix = prefix
        self.state_path = self.path.with_name('viewer-deletions.json')
        self.stamp = None
        self.groups = []
        self.lookup = {}
        self.lock = threading.Lock()

    def load(self):
        with self.lock:
            stat = self.path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
            if stamp != self.stamp:
                text = self.path.read_text(encoding='utf-8-sig')
                groups = parse_report(text, prefix=self.prefix)
                try:
                    deleted = json.loads(self.state_path.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    deleted = {}
                self.deleted = deleted.get('paths', {}) if deleted.get('revision') == self.revision(stat) else {}
                visible = []
                for group in groups:
                    group['files'] = [file for file in group['files'] if file['path'] not in self.deleted]
                    if len(group['files']) > 1:
                        sizes = [file['size'] for file in group['files']]
                        group.update(size=max(sizes), savings=sum(sizes) - max(sizes), sameSize=min(sizes) == max(sizes))
                        visible.append(group)
                # shortcut: Rclone reports have no completion marker; reload after the scan window says complete.
                self.groups = visible
                self.lookup = {group['id']: group for group in visible}
                self.stamp = stamp
            return {'groups': self.groups, 'revision': self.revision(stat), 'scannedAt': datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()}

    @staticmethod
    def revision(stat):
        return f'{stat.st_mtime_ns}:{stat.st_size}'

    def file(self, group_id, file_id):
        self.load()
        group = self.lookup.get(group_id)
        if group:
            for file in group['files']:
                if file['id'] == file_id:
                    return file
        raise ValueError('This file is not in the current duplicate report. Reload the report.')

    def mark_deleted(self, path, revision):
        self.load()
        with self.lock:
            if self.revision(self.path.stat()) != revision:
                raise ValueError('The scan changed during deletion. Reload the scan before continuing.')
            self.deleted[path] = datetime.now(timezone.utc).isoformat()
            state = {'revision': revision, 'paths': self.deleted}
            temporary = self.state_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
            temporary.replace(self.state_path)
            self.stamp = None


class PreviewError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def trusted_thumbnail_url(url):
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or '').lower()
    domains = ('svc.ms', '1drv.com', 'live.com', 'sharepoint.com', 'sharepointonline.com', 'onedrive.com', 'microsoft.com')
    return parsed.scheme == 'https' and not parsed.username and not parsed.password and parsed.port in (None, 443) and any(host == domain or host.endswith('.' + domain) for domain in domains)


class ThumbnailRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not trusted_thumbnail_url(newurl):
            raise PreviewError('OneDrive returned an unsupported thumbnail location.')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class NoGraphRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PreviewError('Unexpected redirect from Microsoft. Please reconnect Rclone.')


class OneDrive:
    def __init__(self):
        self.lock = threading.RLock()
        self.next_request = 0.0
        self.cooldown = 0.0
        self.metadata = OrderedDict()
        self.thumbnails = OrderedDict()
        self.graph_opener = urllib.request.build_opener(NoGraphRedirects())
        self.thumb_opener = urllib.request.build_opener(ThumbnailRedirects())

    def credentials(self):
        config = configparser.ConfigParser(interpolation=None)
        config.read(Path(os.environ['APPDATA']) / 'rclone' / 'rclone.conf', encoding='utf-8-sig')
        remote = config['onedrive']
        token = json.loads(remote['token'])
        expiry = datetime.fromisoformat(token['expiry'].replace('Z', '+00:00'))
        if expiry.timestamp() < time.time() + 90:
            rclone = Path(os.environ['LOCALAPPDATA']) / 'Microsoft/WinGet/Links/rclone.exe'
            result = subprocess.run([str(rclone), 'lsd', 'onedrive:', '--max-depth', '1', '--retries', '1', '--low-level-retries', '1'], capture_output=True, timeout=90, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode:
                raise PreviewError('The OneDrive connection needs attention. Reconnect Rclone and try again.', 401)
            config.read(Path(os.environ['APPDATA']) / 'rclone' / 'rclone.conf', encoding='utf-8-sig')
            remote = config['onedrive']
            token = json.loads(remote['token'])
        return remote['drive_id'], token['access_token']

    def graph_request(self, tail, method='GET', etag=None):
        with self.lock:
            wait = self.cooldown - time.monotonic()
            if wait > 0:
                raise PreviewError(f'Microsoft asked us to pause. Try again in {int(wait) + 1} seconds.', 429)
            time.sleep(max(0, self.next_request - time.monotonic()))
            drive_id, access_token = self.credentials()
            url = 'https://graph.microsoft.com/v1.0/drives/' + urllib.parse.quote(drive_id, safe='') + '/' + tail
            headers = {'Authorization': 'Bearer ' + access_token, 'Accept': 'application/json'}
            if etag:
                headers['If-Match'] = etag
            request = urllib.request.Request(url, headers=headers, method=method)
            self.next_request = time.monotonic() + 0.6
            try:
                with self.graph_opener.open(request, timeout=25) as response:
                    if method == 'DELETE':
                        if response.status != 204:
                            raise PreviewError('Microsoft returned an unexpected deletion result. Check OneDrive before trying again.')
                        return None
                    return json.load(response)
            except urllib.error.HTTPError as error:
                error.close()
                if error.code in (429, 503):
                    try:
                        delay = max(10, int(error.headers.get('Retry-After', '60')))
                    except ValueError:
                        delay = 60
                    self.cooldown = time.monotonic() + delay
                    raise PreviewError(f'Microsoft asked us to pause. Try again in {delay} seconds.', 429) from None
                messages = {401: 'Microsoft sign-in has expired. Reopen the viewer or reconnect Rclone.', 403: 'OneDrive did not allow this operation.', 404: 'This file has moved or been removed since the scan.', 409: 'OneDrive could not change this file. It may be locked.', 412: 'This file changed after review and was not deleted. Run a fresh scan.'}
                raise PreviewError(messages.get(error.code, 'OneDrive could not complete the request. Try again later.'), error.code if error.code in messages else 502) from None

    def check_folder(self, path):
        path = normalize_folder(path)
        data = self.graph_request('root:/' + urllib.parse.quote(path, safe='/') + '?' + urllib.parse.urlencode({'$select': 'id,folder'}))
        if 'folder' not in data:
            raise ValueError('Choose a folder rather than a file.')

    def folders(self, path):
        path = normalize_folder(path, allow_root=True)
        tail = ('root:/' + urllib.parse.quote(path, safe='/') + ':/children' if path else 'root/children')
        tail += '?' + urllib.parse.urlencode({'$select': 'id,name,folder', '$top': '200'})
        folders = []
        while tail:
            data = self.graph_request(tail)
            for item in data.get('value', []):
                if 'folder' in item:
                    child = (path + '/' if path else '') + item['name']
                    if child.split('/')[0].casefold() != 'personal vault':
                        folders.append({'name': item['name'], 'path': normalize_folder(child)})
            next_link = data.get('@odata.nextLink')
            tail = None
            if next_link:
                parsed = urllib.parse.urlsplit(next_link)
                drive_id, _ = self.credentials()
                expected = '/v1.0/drives/' + urllib.parse.quote(drive_id, safe='') + '/'
                if parsed.scheme != 'https' or parsed.netloc != 'graph.microsoft.com' or not parsed.path.startswith(expected):
                    raise PreviewError('Microsoft returned an unexpected folder listing location.')
                tail = parsed.path[len(expected):] + ('?' + parsed.query if parsed.query else '')
        return {'path': path, 'folders': sorted(folders, key=lambda folder: folder['name'].casefold())}

    def details(self, path, expected_size, expected_hash, force=False):
        with self.lock:
            cached = self.metadata.get((path, expected_size, expected_hash))
            if not force and cached and cached[0] > time.monotonic():
                return cached[1]
            query = urllib.parse.urlencode({'$select': 'id,name,size,eTag,webUrl,image,video,photo,file,lastModifiedDateTime', '$expand': 'thumbnails'})
            data = self.graph_request('root:/' + urllib.parse.quote(path, safe='/') + '?' + query)
            image = data.get('image') or {}
            video = data.get('video') or {}
            photo = data.get('photo') or {}
            file_data = data.get('file') or {}
            actual_hash = (file_data.get('hashes') or {}).get('quickXorHash')
            if actual_hash:
                import base64
                try:
                    actual_hash = base64.b64decode(actual_hash, validate=True).hex()
                except ValueError:
                    actual_hash = None
            thumb_url = None
            thumb_dimensions = None
            for thumb_set in data.get('thumbnails', []):
                thumb = thumb_set.get('large') or thumb_set.get('medium') or thumb_set.get('small')
                if thumb and trusted_thumbnail_url(thumb.get('url', '')):
                    thumb_url = thumb['url']
                    thumb_dimensions = {'width': thumb.get('width'), 'height': thumb.get('height')}
                    break
            web_url = data.get('webUrl', '')
            if not trusted_thumbnail_url(web_url):
                web_url = None
            result = {'size': data.get('size'), 'width': image.get('width') or video.get('width'), 'height': image.get('height') or video.get('height'), 'duration': video.get('duration'), 'bitrate': video.get('bitrate'), 'mime': file_data.get('mimeType'), 'modified': data.get('lastModifiedDateTime'), 'taken': photo.get('takenDateTime'), 'camera': ' '.join(str(photo[key]) for key in ('cameraMake', 'cameraModel') if photo.get(key)), 'webUrl': web_url, 'hasThumbnail': bool(thumb_url), 'thumbnailDimensions': thumb_dimensions, 'changedSinceScan': data.get('size') != expected_size or (actual_hash is not None and actual_hash != expected_hash), '_thumbnailUrl': thumb_url}
            result.update(_itemId=data.get('id'), _etag=data.get('eTag'), _hash=actual_hash, _isFile='file' in data)
            result['canDelete'] = bool(data.get('id') and data.get('eTag') and 'file' in data and actual_hash == expected_hash and data.get('size') == expected_size)
            self.metadata[(path, expected_size, expected_hash)] = (time.monotonic() + 1200, result)
            while len(self.metadata) > 256:
                self.metadata.popitem(last=False)
            return result

    def recycle(self, details):
        if not details.get('_itemId') or not details.get('_etag') or not details.get('_isFile'):
            raise PreviewError('OneDrive did not supply enough information to delete this file safely.', 409)
        self.graph_request('items/' + urllib.parse.quote(details['_itemId'], safe=''), method='DELETE', etag=details['_etag'])

    def thumbnail(self, details):
        url = details.get('_thumbnailUrl')
        if not url or not trusted_thumbnail_url(url):
            raise PreviewError('OneDrive has no thumbnail for this file.', 404)
        with self.lock:
            if url in self.thumbnails:
                self.thumbnails.move_to_end(url)
                return self.thumbnails[url]
            try:
                with self.thumb_opener.open(url, timeout=25) as response:
                    content_type = response.headers.get_content_type()
                    if content_type not in ('image/jpeg', 'image/png', 'image/webp', 'image/gif'):
                        raise PreviewError('OneDrive returned an unsupported preview format.')
                    content = response.read(2 * 1024 * 1024 + 1)
                    if len(content) > 2 * 1024 * 1024:
                        raise PreviewError('This thumbnail exceeds the 2 MB preview limit.')
            except urllib.error.HTTPError as error:
                error.close()
                raise PreviewError('This OneDrive thumbnail is unavailable. Try again later.') from None
            result = (content, content_type)
            self.thumbnails[url] = result
            while len(self.thumbnails) > 16:
                self.thumbnails.popitem(last=False)
            return result


def recycle_selection(report, onedrive, payload):
    if not isinstance(payload, dict) or payload.get('confirmRecycleBin') is not True:
        raise ValueError('Review the selected paths and confirm moving them to the Recycle Bin.')
    data = report.load()
    revision = payload.get('revision')
    if revision != data['revision']:
        raise ValueError('The scan has changed. Reload and review the selected copies again.')
    group_id = payload.get('group')
    if not isinstance(group_id, str):
        raise ValueError('Choose a duplicate group first.')
    group = report.lookup.get(group_id)
    selected_ids = payload.get('files')
    if not group or not isinstance(selected_ids, list) or not selected_ids or any(not isinstance(value, str) for value in selected_ids) or len(set(selected_ids)) != len(selected_ids):
        raise ValueError('Choose the individual copies to move to the Recycle Bin.')
    if not group['sameSize']:
        raise ValueError('The sizes differ. Review these files directly in OneDrive before deleting.')
    selected = [report.file(group_id, value).copy() for value in selected_ids]
    keep = [file.copy() for file in group['files'] if file['id'] not in selected_ids]
    if not keep:
        raise ValueError('Keep at least one copy in this group.')

    def verify(file):
        details = onedrive.details(file['path'], file['size'], group_id, force=True)
        if details.get('changedSinceScan') or details.get('_hash') != group_id or not details.get('_isFile') or not details.get('_etag') or not details.get('_itemId'):
            raise PreviewError('A file changed or could not be verified. Nothing else will be deleted. Run a fresh scan.', 409)
        return details

    kept = verify(keep[0])
    checked = [(file, verify(file)) for file in selected]
    item_ids = [details['_itemId'] for _, details in checked] + [kept['_itemId']]
    if len(set(item_ids)) != len(item_ids):
        raise ValueError('These paths refer to the same OneDrive item. Run a fresh scan before deleting.')
    deleted = []
    error = None
    for file, details in checked:
        try:
            verify(keep[0])
            if report.load()['revision'] != revision:
                raise ValueError('The scan changed. Reload before continuing.')
            onedrive.recycle(details)
            deleted.append({'id': file['id'], 'path': file['path']})
            report.mark_deleted(file['path'], revision)
        except (PreviewError, ValueError) as failure:
            error = str(failure)
            break
        except Exception:
            error = 'The request outcome is uncertain. Check OneDrive before trying again; requests are not retried automatically.'
            break
    return {'deleted': deleted, 'error': error}


class ViewerHandler(BaseHTTPRequestHandler):
    def selected_report(self, view):
        if view == 'all':
            return self.server.report
        if view == 'folder' and self.server.folder_scans.report:
            return self.server.folder_scans.report
        raise ValueError('Run a folder scan first, then choose a result.')

    def log_message(self, *_args):
        pass  # Paths and signed thumbnail URLs must not appear in server logs.

    def reply(self, code, body=b'', content_type='application/json; charset=utf-8', extra=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        headers = {'Content-Type': content_type, 'Content-Length': str(len(body)), 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer', 'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"}
        headers.update(extra or {})
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        origin = f'http://127.0.0.1:{self.server.server_port}'
        if self.headers.get('Host') != origin.removeprefix('http://') or self.headers.get('Origin', origin) != origin:
            self.reply(403, {'error': 'Only the local viewer can access this service.'})
            return False
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
        except Exception:
            self.reply(403, {'error': 'Invalid viewer session.'})
            return False
        session = cookie.get('viewer')
        if not session or not session.value.isascii() or not secrets.compare_digest(session.value, self.server.secret):
            self.reply(401, {'error': 'Open OneDrive Duplicate Viewer using its desktop shortcut.'})
            return False
        return True

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}':
            self.reply(403, {'error': 'Only the local viewer can access this service.'})
            return
        if parsed.path == '/health':
            self.reply(200, {'app': 'onedrive-duplicate-viewer-v1'})
            return
        if parsed.path.startswith('/open/'):
            if secrets.compare_digest(parsed.path.removeprefix('/open/'), self.server.secret):
                self.reply(303, b'', extra={'Location': '/', 'Set-Cookie': f'viewer={self.server.secret}; HttpOnly; SameSite=Strict; Path=/'})
            else:
                self.reply(403, {'error': 'Open the viewer using its desktop shortcut.'})
            return
        if not self.authorized():
            return
        try:
            args = urllib.parse.parse_qs(parsed.query)
            view = args.get('view', ['all'])[0]
            if parsed.path == '/api/groups':
                if view == 'folder':
                    self.reply(200, self.server.folder_scans.results())
                else:
                    self.reply(200, self.selected_report(view).load())
            elif parsed.path == '/api/folders':
                self.reply(200, self.server.onedrive.folders(args.get('path', [''])[0]))
            elif parsed.path == '/api/folder-scan':
                self.reply(200, self.server.folder_scans.status())
            elif parsed.path == '/api/rescan':
                if view not in ('all', 'folder'):
                    raise ValueError('Choose a valid view to refresh.')
                manager = self.server.main_scans if view == 'all' else self.server.folder_scans
                self.reply(200, manager.status())
            elif parsed.path in ('/api/file', '/api/thumbnail'):
                group_id, file_id = args.get('group', [''])[0], args.get('file', [''])[0]
                entry = self.selected_report(view).file(group_id, file_id)
                details = self.server.onedrive.details(entry['path'], entry['size'], group_id)
                if parsed.path == '/api/file':
                    self.reply(200, {key: value for key, value in details.items() if not key.startswith('_')})
                else:
                    content, content_type = self.server.onedrive.thumbnail(details)
                    self.reply(200, content, content_type)
            elif parsed.path in ('/', '/app.mjs', '/model.mjs', '/style.css', '/favicon.svg'):
                name = {'/': 'index.html'}.get(parsed.path, parsed.path[1:])
                types = {'.html': 'text/html', '.mjs': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml'}
                self.reply(200, (HERE / name).read_bytes(), types[Path(name).suffix] + '; charset=utf-8')
            elif parsed.path == '/favicon.ico':
                self.reply(204, b'')
            else:
                self.reply(404, {'error': 'Not found.'})
        except PreviewError as error:
            self.reply(error.status, {'error': str(error)})
        except (ValueError, FileNotFoundError) as error:
            self.reply(400, {'error': str(error) if not isinstance(error, FileNotFoundError) else 'No scan report found. Run Scan OneDrive Duplicates first.'})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.reply(502, {'error': 'Could not read the report or contact OneDrive. Check the connection and try again.'})

    def do_POST(self):
        endpoint = urllib.parse.urlsplit(self.path).path
        if endpoint not in ('/api/delete', '/api/folder-scan', '/api/folder-scan/cancel', '/api/rescan', '/api/rescan/cancel'):
            self.reply(405, {'error': 'This operation is not supported.'})
            return
        if not self.authorized():
            return
        if self.headers.get('Origin') != f'http://127.0.0.1:{self.server.server_port}' or self.headers.get_content_type() != 'application/json':
            self.reply(403, {'error': 'Use the controls in this viewer to perform this operation.'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length <= 0 or length > 65536:
                raise ValueError('Invalid selection size.')
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError('Invalid request.')
            if endpoint == '/api/folder-scan':
                result = self.server.folder_scans.start(payload.get('path'))
            elif endpoint == '/api/folder-scan/cancel':
                result = self.server.folder_scans.cancel()
            elif endpoint in ('/api/rescan', '/api/rescan/cancel'):
                view = payload.get('view', 'all')
                if view not in ('all', 'folder'):
                    raise ValueError('Choose a valid view to refresh.')
                manager = self.server.main_scans if view == 'all' else self.server.folder_scans
                if endpoint.endswith('/cancel'):
                    result = manager.cancel()
                else:
                    path = '' if view == 'all' else payload.get('scope', self.server.folder_scans.scope)
                    result = manager.request_refresh(path)
            else:
                with self.server.deletion_lock:
                    report = self.selected_report(payload.get('view', 'all'))
                    result = recycle_selection(report, self.server.onedrive, payload)
                    for other in (self.server.report, self.server.folder_scans.report):
                        if other is not None and other is not report:
                            try:
                                snapshot = other.load()
                                known = {file['path'] for group in snapshot['groups'] for file in group['files']}
                                for deleted in result['deleted']:
                                    if deleted['path'] in known:
                                        other.mark_deleted(deleted['path'], snapshot['revision'])
                            except (OSError, ValueError):
                                result['error'] = (result['error'] or '') + ' Run a fresh scan to update the other view.'
            self.reply(200, result)
        except PreviewError as error:
            self.reply(error.status, {'error': str(error)})
        except ValueError as error:
            self.reply(400, {'error': str(error)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.reply(502, {'error': 'Could not verify the selected files. Check OneDrive before trying again.'})

    def do_DELETE(self):
        self.reply(405, {'error': 'Use the selection and confirmation in the viewer.'})

    do_PUT = do_DELETE
    do_PATCH = do_DELETE


def make_server(port=0, report=None, onedrive=None):
    server = ThreadingHTTPServer(('127.0.0.1', port), ViewerHandler)
    server.daemon_threads = True
    server.secret = secrets.token_urlsafe(32)
    server.report = Report(report or APP_DIR / 'last-scan.txt')
    server.onedrive = onedrive or OneDrive()
    server.deletion_lock = threading.Lock()
    server.folder_scans = FolderScans(server.onedrive, server.report.path.parent / 'folder-scans', Report)
    server.main_scans = FolderScans(server.onedrive, server.report.path.parent, Report, main_report=server.report)
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--open-browser', action='store_true')
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    runtime_file = APP_DIR / 'viewer-runtime.json'
    try:
        existing = json.loads(runtime_file.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        existing = {}
    if args.open_browser and existing:
        try:
            url = f"http://127.0.0.1:{int(existing['port'])}"
            request = urllib.request.Request(url + '/api/groups', headers={'Cookie': 'viewer=' + existing['secret']})
            with urllib.request.urlopen(request, timeout=2) as response:
                if response.status == 200:
                    webbrowser.open(url + '/open/' + existing['secret'])
                    return
        except Exception:
            pass
    server = make_server(args.port)
    if existing.get('port') == server.server_port and isinstance(existing.get('secret'), str) and existing['secret'].isascii() and len(existing['secret']) >= 32:
        server.secret = existing['secret']
    runtime_file.write_text(json.dumps({'port': server.server_port, 'secret': server.secret}), encoding='utf-8')
    url = f'http://127.0.0.1:{server.server_port}'
    if args.open_browser:
        webbrowser.open(url + '/open/' + server.secret)
    print('Local viewer is ready at ' + url, flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        try:
            if json.loads(runtime_file.read_text())['secret'] == server.secret:
                runtime_file.unlink()
        except (OSError, ValueError, KeyError):
            pass


if __name__ == '__main__':
    main()
