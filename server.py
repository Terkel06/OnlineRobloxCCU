"""Roblox Studio Pulse — local, dependency-free CCU dashboard.

Run: python server.py
Then open http://127.0.0.1:8765
"""
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.parse import urlencode, urlsplit
from urllib.error import HTTPError, URLError
from pathlib import Path
from threading import Lock, Timer
import json
import time
import webbrowser
import os

GROUP_ID = 934104263
PORT = int(os.environ.get("PORT", "8765"))
HOST = "0.0.0.0" if "PORT" in os.environ else "127.0.0.1"
ROOT = Path(__file__).resolve().parent

# Group inventory doesn't need to be re-fetched every 10 seconds. Only CCU does.
GROUP_CACHE_SECONDS = 300
METADATA_CACHE_SECONDS = 1800
STALE_DATA_MAX_SECONDS = 300

_cache_lock = Lock()
_cache = {
    'games': [], 'games_expires': 0,
    'group_name': None, 'name_expires': 0,
    'icons': {}, 'icons_expires': 0,
    'latest': None, 'latest_at': 0,
}


def get_json(host, path, params=None):
    url = f'https://{host}{path}'
    if params:
        url += '?' + urlencode(params)
    req = Request(url, headers={
        'Accept': 'application/json',
        'User-Agent': 'Mozilla/5.0 (compatible; StudioPulse/2.0)',
    })
    with urlopen(req, timeout=14) as response:
        return json.load(response)


def group_games():
    with _cache_lock:
        if time.time() < _cache['games_expires']:
            return list(_cache['games'])

    seen, games, cursor = set(), [], None
    for _ in range(100):
        params = {'accessFilter': 2, 'limit': 100, 'sortOrder': 'Asc'}
        if cursor:
            params['cursor'] = cursor
        page = get_json('games.roblox.com', f'/v2/groups/{GROUP_ID}/gamesV2', params)
        if not isinstance(page, dict) or not isinstance(page.get('data'), list):
            raise ValueError('Roblox returned an unexpected group-games response.')
        for game in page['data']:
            universe_id = game.get('id', game.get('universeId'))
            if universe_id is not None and str(universe_id).isdigit() and str(universe_id) not in seen:
                seen.add(str(universe_id))
                games.append({'id': int(universe_id), 'name': game.get('name') or 'Unnamed experience'})
        next_cursor = page.get('nextPageCursor')
        if not next_cursor:
            break
        if next_cursor == cursor:
            raise ValueError('Roblox returned a repeated pagination cursor.')
        cursor = next_cursor
    else:
        raise ValueError('Group game list exceeded the supported pagination limit.')

    with _cache_lock:
        _cache['games'] = games
        _cache['games_expires'] = time.time() + GROUP_CACHE_SECONDS
    return games


def get_group_name():
    with _cache_lock:
        if time.time() < _cache['name_expires']:
            return _cache['group_name']
    name = f'Community {GROUP_ID}'
    try:
        result = get_json('groups.roblox.com', f'/v1/groups/{GROUP_ID}')
        name = result.get('name') or name
    except (HTTPError, URLError, TimeoutError, ValueError, KeyError):
        pass  # Group display name is optional; live CCU still works.
    with _cache_lock:
        _cache['group_name'] = name
        _cache['name_expires'] = time.time() + METADATA_CACHE_SECONDS
    return name


def get_icons(ids):
    with _cache_lock:
        if time.time() < _cache['icons_expires']:
            return dict(_cache['icons'])
    icons = {}
    try:
        for i in range(0, len(ids), 50):
            data = get_json('thumbnails.roblox.com', '/v1/games/icons', {
                'universeIds': ','.join(map(str, ids[i:i + 50])),
                'size': '150x150', 'format': 'Png', 'isCircular': 'false'
            })
            for item in data.get('data', []):
                url = item.get('imageUrl')
                # Avoid allowing arbitrary URL values into the client.
                if isinstance(url, str) and url.startswith('https://'):
                    icons[str(item.get('targetId'))] = url
    except (HTTPError, URLError, TimeoutError, ValueError, KeyError):
        pass  # Icons are decorative and must not break live stats.
    with _cache_lock:
        _cache['icons'] = icons
        _cache['icons_expires'] = time.time() + METADATA_CACHE_SECONDS
    return icons


def get_snapshot():
    games = group_games()
    names = {str(g['id']): g['name'] for g in games}
    ids = [g['id'] for g in games]
    results = []
    for i in range(0, len(ids), 50):
        data = get_json('games.roblox.com', '/v1/games', {
            'universeIds': ','.join(map(str, ids[i:i + 50]))
        })
        if not isinstance(data, dict) or not isinstance(data.get('data'), list):
            raise ValueError('Roblox returned an unexpected game-details response.')
        for game in data['data']:
            uid = str(game.get('id'))
            if uid not in names:
                continue
            results.append({
                'id': int(uid),
                'name': game.get('name') or names[uid],
                'playing': int(game.get('playing') or 0),
                'visits': int(game.get('visits') or 0),
                'rootPlaceId': game.get('rootPlaceId'),
            })

    icons = get_icons(ids)
    for game in results:
        game['icon'] = icons.get(str(game['id']))

    results.sort(key=lambda item: item['playing'], reverse=True)
    snapshot = {
        'groupId': GROUP_ID,
        'groupName': get_group_name(),
        'total': sum(g['playing'] for g in results),
        'games': results,
        'count': len(results),
        'updatedAt': int(time.time()),
        'stale': False,
    }
    with _cache_lock:
        _cache['latest'] = snapshot
        _cache['latest_at'] = time.time()
    return snapshot


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/api/ccu':
            try:
                self.json_reply(200, get_snapshot())
            except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
                with _cache_lock:
                    saved = _cache['latest']
                    saved_at = _cache['latest_at']
                if saved and time.time() - saved_at < STALE_DATA_MAX_SECONDS:
                    self.json_reply(200, {**saved, 'stale': True, 'error': 'Roblox data is temporarily unavailable. Showing the last successful reading.'})
                else:
                    status = f'HTTP {exc.code}' if isinstance(exc, HTTPError) else 'connection error'
                    self.json_reply(502, {'error': f'Unable to reach Roblox ({status}). Please try again.'})
            except Exception as exc:
                print('Unexpected error:', repr(exc))
                self.json_reply(500, {'error': 'Unexpected server error. Check the terminal window.'})
            return
        # Serve only the dashboard and its favicon; do not expose the directory.
        if path in ('/', '/index.html', '/favicon.svg'):
            return super().do_GET()
        self.send_error(404)

    def json_reply(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store, max-age=0')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run():
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        if getattr(exc, 'errno', None) in (98, 10048):
            print(f'Port {PORT} is already in use. If the dashboard is already running, open http://localhost:{PORT}')
        else:
            print('Could not start local server:', exc)
        if HOST == '127.0.0.1':
            input('Press Enter to exit...')
        return
    print('\n  STUDIO PULSE | Roblox CCU Dashboard')
    print(f'  Community: {GROUP_ID}')
    print(f'  Local website: http://localhost:{PORT}')
    print('  Refresh interval: 10 seconds')
    print('  Press Ctrl+C or close this window to stop.\n')
    if HOST == '127.0.0.1':
        Timer(0.6, lambda: webbrowser.open(f'http://localhost:{PORT}')).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nDashboard stopped.')
    finally:
        server.server_close()


if __name__ == '__main__':
    run()
