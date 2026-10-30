import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from server import make_server, OneDrive, parse_report, PreviewError, Report, recycle_selection, trusted_thumbnail_url

SAMPLE = ('abc123: 2 duplicates\n'
          '  1:      4096 bytes, 2026-01-01 12:00:00.000000000, Photos/旅行 #1, original.jpg\n'
          '  2:      4096 bytes, 2026-01-02 12:00:00.000000000, Copies/photo.jpg\n')


class ReportTests(unittest.TestCase):
    def test_names_sizes_and_counts(self):
        group = parse_report(SAMPLE)[0]
        self.assertEqual(group['files'][0]['path'], 'Photos/旅行 #1, original.jpg')
        self.assertEqual(group['files'][0]['type'], 'image')
        self.assertEqual(group['savings'], 4096)
        self.assertTrue(group['sameSize'])

    def test_incomplete_or_failed_reports_are_rejected(self):
        for report in (SAMPLE.rsplit('  2:', 1)[0], SAMPLE + '2026/10/27 08:10:23 ERROR : unavailable folder\n', 'unexpected data'):
            with self.assertRaises(ValueError):
                parse_report(report)

    def test_different_sizes_not_presented_as_same(self):
        group = parse_report(SAMPLE.replace('  2:      4096', '  2:      2048'))[0]
        self.assertFalse(group['sameSize'])
        self.assertEqual(group['savings'], 2048)

    def test_only_microsoft_https_thumbnail_locations(self):
        self.assertTrue(trusted_thumbnail_url('https://southcentralus1-mediap.svc.ms/preview?signature=example'))
        for url in ('http://x.svc.ms/preview', 'https://svc.ms.attacker.com/x', 'https://127.0.0.1/x', 'https://svc.ms@attacker.com/x', 'https://svc.ms:8080/x'):
            self.assertFalse(trusted_thumbnail_url(url))


