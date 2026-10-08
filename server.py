import calendar
import ctypes
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
DB = DATA / 'backup.db'
CONFIG = DATA / 'config.json'
BACKUP_ROOT = Path('H:/BackupMonitoring')
HOST = os.environ.get('BACKUP_HOST', '192.168.10.8')
# Port 8090 is the port currently published by the IIS site for
# backups.primexeu.com.  Keeping the dashboard on that port lets the existing
# router / Cloudflare route serve this application directly.
PORT = int(os.environ.get('BACKUP_PORT', '8090'))
SESSIONS = {}
LOGIN_ATTEMPTS = {}
RUN_LOCK = threading.Lock()


def usb_ready():
    if not Path('H:/').exists():
        return False
    label = ctypes.create_unicode_buffer(261)
    serial = ctypes.c_ulong()
    max_component = ctypes.c_ulong()
    flags = ctypes.c_ulong()
    filesystem = ctypes.create_unicode_buffer(261)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        'H:\\', label, len(label), ctypes.byref(serial), ctypes.byref(max_component),
        ctypes.byref(flags), filesystem, len(filesystem))
    return bool(ok) and label.value.upper() == 'EXTERNAL_USB'


def now():
    return datetime.now().astimezone()


def stamp(dt=None):
    return (dt or now()).isoformat(timespec='seconds')


def connect():
    db = sqlite3.connect(DB, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def setup():
    DATA.mkdir(exist_ok=True)
    if not CONFIG.exists():
        password = secrets.token_urlsafe(18)
        salt = secrets.token_hex(16)
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
        CONFIG.write_text(json.dumps({'salt': salt, 'password_hash': digest}, indent=2), encoding='utf-8')
        (DATA / 'initial-password.txt').write_text(password, encoding='utf-8')
    with connect() as db:
        db.executescript('''
          CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, source TEXT NOT NULL UNIQUE,
            destination TEXT NOT NULL, frequency TEXT NOT NULL, run_time TEXT NOT NULL,
            weekday INTEGER NOT NULL DEFAULT 0, monthday INTEGER NOT NULL DEFAULT 1,
            enabled INTEGER NOT NULL DEFAULT 1, next_run TEXT,
            created_at TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0
          );
          CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, job_id TEXT NOT NULL, status TEXT NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT, exit_code INTEGER,
            message TEXT NOT NULL DEFAULT '', trigger TEXT NOT NULL,
            log_path TEXT NOT NULL,
            FOREIGN KEY(job_id) REFERENCES jobs(id)
          );
          CREATE INDEX IF NOT EXISTS idx_runs_job ON runs(job_id, started_at DESC);
        ''')
        if 'archived' not in [row['name'] for row in db.execute('PRAGMA table_info(jobs)')]:
            db.execute('ALTER TABLE jobs ADD COLUMN archived INTEGER NOT NULL DEFAULT 0')
        db.execute("UPDATE runs SET status='interrupted', finished_at=?, message='Server stopped during this run' WHERE status='running'", (stamp(),))
        db.commit()
    for path, name in [('F:/FILES', 'FILES'), ('E:/Klientat', 'Klientat')]:
        if Path(path).is_dir():
            with connect() as db:
                if not db.execute('SELECT 1 FROM jobs WHERE lower(source)=lower(?)', (str(Path(path)),)).fetchone():
                    add_job(name, path, 'daily', '22:00', 0, 1, db)


def next_due(frequency, run_time, weekday, monthday, after=None):
    ref = after or now()
    hour, minute = map(int, run_time.split(':'))
    base = ref.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if frequency == 'daily':
        if base <= ref:
            base += timedelta(days=1)
    elif frequency == 'weekly':
        base += timedelta(days=(weekday - base.weekday()) % 7)
        if base <= ref:
            base += timedelta(days=7)
    else:
        year, month = ref.year, ref.month
        while True:
            day = min(monthday, calendar.monthrange(year, month)[1])
            base = base.replace(year=year, month=month, day=day)
            if base > ref:
                break
            month += 1
            if month > 12:
                year, month = year + 1, 1
    return stamp(base)


def validate_source(raw):
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError('Choose a source folder.')
    path = Path(raw.strip()).resolve(strict=True)
    if not path.is_dir():
        raise ValueError('Source must be a folder.')
    if path.drive.upper() not in ('C:', 'E:', 'F:'):
        raise ValueError('Choose a folder on C:, E:, or F:. The USB drive cannot be a source.')
    if str(path).lower().startswith(str(ROOT).lower()):
        raise ValueError('The dashboard folder cannot be a source.')
    return str(path)


def add_job(name, source, frequency, run_time, weekday, monthday, db=None):
    source = validate_source(source)
    name = str(name).strip()[:80]
    if not name:
        raise ValueError('Enter a name for this backup.')
    if frequency not in ('daily', 'weekly', 'monthly'):
        raise ValueError('Choose daily, weekly, or monthly.')
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', run_time):
        raise ValueError('Choose a valid time.')
    weekday, monthday = int(weekday), int(monthday)
    if weekday not in range(7) or monthday not in range(1, 32):
        raise ValueError('Choose a valid day.')
    job_id = uuid.uuid4().hex[:12]
    destination = str(BACKUP_ROOT / 'jobs' / job_id / 'data')
    own = db is None
    db = db or connect()
    try:
        existing = db.execute('SELECT id, archived FROM jobs WHERE lower(source)=lower(?)', (source,)).fetchone()
        if existing and existing['archived']:
            db.execute('UPDATE jobs SET archived=0, enabled=1, name=?, frequency=?, run_time=?, weekday=?, monthday=?, next_run=? WHERE id=?',
                       (name, frequency, run_time, weekday, monthday,
                        next_due(frequency, run_time, weekday, monthday), existing['id']))
            db.commit()
            return existing['id']
        db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                   (job_id, name, source, destination, frequency, run_time,
                    weekday, monthday, 1, next_due(frequency, run_time, weekday, monthday), stamp(), 0))
        db.commit()
        return job_id
    except sqlite3.IntegrityError:
        raise ValueError('This folder already has a backup job.')
    finally:
        if own:
            db.close()


