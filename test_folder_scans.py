import json
from pathlib import Path
import tempfile
import threading
import unittest

from folder_scans import FolderScans, normalize_folder
from server import OneDrive, PreviewError, Report, parse_report

SAMPLE = ('abc123: 2 duplicates\n'
          '  1:      4096 bytes, 2026-01-01 12:00:00.000000000, original.jpg\n'
          '  2:      4096 bytes, 2026-01-02 12:00:00.000000000, Child/copy.jpg\n')


class FolderTests(unittest.TestCase):
    def test_folder_paths_cannot_escape_scope_or_target_vault(self):
        self.assertEqual(normalize_folder('Photos/旅行 & family'), 'Photos/旅行 & family')
        self.assertEqual(normalize_folder('', allow_root=True), '')
        for path in ('', '/Photos', '../Photos', 'Photos/../Other', 'Photos//Other', 'C:\\Photos', 'onedrive:Other', 'Personal Vault/Photos', 'Photos\nOther', None):
            with self.assertRaises(ValueError):
                normalize_folder(path)

    def test_scoped_report_keeps_full_paths_and_stable_file_identity(self):
        scoped = parse_report(SAMPLE, prefix='Family/旅行')[0]
        self.assertEqual([file['path'] for file in scoped['files']], ['Family/旅行/original.jpg', 'Family/旅行/Child/copy.jpg'])
        full = SAMPLE.replace('original.jpg', 'Family/旅行/original.jpg').replace('Child/copy.jpg', 'Family/旅行/Child/copy.jpg')
        self.assertEqual(scoped['files'][0]['id'], parse_report(full)[0]['files'][0]['id'])
        for relative in ('../other.jpg', '/outside.jpg', 'Child/../../outside.jpg'):
            with self.assertRaises(ValueError):
                parse_report(SAMPLE.replace('original.jpg', relative), prefix='Family')

    def test_folder_picker_paginates_and_omits_files_and_personal_vault(self):
        drive = OneDrive()
        drive.credentials = lambda: ('drive', 'token')
        calls = []
        pages = [{'value': [{'name': 'Photos', 'folder': {}}, {'name': 'note.txt', 'file': {}}, {'name': 'Personal Vault', 'folder': {}}], '@odata.nextLink': 'https://graph.microsoft.com/v1.0/drives/drive/root/children?$skiptoken=next'}, {'value': [{'name': 'Archive', 'folder': {}}]}]
        drive.graph_request = lambda tail: (calls.append(tail), pages.pop(0))[1]
        self.assertEqual([folder['path'] for folder in drive.folders('')['folders']], ['Archive', 'Photos'])
        self.assertEqual(len(calls), 2)
        self.assertIn('$skiptoken=next', calls[1])

    def test_folder_picker_rejects_foreign_pagination_location(self):
        drive = OneDrive()
        drive.credentials = lambda: ('drive', 'token')
        drive.graph_request = lambda tail: {'value': [], '@odata.nextLink': 'https://example.com/next'}
        with self.assertRaises(PreviewError):
            drive.folders('Photos')


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.main = self.directory / 'main.txt'
        self.main.write_text('Existing main report', encoding='utf-8')

        class Drive:
            def __init__(self):
                self.checked = []

            def check_folder(self, path):
                self.checked.append(path)

        self.drive = Drive()
        self.commands = []
        self.exit_code = 0

        def popen(command, stdout, stderr, **kwargs):
            self.commands.append(command)
            self.assertNotIn('shell', kwargs)
            stdout.write(SAMPLE.encode('utf-8'))

            class Process:
                def wait(process):
                    return self.exit_code

            return Process()

        self.scans = FolderScans(self.drive, self.directory / 'scans', Report, popen=popen)

    def tearDown(self):
        self.temporary.cleanup()

    def finish(self):
        self.scans.thread.join(timeout=4)
        self.assertFalse(self.scans.thread.is_alive())

    def test_real_scoped_command_and_results_are_separate_from_main_report(self):
        self.scans.start('Photos/旅行')
        self.finish()
        self.assertEqual(self.scans.status()['status'], 'completed')
        command = self.commands[0]
        self.assertEqual(command[-1], 'onedrive:Photos/旅行')
        for flag in ('--dry-run', '--onedrive-delta=false', '--fast-list=false'):
            self.assertIn(flag, command)
        self.assertEqual(command[command.index('--dedupe-mode') + 1], 'list')
        self.assertEqual(self.main.read_text(encoding='utf-8'), 'Existing main report')
        results = self.scans.results()
        self.assertEqual(results['scope'], 'Photos/旅行')
        self.assertEqual(len(results['groups']), 1)
        self.assertEqual(results['groups'][0]['files'][1]['path'], 'Photos/旅行/Child/copy.jpg')
        reopened = FolderScans(self.drive, self.directory / 'scans', Report)
        self.assertEqual(reopened.results()['scope'], 'Photos/旅行')

    def test_failed_scan_preserves_previous_completed_results(self):
        self.scans.start('Photos')
        self.finish()
        before = self.scans.results()
        self.exit_code = 1
        self.scans.start('Other')
        self.finish()
        self.assertEqual(self.scans.status()['status'], 'failed')
        self.assertEqual(self.scans.results(), before)

    def test_full_drive_refresh_uses_root_delta_and_atomically_updates_main_report(self):
        self.main.write_text(SAMPLE, encoding='utf-8')
        main_report = Report(self.main)
        full = FolderScans(self.drive, self.directory, Report, popen=self.scans.popen, main_report=main_report)
        full.request_refresh('')
        full.thread.join(timeout=4)
        self.assertFalse(full.thread.is_alive())
        self.assertEqual(full.status()['status'], 'completed')
        command = self.commands[-1]
        self.assertEqual(command[-1], 'onedrive:')
        self.assertIn('--onedrive-delta', command)
        self.assertIn('--fast-list', command)
        self.assertIn('/Personal Vault/**', command)
        self.assertEqual(self.drive.checked, [])
        self.assertEqual(main_report.load()['groups'][0]['files'][0]['path'], 'original.jpg')

    def test_refresh_requested_during_scan_runs_again_after_the_first_scan(self):
        started = threading.Event()
        release = threading.Event()
        calls = []

        def popen(command, stdout, stderr, **kwargs):
            number = len(calls)
            calls.append(command)
            stdout.write(SAMPLE.encode('utf-8') if number == 0 else b'')

            class Process:
                def wait(process):
                    if number == 0:
                        started.set()
                        release.wait(timeout=4)
                    return 0

            return Process()

        self.scans.popen = popen
        self.scans.start('Photos')
        self.assertTrue(started.wait(timeout=2))
        queued = self.scans.request_refresh('Photos')
        self.assertEqual(queued['status'], 'running')
        self.assertIn('queued', self.scans.status()['message'])
        release.set()
        self.finish()
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.scans.status()['status'], 'completed')
        self.assertEqual(self.scans.results()['groups'], [])

    def test_failed_full_refresh_keeps_the_last_saved_report(self):
        self.main.write_text(SAMPLE, encoding='utf-8')
        self.exit_code = 1
        full = FolderScans(self.drive, self.directory, Report, popen=self.scans.popen, main_report=Report(self.main))
        full.request_refresh('')
        full.thread.join(timeout=4)
        self.assertEqual(full.status()['status'], 'failed')
        self.assertEqual(self.main.read_text(encoding='utf-8'), SAMPLE)

    def test_cancel_stops_only_this_scan_and_publishes_no_partial_results(self):
        started = threading.Event()
        stopped = threading.Event()

        class Process:
            def wait(process):
                started.set()
                stopped.wait(timeout=4)
                return -15

            def poll(process):
                return None

            def terminate(process):
                stopped.set()

        self.scans.popen = lambda *args, **kwargs: Process()
        self.scans.start('Photos')
        self.assertTrue(started.wait(timeout=2))
        with self.assertRaises(ValueError):
            self.scans.start('Other')
        self.scans.cancel()
        self.finish()
        self.assertTrue(stopped.is_set())
        self.assertEqual(self.scans.status()['status'], 'cancelled')
        self.assertIsNone(self.scans.results()['scope'])
        self.assertEqual(self.main.read_text(encoding='utf-8'), 'Existing main report')


if __name__ == '__main__':
    unittest.main()