class GraphTests(unittest.TestCase):
    def test_delete_uses_item_id_and_if_match_and_never_permanent_delete(self):
        graph = OneDrive()
        graph.credentials = lambda: ('example-drive', 'example-token')
        received = []

        class Opener:
            def open(self, request, timeout):
                received.append(request)
                response = io.BytesIO()
                response.status = 204
                return response

        graph.graph_opener = Opener()
        graph.recycle({'_itemId': 'item#1', '_etag': '"version-3"', '_isFile': True})
        self.assertEqual(received[0].get_method(), 'DELETE')
        self.assertTrue(received[0].full_url.endswith('/items/item%231'))
        self.assertEqual(received[0].get_header('If-match'), '"version-3"')
        self.assertNotIn('permanentDelete', received[0].full_url)

    def test_changed_etag_is_not_retried(self):
        graph = OneDrive()
        graph.credentials = lambda: ('example-drive', 'example-token')
        calls = []

        class Opener:
            def open(self, request, timeout):
                calls.append(request)
                raise urllib.error.HTTPError(request.full_url, 412, 'Precondition Failed', {}, io.BytesIO())

        graph.graph_opener = Opener()
        with self.assertRaises(PreviewError) as raised:
            graph.recycle({'_itemId': 'item-1', '_etag': '"old-version"', '_isFile': True})
        self.assertEqual(raised.exception.status, 412)
        self.assertEqual(len(calls), 1)

    def test_metadata_request_never_fetches_original_and_is_cached(self):
        graph = OneDrive()
        graph.credentials = lambda: ('example-drive', 'never-expose-this')
        received = []

        class Opener:
            def open(self, request, timeout):
                received.append(request)
                return io.BytesIO(json.dumps({'size': 4096, 'image': {'width': 4032, 'height': 3024}, 'file': {'hashes': {'quickXorHash': 'YWJj'}}, 'thumbnails': [{'large': {'url': 'https://example.svc.ms/thumbnail', 'width': 800, 'height': 600}}]}).encode())

        graph.graph_opener = Opener()
        one = graph.details('Photos/旅行 #1.jpg', 4096, '616263')
        two = graph.details('Photos/旅行 #1.jpg', 4096, '616263')
        self.assertIs(one, two)
        self.assertEqual(len(received), 1)
        self.assertIn('%23', received[0].full_url)
        self.assertNotIn('/content', received[0].full_url)
        self.assertNotIn('@microsoft.graph.downloadUrl', received[0].full_url)
        self.assertEqual(one['width'], 4032)
        self.assertFalse(one['changedSinceScan'])
        self.assertEqual(received[0].get_method(), 'GET')

    def test_throttle_stops_following_requests(self):
        graph = OneDrive()
        graph.credentials = lambda: ('example-drive', 'example-token')
        calls = []

        class Opener:
            def open(self, request, timeout):
                calls.append(request)
                raise urllib.error.HTTPError(request.full_url, 429, 'Too Many Requests', {'Retry-After': '60'}, io.BytesIO())

        graph.graph_opener = Opener()
        for name in ('a.jpg', 'b.jpg'):
            with self.assertRaises(PreviewError) as raised:
                graph.details(name, 4096, '616263')
            self.assertEqual(raised.exception.status, 429)
        self.assertEqual(len(calls), 1)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        report = Path(self.folder.name) / 'report.txt'
        report.write_text(SAMPLE, encoding='utf-8')
        self.server = make_server(report=report)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.folder.cleanup()

    def request(self, path, authenticated=True, method='GET', headers=None):
        request_headers = {'Cookie': 'viewer=' + self.server.secret} if authenticated else {}
        request_headers.update(headers or {})
        request = urllib.request.Request(self.url + path, method=method, headers=request_headers)
        try:
            return urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as error:
            return error

    def test_requires_local_session_and_origin(self):
        with self.request('/api/groups', authenticated=False) as response:
            self.assertEqual(response.status, 401)
        with self.request('/api/groups', headers={'Origin': 'https://example.com'}) as response:
            self.assertEqual(response.status, 403)
        with self.request('/api/groups', headers={'Host': 'example.com'}) as response:
            self.assertEqual(response.status, 403)
        with self.request('/api/groups') as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(len(json.load(response)['groups']), 1)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_rejects_mutations_unknown_files_and_arbitrary_paths(self):
        for method in ('POST', 'DELETE', 'PUT', 'PATCH'):
            with self.request('/api/file', method=method) as response:
                self.assertEqual(response.status, 405)
        with self.request('/api/file?group=abc123&index=100') as response:
            self.assertEqual(response.status, 400)
        with self.request('/../server.py') as response:
            self.assertEqual(response.status, 404)

    def test_no_graph_token_or_signed_thumbnail_url_reaches_client(self):
        fake = {'size': 4096, 'hasThumbnail': True, '_thumbnailUrl': 'https://example.svc.ms/signed-secret-url'}
        with patch.object(self.server.onedrive, 'details', return_value=fake):
            file_id = self.server.report.load()['groups'][0]['files'][0]['id']
            with self.request('/api/file?group=abc123&file=' + file_id) as response:
                data = json.load(response)
                self.assertNotIn('_thumbnailUrl', data)
                self.assertEqual(data['size'], 4096)

    def test_deletion_requires_session_and_same_origin_json_confirmation(self):
        request = urllib.request.Request(self.url + '/api/delete', data=b'{}', headers={'Content-Type': 'application/json'}, method='POST')
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(raised.exception.code, 401)
        raised.exception.close()
        request.add_header('Cookie', 'viewer=' + self.server.secret)
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()
        request.add_header('Origin', self.url)
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()

    def test_folder_results_preview_paths_and_deletion_stay_in_selected_scope(self):
        scoped_path = Path(self.folder.name) / 'scoped' / 'report.txt'
        scoped_path.parent.mkdir()
        scoped_path.write_text(SAMPLE, encoding='utf-8')
        self.server.folder_scans.report = Report(scoped_path, prefix='Selected folder')
        self.server.folder_scans.scope = 'Selected folder'
        with self.request('/api/groups?view=folder') as response:
            scoped = json.load(response)
        file = scoped['groups'][0]['files'][0]
        with patch.object(self.server.onedrive, 'details', return_value={'size': 4096}) as details:
            with self.request('/api/file?view=folder&group=abc123&file=' + file['id']) as response:
                self.assertEqual(response.status, 200)
            self.assertEqual(details.call_args.args[0], 'Selected folder/' + parse_report(SAMPLE)[0]['files'][0]['path'])
            with self.request('/api/file?view=all&group=abc123&file=' + file['id']) as response:
                self.assertEqual(response.status, 400)
        payload = {'view': 'folder', 'group': 'abc123', 'files': [file['id']], 'revision': scoped['revision'], 'confirmRecycleBin': True}

        def metadata(path, size, expected_hash, force=False):
            return {'size': size, '_hash': expected_hash, '_itemId': path, '_etag': 'v1', '_isFile': True}

        with patch.object(self.server.onedrive, 'details', side_effect=metadata), patch.object(self.server.onedrive, 'recycle') as recycle:
            request = urllib.request.Request(self.url + '/api/delete', data=json.dumps(payload).encode(), headers={'Cookie': 'viewer=' + self.server.secret, 'Origin': self.url, 'Content-Type': 'application/json'}, method='POST')
            with urllib.request.urlopen(request, timeout=3) as response:
                result = json.load(response)
            self.assertEqual(result['deleted'][0]['path'], file['path'])
            self.assertEqual(recycle.call_args.args[0]['_itemId'], file['path'])
        self.assertEqual(len(self.server.report.load()['groups']), 1)
        self.assertEqual(self.server.folder_scans.results()['groups'], [])

    def test_rescan_targets_the_deleted_report_scope_in_each_view(self):
        for view, scope, target, expected in [('all', None, self.server.main_scans, ''), ('folder', 'Selected/Folder', self.server.folder_scans, 'Selected/Folder')]:
            with patch.object(target, 'request_refresh', return_value={'status': 'running', 'path': expected}) as refresh:
                payload = json.dumps({'view': view, 'scope': scope}).encode()
                request = urllib.request.Request(self.url + '/api/rescan', data=payload, headers={'Cookie': 'viewer=' + self.server.secret, 'Origin': self.url, 'Content-Type': 'application/json'}, method='POST')
                with urllib.request.urlopen(request, timeout=3) as response:
                    self.assertEqual(json.load(response)['status'], 'running')
                refresh.assert_called_once_with(expected)


class DeletionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / 'report.txt'
        self.path.write_text(SAMPLE, encoding='utf-8')
        self.report = Report(self.path)
        self.data = self.report.load()
        self.files = self.data['groups'][0]['files']

        class FakeDrive:
            def __init__(self):
                self.deleted = []
                self.verifications = []
                self.changed = None
                self.failure = None

            def details(self, path, expected_size, expected_hash, force=False):
                self.verifications.append((path, force))
                return {'size': expected_size, '_hash': expected_hash, '_isFile': True, '_etag': 'version-1', '_itemId': path, 'changedSinceScan': self.changed == path}

            def recycle(self, details):
                if self.failure == details['_itemId']:
                    raise PreviewError('Simulated failure', 412)
                self.deleted.append(details['_itemId'])

        self.drive = FakeDrive()

    def tearDown(self):
        self.folder.cleanup()

    def payload(self, ids):
        return {'group': 'abc123', 'files': ids, 'revision': self.data['revision'], 'confirmRecycleBin': True}

    def test_cannot_delete_all_copies_unknown_files_or_duplicate_selections(self):
        for ids in ([file['id'] for file in self.files], ['unknown-id'], [self.files[0]['id']] * 2, []):
            with self.assertRaises(ValueError):
                recycle_selection(self.report, self.drive, self.payload(ids))
        self.assertEqual(self.drive.deleted, [])

    def test_checks_selected_file_and_kept_file_before_any_deletion(self):
        for file in self.files:
            self.drive.changed = file['path']
            with self.assertRaises(PreviewError):
                recycle_selection(self.report, self.drive, self.payload([self.files[0]['id']]))
        self.assertEqual(self.drive.deleted, [])
        self.assertTrue(all(force for _, force in self.drive.verifications))

    def test_success_removes_only_selected_copy_and_persists_report_view(self):
        result = recycle_selection(self.report, self.drive, self.payload([self.files[0]['id']]))
        self.assertEqual(self.drive.deleted, [self.files[0]['path']])
        self.assertEqual(len(result['deleted']), 1)
        self.assertIsNone(result['error'])
        self.assertEqual(Report(self.path).load()['groups'], [])
        self.assertIn(self.files[1]['path'], self.path.read_text(encoding='utf-8'))
        self.path.write_text(SAMPLE + '2026/10/27 10:00:00 NOTICE: fresh scan\n', encoding='utf-8')
        self.assertEqual(len(Report(self.path).load()['groups']), 1)

    def test_partial_failure_stops_and_tracks_only_confirmed_deletions(self):
        third = '  3:      4096 bytes, 2026-01-03 12:00:00.000000000, Copies/third.jpg\n'
        self.path.write_text(SAMPLE.replace('2 duplicates', '3 duplicates') + third, encoding='utf-8')
        self.data = self.report.load()
        files = self.data['groups'][0]['files']
        self.drive.failure = files[1]['path']
        result = recycle_selection(self.report, self.drive, self.payload([files[0]['id'], files[1]['id']]))
        self.assertEqual(self.drive.deleted, [files[0]['path']])
        self.assertEqual(len(result['deleted']), 1)
        self.assertIn('Simulated failure', result['error'])
        remaining = self.report.load()['groups'][0]['files']
        self.assertEqual([file['id'] for file in remaining], [files[1]['id'], files[2]['id']])

    def test_stale_report_and_unconfirmed_request_do_nothing(self):
        payload = self.payload([self.files[0]['id']])
        payload['confirmRecycleBin'] = False
        with self.assertRaises(ValueError):
            recycle_selection(self.report, self.drive, payload)
        payload['confirmRecycleBin'] = True
        payload['revision'] = 'outdated'
        with self.assertRaises(ValueError):
            recycle_selection(self.report, self.drive, payload)
        self.assertEqual(self.drive.deleted, [])


if __name__ == '__main__':
    unittest.main()