def run_job(job_id, trigger):
    if not RUN_LOCK.acquire(blocking=False):
        raise ValueError('Another backup is running. Try again when it finishes.')
    try:
        with connect() as db:
            job = db.execute('SELECT * FROM jobs WHERE id=? AND archived=0', (job_id,)).fetchone()
            if not job:
                raise ValueError('Backup job not found.')
            run_id = uuid.uuid4().hex[:14]
            log_path = DATA / 'logs' / (run_id + '.log')
            log_path.parent.mkdir(exist_ok=True)
            db.execute('INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?)',
                       (run_id, job_id, 'running', stamp(), None, None, 'Copying files', trigger, str(log_path)))
            db.commit()
        threading.Thread(target=perform_backup, args=(run_id, dict(job), log_path), daemon=True).start()
        return run_id
    except Exception:
        RUN_LOCK.release()
        raise


def perform_backup(run_id, job, log_path):
    code = None
    status = 'failed'
    message = 'Backup failed'
    try:
        source = validate_source(job['source'])
        if not usb_ready():
            raise RuntimeError('EXTERNAL_USB (H:) is unavailable or has a different label.')
        Path(job['destination']).mkdir(parents=True, exist_ok=True)
        manifest = Path(job['destination']).parent / 'source.json'
        manifest.write_text(json.dumps({'source': source, 'job': job['name'], 'job_id': job['id']}, indent=2), encoding='utf-8')
        command = ['robocopy', source, job['destination'], '/E', '/COPY:DAT', '/DCOPY:DAT',
                   '/R:2', '/W:3', '/XJ', '/FFT', '/MT:32', '/NP', '/TS', '/FP',
                   '/XD', '$RECYCLE.BIN', 'System Volume Information', '/LOG:' + str(log_path)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=48 * 3600,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        code = result.returncode
        if code < 8:
            status, message = 'success', 'Files copied successfully'
            if code & 4:
                status, message = 'warning', 'Copy finished with mismatched files; inspect the log'
        else:
            message = 'Robocopy reported an error (exit code %s)' % code
        if result.stderr:
            with log_path.open('a', encoding='utf-8', errors='replace') as log:
                log.write('\n\nERROR OUTPUT\n' + result.stderr)
    except subprocess.TimeoutExpired:
        message = 'Backup exceeded the 48-hour limit'
    except Exception as error:
        message = str(error)
        with log_path.open('a', encoding='utf-8', errors='replace') as log:
            log.write(message + '\n')
    finally:
        with connect() as db:
            db.execute('UPDATE runs SET status=?, finished_at=?, exit_code=?, message=? WHERE id=?',
                       (status, stamp(), code, message, run_id))
            db.commit()
        RUN_LOCK.release()


def scheduler():
    while True:
        try:
            with connect() as db:
                due = db.execute("SELECT * FROM jobs WHERE enabled=1 AND archived=0 AND next_run<=? ORDER BY next_run LIMIT 1", (stamp(),)).fetchone()
                if due:
                    db.execute('UPDATE jobs SET next_run=? WHERE id=?',
                               (next_due(due['frequency'], due['run_time'], due['weekday'], due['monthday']), due['id']))
                    db.commit()
            if due:
                try:
                    run_job(due['id'], 'scheduled')
                except ValueError:
                    with connect() as db:
                        db.execute('UPDATE jobs SET next_run=? WHERE id=?',
                                   (stamp(now() + timedelta(minutes=5)), due['id']))
                        db.commit()
        except Exception as error:
            print('Scheduler error:', error, flush=True)
        time.sleep(15)


class Handler(BaseHTTPRequestHandler):
    server_version = 'BackupMonitoring/1.0'

    def respond(self, status, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def body(self):
        length = int(self.headers.get('Content-Length', '0'))
        if length > 32768:
            raise ValueError('Request too large.')
        return json.loads(self.rfile.read(length) or b'{}')

    def authenticated(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
            token = cookie['backup_session'].value
            expiry = SESSIONS.get(token, 0)
            return expiry > time.time()
        except (KeyError, ValueError):
            return False

    def require_auth(self):
        if not self.authenticated():
            self.respond(401, {'error': 'Sign in to continue.'})
            return False
        return True

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        if route in ('/', '/index.html', '/app.css', '/branding.css', '/app.js', '/sidebar-logo.png', '/tab-logo.png'):
            file = ROOT / ('index.html' if route == '/' else route.lstrip('/'))
            if not file.is_file():
                self.send_error(404)
                return
            raw = file.read_bytes()
            kind = 'text/html' if file.suffix == '.html' else ('text/css' if file.suffix == '.css' else ('image/png' if file.suffix == '.png' else 'text/javascript'))
            self.send_response(200)
            self.send_header('Content-Type', kind + '; charset=utf-8')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if not self.require_auth():
            return
        try:
            if route == '/api/state':
                with connect() as db:
                    jobs = [dict(row) for row in db.execute('SELECT * FROM jobs WHERE archived=0 ORDER BY created_at')]
                    runs = [dict(row) for row in db.execute('''SELECT runs.*, jobs.name AS job_name, jobs.source, jobs.destination
                      FROM runs JOIN jobs ON jobs.id=runs.job_id ORDER BY runs.started_at DESC LIMIT 100''')]
                for run in runs:
                    run.pop('log_path', None)
                connected = usb_ready()
                usage = shutil.disk_usage('H:/') if connected else None
                self.respond(200, {'jobs': jobs, 'runs': runs, 'usb_connected': connected,
                                   'usb_free': usage.free if usage else None,
                                   'server_time': stamp(), 'destination_root': str(BACKUP_ROOT)})
            elif route == '/api/browse':
                raw = parse_qs(parsed.query).get('path', [''])[0]
                if not raw:
                    self.respond(200, {'path': '', 'folders': [{'name': 'C:', 'path': 'C:/'}, {'name': 'E:', 'path': 'E:/'}, {'name': 'F:', 'path': 'F:/'}]})
                    return
                path = validate_source(raw)
                folders = []
                with os.scandir(path) as entries:
                    for entry in entries:
                        try:
                            if entry.is_dir(follow_symlinks=False) and not entry.name.startswith('$'):
                                folders.append({'name': entry.name, 'path': str(Path(path) / entry.name)})
                        except PermissionError:
                            pass
                folders.sort(key=lambda item: item['name'].lower())
                self.respond(200, {'path': path, 'parent': str(Path(path).parent) if Path(path).parent.drive in ('C:', 'E:', 'F:') else '', 'folders': folders[:500]})
            elif route.startswith('/api/runs/') and route.endswith('/log'):
                run_id = route.split('/')[3]
                with connect() as db:
                    row = db.execute('SELECT log_path FROM runs WHERE id=?', (run_id,)).fetchone()
                if not row:
                    self.respond(404, {'error': 'Run not found.'})
                    return
                path = Path(row['log_path'])
                if path.exists():
                    with path.open('rb') as log_file:
                        log_file.seek(0, os.SEEK_END)
                        log_file.seek(max(0, log_file.tell() - 100000))
                        content = log_file.read().decode('utf-8', errors='replace')
                else:
                    content = 'Log is not available yet.'
                self.respond(200, {'log': content})
            else:
                self.respond(404, {'error': 'Not found.'})
        except Exception as error:
            self.respond(400, {'error': str(error)})

    def do_POST(self):
        route = urlparse(self.path).path
        try:
            origin = self.headers.get('Origin')
            # Cloudflare terminates HTTPS before forwarding to this local HTTP
            # service.  Respect its scheme header so the public HTTPS site can
            # use the API while still rejecting requests from another origin.
            scheme = self.headers.get('X-Forwarded-Proto', 'http').split(',')[0].strip().lower()
            expected_origin = scheme + '://' + self.headers.get('Host', '')
            if origin and origin != expected_origin:
                self.respond(403, {'error': 'Invalid request origin.'})
                return
            data = self.body()
            if route == '/api/login':
                ip = self.client_address[0]
                attempts = [t for t in LOGIN_ATTEMPTS.get(ip, []) if time.time() - t < 900]
                if len(attempts) >= 8:
                    self.respond(429, {'error': 'Too many attempts. Try again in 15 minutes.'})
                    return
                config = json.loads(CONFIG.read_text(encoding='utf-8'))
                digest = hashlib.scrypt(str(data.get('password', '')).encode(), salt=bytes.fromhex(config['salt']), n=16384, r=8, p=1).hex()
                if not hmac.compare_digest(digest, config['password_hash']):
                    LOGIN_ATTEMPTS[ip] = attempts + [time.time()]
                    self.respond(401, {'error': 'Incorrect password.'})
                    return
                LOGIN_ATTEMPTS.pop(ip, None)
                token = secrets.token_urlsafe(32)
                SESSIONS[token] = time.time() + 12 * 3600
                raw = b'{"ok":true}'
                self.send_response(200)
                cookie = 'backup_session=' + token + '; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200'
                if scheme == 'https':
                    cookie += '; Secure'
                self.send_header('Set-Cookie', cookie)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if not self.require_auth():
                return
            if route == '/api/logout':
                cookie = SimpleCookie()
                cookie.load(self.headers.get('Cookie', ''))
                if 'backup_session' in cookie:
                    SESSIONS.pop(cookie['backup_session'].value, None)
                self.send_response(200)
                self.send_header('Set-Cookie', 'backup_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
                self.send_header('Content-Length', '0')
                self.end_headers()
            elif route == '/api/jobs':
                job_id = add_job(data.get('name', ''), data.get('source', ''), data.get('frequency', ''),
                                 data.get('run_time', ''), data.get('weekday', 0), data.get('monthday', 1))
                self.respond(201, {'id': job_id})
            elif route.startswith('/api/jobs/') and route.endswith('/run'):
                self.respond(202, {'run_id': run_job(route.split('/')[3], 'manual')})
            elif route.startswith('/api/jobs/') and route.endswith('/toggle'):
                job_id = route.split('/')[3]
                with connect() as db:
                    db.execute('UPDATE jobs SET enabled=1-enabled WHERE id=? AND archived=0', (job_id,))
                    db.commit()
                self.respond(200, {'ok': True})
            else:
                self.respond(404, {'error': 'Not found.'})
        except Exception as error:
            self.respond(400, {'error': str(error)})

    def do_DELETE(self):
        if not self.require_auth():
            return
        route = urlparse(self.path).path
        if route.startswith('/api/jobs/'):
            job_id = route.split('/')[3]
            with connect() as db:
                running = db.execute("SELECT 1 FROM runs WHERE job_id=? AND status='running'", (job_id,)).fetchone()
                if running:
                    self.respond(409, {'error': 'Wait for the current run to finish.'})
                    return
                db.execute('UPDATE jobs SET archived=1, enabled=0 WHERE id=?', (job_id,))
                db.commit()
            self.respond(200, {'ok': True})
        else:
            self.respond(404, {'error': 'Not found.'})


if __name__ == '__main__':
    setup()
    threading.Thread(target=scheduler, daemon=True).start()
    print('Backup Monitoring listening on %s:%s' % (HOST, PORT), flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
