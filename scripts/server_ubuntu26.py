#!/usr/bin/env python3
"""
Remote Command Server - Ubuntu 26.04 Compatible
Expects: POST /run with JSON body {"command": "..."}
Returns: JSON with {"stdout": "...", "stderr": "...", "exit_code": N}
Optional "timeout" in the body: seconds, default 30, at most 120.

POST /github with {"type", "query", "limit"} searches GitHub (PS5LM's
github_search tool): type is repositories, code, issues, users or commits,
limit 1 to 10 (default 5). Returns {"text", "total_count"}: the results trimmed
to what a model needs, or {"error"}. Reads only; no command runs.

Environment:
- AI_BRIDGE_TOKEN: if set, a POST must carry the same value in X-Command-Token
  (PS5LM's remote_command_token setting), or it is refused with 403. Unset, the
  token is read from ~/.config/ai_bridge/token if that file exists.
- AI_BRIDGE_BIND: the address to listen on (default 0.0.0.0, every interface).
- GITHUB_TOKEN: a GitHub token for /github, read from ~/.config/ai_bridge/github_token
  if unset. Code search needs one; the other searches work without it, 10 a
  minute instead of 30. A token with no scopes is enough for public repositories.

Single instance management:
- Uses PID file to detect running instances
- Kills old instance before starting
- Handles SIGTERM/SIGINT for graceful shutdown

"""
from http.server import HTTPServer, BaseHTTPRequestHandler
import subprocess
import json
import os
import signal
import sys
import threading
import time
import hmac
import http.client
import urllib.error
import urllib.parse
import urllib.request

PORT = 8080
TOKEN_FILE = os.path.expanduser('~/.config/ai_bridge/token')


