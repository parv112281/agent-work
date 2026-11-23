"""A local Jira REST v2 stand-in for tests. Configure behaviour through server attributes."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import urllib.parse

TOKEN = 'secret'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        server = self.server
        url = urllib.parse.urlparse(self.path)
        server.requests.append(url.path)
        if server.network_file and Path(server.network_file).read_text() != 'Connected':
            return self.reply(503, b'network unavailable')
        if self.headers.get('Authorization') != f'Bearer {TOKEN}':
            return self.reply(401, b'unauthorized')
        base = f'http://{self.headers["Host"]}'
        parts = url.path.removeprefix('/rest/api/2/').split('/')
        if parts == ['field']:
            return self.json([{'id': 'customfield_9', 'name': 'Acceptance Criteria'}])
        if parts[0] == 'issue' and len(parts) >= 2:
            key = parts[1]
            if key in server.fail or key not in server.issues:
                return self.reply(404, b'{"errorMessages":["Issue does not exist"]}')
            issue = server.issues[key]
            if parts[2:] == ['comment']:
                start = int(urllib.parse.parse_qs(url.query)['startAt'][0])
                comments = issue.get('comments', [])
                page = comments[start:start + 1]
                return self.json({'comments': [{'author': {'name': 'bob'}, 'body': c} for c in page],
                                  'total': len(comments), 'maxResults': 1})
            server.requested_fields = urllib.parse.parse_qs(url.query).get('fields', [''])[0]
            attachments = [{'filename': name, 'content': f'{base}/files/{key}/{name}', 'size': declared,
                            'mimeType': 'text/markdown', 'author': {'displayName': 'Ann'}}
                           for name, (_, declared) in issue.get('attachments', {}).items()]
            return self.json({'key': key, 'fields': {
                'summary': issue.get('summary', 'Title'), 'description': issue.get('description', 'Details'),
                'customfield_9': issue.get('acceptance', ''), 'attachment': attachments}})
        if url.path.startswith('/files/'):
            _, _, key, name = url.path.split('/', 3)
            content, _ = server.issues[key]['attachments'][urllib.parse.unquote(name)]
            return self.reply(200, content)
        return self.reply(404, b'not found')

    def json(self, value):
        self.reply(200, json.dumps(value).encode(), 'application/json')

    def reply(self, status, body, content_type='text/plain'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start(test_case, network_file=None):
    """Start a server for one test; issues maps key -> {summary, description, comments, attachments}."""
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.issues, server.fail, server.requests = {}, set(), []
    server.network_file, server.requested_fields = network_file, ''
    server.url = f'http://127.0.0.1:{server.server_port}/rest/api/2/'
    threading.Thread(target=server.serve_forever, daemon=True).start()
    test_case.addCleanup(server.server_close)
    test_case.addCleanup(server.shutdown)
    return server
