import io
from unittest import mock
import zipfile

import requests

from APITaxi2.commands import towns


def test_download_zipfile_uses_configured_timeout(app, tmp_path):
    app.config['DOWNLOAD_HTTP_CONNECT_TIMEOUT'] = 7.0
    app.config['DOWNLOAD_HTTP_READ_TIMEOUT'] = 60.0

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w') as archive:
        archive.writestr('example.txt', 'ok')

    def requests_get(url, timeout=None):
        assert url == 'https://example.test/contours.zip'
        assert timeout == (7.0, 60.0)
        response = requests.Response()
        response.status_code = 200
        response._content = zip_buffer.getvalue()
        return response

    download_path = tmp_path / 'download'
    with mock.patch('requests.get', requests_get):
        towns.download_zipfile('https://example.test/contours.zip', download_path)

    assert (download_path / 'example.txt').read_text() == 'ok'