def load_token():
    token = os.environ.get('AI_BRIDGE_TOKEN', '')
    if not token and os.path.isfile(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            token = f.read().strip()
    return token


TOKEN = load_token()
GITHUB_TOKEN_FILE = os.path.expanduser('~/.config/ai_bridge/github_token')
GITHUB_TYPES = ('repositories', 'code', 'issues', 'users', 'commits')
GITHUB_MAX_LIMIT = 10


def load_github_token():
    token = os.environ.get('GITHUB_TOKEN', '')
    if not token and os.path.isfile(GITHUB_TOKEN_FILE):
        with open(GITHUB_TOKEN_FILE) as f:
            token = f.read().strip()
    return token


def clip(text, n):
    text = ' '.join((text or '').split())
    return text if len(text) <= n else text[:n - 3] + '...'


def format_github(kind, items):
    """One short block per result: what a model needs, without the API's hundred fields."""
    out = []
    for i, it in enumerate(items, 1):
        if kind == 'repositories':
            lines = [f"{i}. {it['full_name']} ({it.get('stargazers_count', 0)} stars, "
                     f"{it.get('language') or 'no language'}, updated {(it.get('pushed_at') or '')[:10]})",
                     f"   {it['html_url']}"]
            if it.get('description'):
                lines.append('   ' + clip(it['description'], 200))
        elif kind == 'code':
            lines = [f"{i}. {it['repository']['full_name']}: {it['path']}", f"   {it['html_url']}"]
            for m in (it.get('text_matches') or [])[:2]:
                lines.append('   > ' + clip(m.get('fragment'), 240))
        elif kind == 'issues':
            repo = it.get('repository_url', '').rsplit('/repos/', 1)[-1]
            what = 'PR' if 'pull_request' in it else 'issue'
            lines = [f"{i}. {repo}#{it['number']} [{what}, {it['state']}, {it.get('comments', 0)} comments, "
                     f"updated {(it.get('updated_at') or '')[:10]}] {clip(it['title'], 160)}",
                     f"   {it['html_url']}"]
            if it.get('body'):
                lines.append('   ' + clip(it['body'], 240))
        elif kind == 'users':
            lines = [f"{i}. {it['login']} ({it.get('type', 'User')})", f"   {it['html_url']}"]
        else:  # commits
            c = it.get('commit', {})
            lines = [f"{i}. {it['repository']['full_name']} {it['sha'][:7]} "
                     f"({(c.get('author') or {}).get('name', '?')}, {((c.get('author') or {}).get('date') or '')[:10]}) "
                     f"{clip((c.get('message') or '').splitlines()[0] if c.get('message') else '', 160)}",
                     f"   {it['html_url']}"]
        out.append('\n'.join(lines))
    return '\n'.join(out)


def github_search(data):
    """Returns (HTTP status, reply) for POST /github."""
    kind = data.get('type') or 'repositories'
    query = (data.get('query') or '').strip()
    if kind not in GITHUB_TYPES:
        return 400, {'error': f"type must be one of {', '.join(GITHUB_TYPES)}, not {kind}"}
    if not query:
        return 400, {'error': 'query is empty'}
    try:
        limit = min(max(int(data.get('limit', 5)), 1), GITHUB_MAX_LIMIT)
    except (TypeError, ValueError):
        limit = 5
    url = (f"https://api.github.com/search/{kind}?"
           + urllib.parse.urlencode({'q': query, 'per_page': limit}))
    headers = {
        # text-match: the lines that matched, for code search
        'Accept': 'application/vnd.github.text-match+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'ai_bridge',
    }
    token = load_github_token()
    if token:
        headers['Authorization'] = f'Bearer {token}'
    elif kind == 'code':
        return 400, {'error': 'code search needs a GitHub token: set GITHUB_TOKEN or write one to '
                              + GITHUB_TOKEN_FILE}
    try:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as r:
                reply = json.load(r)
        except urllib.error.HTTPError:
            raise
        except (OSError, http.client.HTTPException):
            # a dropped connection: once more before giving up
            time.sleep(1)
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as r:
                reply = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            message = json.load(e).get('message', '')
        except Exception:
            message = ''
        if e.code in (403, 429) and e.headers.get('X-RateLimit-Remaining') == '0':
            reset = int(e.headers.get('X-RateLimit-Reset', '0'))
            message = f"rate limit reached; it resets in {max(reset - int(time.time()), 0)} s"
        return 502, {'error': f"GitHub answered HTTP {e.code}: {message or e.reason}"}
    except (OSError, http.client.HTTPException, ValueError) as e:
        return 502, {'error': f"cannot reach GitHub: {getattr(e, 'reason', None) or e}"}
    items = reply.get('items', [])
    total = reply.get('total_count', 0)
    head = f"{total} results for {kind} matching {query!r}"
    head += f", showing {len(items)}" if items else ''
    if reply.get('incomplete_results'):
        head += ' (GitHub timed out; the count may be low)'
    text = head + ('\n' + format_github(kind, items) if items else '') + '\n'
    return 200, {'text': text, 'total_count': total}
BIND = os.environ.get('AI_BRIDGE_BIND', '0.0.0.0')
DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 120
PID_FILE = '/tmp/server.pid'

# Global flag for graceful shutdown

running = True

def signal_handler(sig, frame):
    """Handle shutdown signals"""
    global running, server
    print(f"\nReceived signal {sig}, shutting down...")
    running = False
    # The handler runs in the thread inside serve_forever(); shutdown() waits for
    # that loop to end, so calling it here waited forever. Another thread can.
    threading.Thread(target=server.shutdown, daemon=True).start()

def check_and_kill_old_instance():
    """Check if another instance is running and kill it"""
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, 'r') as f:
                old_pid = int(f.read().strip())
            
            # Check if the process is still running
            if old_pid > 0:
                try:
                    os.kill(old_pid, 0)  # Signal 0 checks if process exists
                    print(f"Old instance found (PID {old_pid}), killing...")
                    
                    # Send SIGTERM for graceful shutdown

                    os.kill(old_pid, signal.SIGTERM)
                    
                    # Wait up to 5 seconds for it to exit
                    for _ in range(50):  # 50 * 100ms = 5 seconds
                        time.sleep(0.1)
                        try:
                            os.kill(old_pid, 0)
                        except ProcessLookupError:
                            # Process has exited
                            print(f"Old instance (PID {old_pid}) exited.")
                            break
                    else:
                        # Still running, force kill
                        print(f"Old instance (PID {old_pid}) still running, force killing...")
                        os.kill(old_pid, signal.SIGKILL)
                        time.sleep(0.1)
                except PermissionError:
                    print(f"Cannot kill old instance (PID {old_pid}): permission denied")
                    sys.exit(1)
        except (ValueError, IOError) as e:
            print(f"Error reading PID file: {e}")
    
    # Write our PID to the file
    with open(PID_FILE, 'w') as f:
        f.write(str(os.getpid()))
    print(f"Server PID file written: {PID_FILE} (PID {os.getpid()})")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        """Handle POST requests from the PS5 watcher"""
        if TOKEN and not hmac.compare_digest(self.headers.get('X-Command-Token', ''), TOKEN):
            error_response = json.dumps({'stdout': '', 'stderr': 'Wrong or missing X-Command-Token', 'exit_code': 1})
            self.send_response(403)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(error_response)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(error_response.encode('utf-8'))
            print(f"[REFUSED] {self.client_address[0]}: wrong or missing token")
            return
        if self.path == '/github':
            data = {}
            try:
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                status, reply = github_search(data)
            except Exception as e:
                status, reply = 400, {'error': f'bad request: {e}'}
            reply_bytes = json.dumps(reply).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(reply_bytes)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(reply_bytes)
            print(f"[GITHUB] {data.get('type', '?')} {data.get('query', '')!r}: "
                  f"{reply.get('error') or str(reply.get('total_count')) + ' results'}")
            print("---")
            return
        try:
            # Read the request body
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length)
            
            # Parse JSON
            data = json.loads(body)
            command = data['command']
            try:
                timeout = min(max(int(data.get('timeout', DEFAULT_TIMEOUT)), 1), MAX_TIMEOUT)
            except (TypeError, ValueError):
                timeout = DEFAULT_TIMEOUT
            
            # Execute the command
            try:
                result = subprocess.run(
                    command,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    universal_newlines=True,
                    errors='replace',
                    timeout=timeout
                )
                response = {
                    'stdout': result.stdout,
                    'stderr': result.stderr,
                    'exit_code': result.returncode
                }
            except subprocess.TimeoutExpired as e:
                def text(value):
                    return value.decode(errors='replace') if isinstance(value, bytes) else (value or '')
                response = {
                    'stdout': text(e.stdout),
                    'stderr': text(e.stderr) + f'\n[timed out after {timeout} s]',
                    'exit_code': 124
                }
            
            # Send response
            response_json = json.dumps(response)
            response_bytes = response_json.encode('utf-8')
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(response_bytes)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(response_bytes)
            
            # Log to stderr for debugging
            print(f"[INPUT] {command}")
            print(f"[OUTPUT] Exit code: {response['exit_code']}")
            if response['stdout']:
                print(f"[STDOUT] {response['stdout']}")
            if response['stderr']:
                print(f"[STDERR] {response['stderr']}")
            print("---")
            
        except json.JSONDecodeError:
            # Invalid JSON
            error_response = json.dumps({
                'stdout': '',
                'stderr': 'Invalid JSON in request',
                'exit_code': 1
            })
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(error_response)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(error_response.encode('utf-8'))
            
        except Exception as e:
            # Other errors
            error_response = json.dumps({
                'stdout': '',
                'stderr': str(e),
                'exit_code': 1
            })
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(error_response)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(error_response.encode('utf-8'))

    def do_GET(self):
        """Handle GET requests (for testing)"""
        if self.path == '/health':
            response = json.dumps({'status': 'ok'})
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(response)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(response.encode('utf-8'))
        else:
            self.send_response(404)
            self.send_header('Connection', 'close')
            self.end_headers()

    def log_message(self, format, *args):
        """Suppress default logging"""
        pass

if __name__ == '__main__':
    # Check for and kill old instance
    check_and_kill_old_instance()
    
    print(f"Server starting on port {PORT}")
    server = HTTPServer((BIND, PORT), Handler)
    print(f"Server ready at http://{BIND}:{PORT}" + (" (token required)" if TOKEN else " (no token: anyone who can reach it can run commands)"))
    
    # Set up signal handlers (after server is created)
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    # Serve until shutdown signal
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    
    # Clean up PID file
    if os.path.exists(PID_FILE):
        os.unlink(PID_FILE)
        print("PID file cleaned up")
    
    print("Server stopped")
