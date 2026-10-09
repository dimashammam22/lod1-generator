"""Local web server for Generator LOD1.

Processing runs here, on this PC, through main.batch_lod1_from_basedir.
The browser UI (files in ./web) only sends commands and receives live
progress over Server-Sent Events. The server listens on 127.0.0.1 only.

Run:  python app/server.py    (opens http://localhost:8765 in the browser)
"""
import os
import sys
import json
import time
import queue
import mimetypes
import threading
import subprocess
import webbrowser
import urllib.request
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

if not getattr(sys, 'frozen', False):
    # Allow `python app/server.py` from anywhere: the lod1 package sits one level up.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

HOST = '127.0.0.1'
PREFERRED_PORT = 8765
APP_ID = 'generator-lod1'
MAX_LOG = 3000
MODE_NAMES = {'las': 'LAS ground', 'dtm': 'External DTM', 'auto': 'Auto DTM'}


def console(text=''):
    if sys.__stdout__ is not None:
        sys.__stdout__.write(text + '\n')
        sys.__stdout__.flush()


def web_root():
    """A ./web folder next to the exe wins over the bundled copy, so the UI
    can be customised without rebuilding."""
    candidates = []
    if getattr(sys, 'frozen', False):
        candidates.append(Path(sys.executable).parent / 'web')
        candidates.append(Path(sys._MEIPASS) / 'web')
    candidates.append(Path(__file__).resolve().parent / 'web')
    for c in candidates:
        if (c / 'index.html').is_file():
            return c.resolve()
    raise FileNotFoundError('web/index.html not found')


# ====================================================================== #
# EVENT HUB (fan-out to every connected browser tab)
# ====================================================================== #
class Hub:
    def __init__(self):
        self.lock = threading.Lock()
        self.clients = set()

    def subscribe(self):
        q = queue.Queue()
        with self.lock:
            self.clients.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.clients.discard(q)

    def publish(self, event, data):
        with self.lock:
            for q in self.clients:
                q.put((event, data))


hub = Hub()


# ====================================================================== #
# INPUT INSPECTION (what the Data panel shows before a run)
# ====================================================================== #
_inspect_cache = {}


def inspect_folder(folder):
    """LAS/GPKG pairs under ``folder`` with header facts. The classification
    share comes from the first 1 M points of the first few tiles only."""
    import numpy as np
    import laspy
    import pyogrio
    from lod1.core import find_tile_pairs

    folder = Path(folder)
    key = str(folder.resolve())
    matched, skipped = find_tile_pairs(folder)
    stamp = tuple((str(m[1]), m[1].stat().st_mtime, str(m[2]), m[2].stat().st_mtime) for m in matched)
    if key in _inspect_cache and _inspect_cache[key][0] == stamp:
        return _inspect_cache[key][1]

    tiles = []
    for n, (label, las_path, gpkg_path, _) in enumerate(matched):
        t = {'label': label, 'las': str(las_path), 'gpkg': str(gpkg_path),
             'bytes': las_path.stat().st_size}
        try:
            with laspy.open(las_path) as f:
                h = f.header
                t['points'] = int(h.point_count)
                t['bounds'] = [float(h.mins[0]), float(h.mins[1]), float(h.maxs[0]), float(h.maxs[1])]
                try:
                    crs = h.parse_crs()
                    t['crs'] = f"EPSG:{crs.to_epsg()}" if crs and crs.to_epsg() else (crs.name if crs else None)
                except Exception:
                    t['crs'] = None
                if n < 5:
                    c = next(f.chunk_iterator(1_000_000))
                    cls = np.asarray(c.classification)
                    t['ground_share'] = float(np.mean(cls == 2))
                    t['unclassified_share'] = float(np.mean(cls <= 1))
        except Exception as e:
            t['error'] = f"LAS: {e}"
        try:
            t['footprints'] = int(pyogrio.read_info(gpkg_path)['features'])
        except Exception as e:
            t['error'] = f"GPKG: {e}"
        tiles.append(t)
    result = {'folder': str(folder), 'tiles': tiles, 'skipped': skipped}
    _inspect_cache[key] = (stamp, result)
    return result


def footprint_geometry(las_path, gpkg_path):
    """Footprints in the LAS CRS, simplified, as plain coordinate rings for
    the browser map. Order matches the BHM arrays of a run."""
    import geopandas as gpd
    import laspy
    gdf = gpd.read_file(gpkg_path)
    crs = None
    try:
        with laspy.open(las_path) as f:
            crs = f.header.parse_crs()
    except Exception:
        pass
    if crs is not None and gdf.crs is not None and gdf.crs != crs:
        gdf = gdf.to_crs(crs)
    feats = []
    for g in gdf.geometry.values:
        rings = []
        if g is not None and not g.is_empty:
            g = g.simplify(0.15)
            polys = getattr(g, 'geoms', [g])
            for p in polys:
                if p.geom_type == 'Polygon':
                    rings.append([[round(x, 2), round(y, 2)] for x, y in p.exterior.coords])
        feats.append(rings)
    b = gdf.total_bounds
    return {'bounds': [float(v) for v in b], 'features': feats}


def dtm_hillshade_png(dtm_path, max_px=1400):
    """Hillshade of a DTM GeoTIFF as PNG bytes plus its bounds."""
    import numpy as np
    import rasterio
    from rasterio.io import MemoryFile
    with rasterio.open(dtm_path) as src:
        step = max(1, int(np.ceil(max(src.width, src.height) / max_px)))
        d = src.read(1, out_shape=(src.height // step, src.width // step)).astype(float)
        nodata = src.nodata
        b = src.bounds
        res = abs(src.res[0]) * step
    valid = d != nodata if nodata is not None else np.ones_like(d, bool)
    d[~valid] = np.nan
    gy, gx = np.gradient(np.where(valid, d, np.nanmean(d)), res)
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    az, alt = np.radians(315), np.radians(45)
    hs = np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(az - aspect)
    grey = np.clip(40 + hs * 170, 0, 255).astype(np.uint8)
    alpha = np.where(valid, 255, 0).astype(np.uint8)
    with MemoryFile() as mem:
        with mem.open(driver='PNG', width=grey.shape[1], height=grey.shape[0], count=2,
                      dtype='uint8') as dst:
            dst.write(grey, 1)
            dst.write(alpha, 2)
        png = mem.read()
    return png, [b.left, b.bottom, b.right, b.top]


# ====================================================================== #
# JOB STATE
# ====================================================================== #
class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.seq = 0
        self.logs = []
        self.history = []
        self._reset()

    def _reset(self):
        self.running = False
        self.status = 'Ready'
        self.mode = None
        self.start_time = None
        self.end_time = None
        self.tiles = []          # [{label, las, gpkg, bytes}]
        self.tile_index = 0
        self.stages = []         # current tile's pipeline
        self.progress = 0.0      # 0..1 over the whole batch
        self.params = {}
        self.summary = None
        self.results = {}        # label -> stats
        self.outputs = []
        self.report = None
        self.warnings = 0
        self.log_start = self.seq

    # -- state ---------------------------------------------------------- #
    def snapshot(self, with_logs=True):
        with self.lock:
            snap = {
                'running': self.running, 'status': self.status, 'mode': self.mode,
                'start_time': self.start_time, 'end_time': self.end_time,
                'tiles': [{k: t[k] for k in ('label', 'las', 'gpkg', 'bytes')} for t in self.tiles],
                'tile_index': self.tile_index, 'stages': [dict(s) for s in self.stages],
                'progress': self.progress, 'params': dict(self.params),
                'summary': self.summary, 'results': self.results,
                'outputs': list(self.outputs), 'report': self.report,
                'warnings': self.warnings, 'history': list(self.history), 'now': time.time(),
            }
            if with_logs:
                snap['logs'] = list(self.logs)
            return snap

    def set_status(self, status):
        with self.lock:
            self.status = status
        hub.publish('status', {'status': status, 'running': self.running,
                               'start_time': self.start_time, 'end_time': self.end_time})

    def log(self, level, text):
        with self.lock:
            self.seq += 1
            if level == 'warning' and self.running:
                self.warnings += 1
            entry = {'id': self.seq, 't': datetime.now().strftime('%H:%M:%S'),
                     'level': level, 'text': text}
            self.logs.append(entry)
            del self.logs[:-MAX_LOG]
        hub.publish('log', entry)

    def clear_log(self):
        with self.lock:
            self.logs.clear()

    # -- control -------------------------------------------------------- #
    def start(self, data):
        from lod1.core import find_tile_pairs, PIPELINES
        with self.lock:
            if self.running:
                return "A process is already running."
        inp = str(data.get('input', '')).strip()
        out = str(data.get('output', '')).strip()
        mode = str(data.get('ground', 'auto'))
        if not inp or not out:
            return "Input folder and output folder are required."
        if not os.path.isdir(inp):
            return f"Input folder not found: {inp}"
        if mode not in PIPELINES:
            return f"Unknown ground source: {mode}"
        try:
            percentile = int(data.get('percentile', 95))
            min_height = float(data.get('min_height', 2.0))
            inner_buffer = float(data.get('inner_buffer', 0.5))
            dtm_res = float(data.get('dtm_res', 0.5))
            rigidness = int(data.get('rigidness', 2))
            dtm_offset = float(data.get('dtm_offset', 0) or 0)
        except (TypeError, ValueError):
            return "Settings must be numbers."
        if not 1 <= percentile <= 100:
            return "Percentile must be between 1 and 100."
        if not 0.1 <= dtm_res <= 5:
            return "DTM resolution must be between 0.1 and 5 m."
        if rigidness not in (1, 2, 3):
            return "Terrain setting must be 1, 2 or 3."
        dtm = str(data.get('dtm', '')).strip()
        if mode == 'dtm':
            if not dtm:
                return "DTM path is required for the External DTM ground source."
            if not os.path.exists(dtm):
                return f"DTM not found: {dtm}"

        matched, _ = find_tile_pairs(inp)
        params = {'Input folder': inp, 'Output folder': out,
                  'Ground source': MODE_NAMES[mode], 'Percentile': f"P{percentile}"}
        if mode == 'dtm':
            params.update({'DTM': dtm, 'Vertical offset': f"{dtm_offset:.2f} m"})
        if mode == 'auto':
            params.update({'DTM resolution': f"{dtm_res} m",
                           'Terrain': {1: 'Steep', 2: 'Hilly', 3: 'Flat'}[rigidness] + f" (rigidness {rigidness})"})
        if mode != 'las':
            params.update({'Min height': f"{min_height} m", 'Inner buffer': f"{inner_buffer} m"})
        params.update({'Target CRS': 'EPSG:4326', 'Started': datetime.now().strftime('%Y-%m-%d %H:%M:%S')})

        with self.lock:
            self._reset()
            self.running = True
            self.mode = mode
            self.stop_event.clear()
            self.start_time = time.time()
            self.tiles = [{'label': m[0], 'las': str(m[1]), 'gpkg': str(m[2]),
                           'bytes': m[1].stat().st_size} for m in matched]
            self.stages = self._new_stages(mode)
            self.params = params
            self.status = 'Scanning files...'
            self.log_start = self.seq
        hub.publish('reset', self.snapshot(with_logs=False))
        self.log('info', f"Processing started · {MODE_NAMES[mode]} · {len(matched)} tile(s) · {inp}")

        kwargs = dict(basedir=inp, output_dir=out, ground_mode=mode, percentile=percentile,
                      min_height=min_height, inner_buffer=inner_buffer, dtm_res=dtm_res,
                      rigidness=rigidness, dtm_path=dtm if mode == 'dtm' else None,
                      dtm_offset=dtm_offset, target_epsg=4326, resolution=0.1)
        self.thread = threading.Thread(target=self._run, args=(kwargs, out), daemon=True)
        self.thread.start()
        return None

    @staticmethod
    def _new_stages(mode):
        from lod1.core import PIPELINES
        return [{'key': k, 'label': lbl, 'weight': w, 'status': 'queued', 'detail': '',
                 'frac': 0.0, 'started': None, 'ended': None} for k, lbl, w in PIPELINES[mode]]

    def stop(self):
        if self.running and not self.stop_event.is_set():
            self.stop_event.set()
            self.set_status('Stopping after current tile...')
            self.log('warning', "Stop requested. Processing will stop after the current tile.")

    # -- callbacks from the worker thread ------------------------------- #
    def _recompute_progress(self):
        total = sum(t['bytes'] for t in self.tiles) or 1
        done = sum(t['bytes'] for t in self.tiles[:self.tile_index])
        cur = self.tiles[self.tile_index]['bytes'] if self.tile_index < len(self.tiles) else 0
        within = sum(s['weight'] * (1.0 if s['status'] == 'done' else s['frac']) for s in self.stages)
        self.progress = min(1.0, (done + cur * within) / total)

    def _on_progress(self, done, total, label, done_bytes, total_bytes):
        with self.lock:
            self.tile_index = done
            if label:
                self.stages = self._new_stages(self.mode)
            self._recompute_progress()
        if label:
            self.set_status(f"Tile {done + 1}/{total} · {label}")
            self.log('info', f"[{done + 1}/{total}] {label}")
        hub.publish('progress', {'progress': self.progress, 'tile_index': self.tile_index,
                                 'stages': self.stages})

    def _on_stage(self, tile_idx, key, status, detail, frac):
        now = time.time()
        with self.lock:
            for s in self.stages:
                if s['key'] != key:
                    continue
                if status == 'running' and s['started'] is None:
                    s['started'] = now
                if status in ('done', 'failed'):
                    s['ended'] = now
                    s['frac'] = 1.0 if status == 'done' else s['frac']
                if frac is not None:
                    s['frac'] = float(frac)
                if detail:
                    s['detail'] = detail
                s['status'] = status
            self._recompute_progress()
            stages = [dict(s) for s in self.stages]
        if status == 'done' and detail:
            label = next((s['label'] for s in stages if s['key'] == key), key)
            self.log('done', f"{label} · {detail}")
        hub.publish('progress', {'progress': self.progress, 'tile_index': self.tile_index,
                                 'stages': stages})

    def _on_tile(self, idx, label, stats):
        with self.lock:
            self.results[label] = stats
        hub.publish('tile', {'index': idx, 'label': label, 'stats': stats})

    def _run(self, kwargs, out):
        try:
            from lod1.core import batch_lod1_from_basedir
            summary = batch_lod1_from_basedir(
                **kwargs,
                progress_callback=self._on_progress,
                stage_callback=self._on_stage,
                tile_callback=self._on_tile,
                stop_event=self.stop_event,
            )
            self._finish(summary, out)
        except Exception as e:
            self._finish(None, out, str(e))

    def _finish(self, summary, out, error=None):
        end = time.time()
        elapsed = time.strftime('%H:%M:%S', time.gmtime(end - self.start_time))
        outputs = []
        if summary:
            for label, st in summary.get('tiles', {}).items():
                if 'error' in st:
                    continue
                outputs.append({'name': os.path.relpath(st['output'], out), 'path': st['output'],
                                'kind': 'geojson', 'label': label})
                if st.get('dtm'):
                    outputs.append({'name': os.path.relpath(st['dtm'], out), 'path': st['dtm'],
                                    'kind': 'dtm', 'label': label})
                self.log('success', f"Saved {os.path.relpath(st['output'], out)}")
            for item in summary['failed']:
                self.log('error', f"Failed {item['name']}: {item['reason']}")
            for item in summary['skipped']:
                self.log('warning', f"Skipped {item['name']}: {item['reason']}")

        if error:
            status = 'Error'
            message = f"Processing failed after {elapsed}: {error}"
            self.log('error', message)
        else:
            ok, fail, skip = (len(summary['success']), len(summary['failed']),
                              len(summary['skipped']))
            counts = f"{ok} success, {fail} failed, {skip} skipped"
            if summary.get('cancelled'):
                status, message = 'Stopped', f"Stopped by user after {elapsed}: {counts}."
                self.log('warning', message)
            else:
                status, message = 'Completed', f"Completed in {elapsed}: {counts}."
                self.log('success' if fail == 0 else 'warning', message)

        with self.lock:
            self.running = False
            self.end_time = end
            self.summary = None if summary is None else {
                k: summary[k] for k in ('success', 'failed', 'skipped', 'cancelled') if k in summary}
            self.params['Duration'] = elapsed
            self.status = status
            if status == 'Completed':
                self.progress = 1.0
            report = {
                'app': APP_ID, 'status': status, 'mode': self.mode, 'parameters': self.params,
                'started': self.params.get('Started'), 'duration': elapsed,
                'summary': self.summary, 'error': error,
                'tiles': {lbl: {k: v for k, v in st.items() if k not in ('bhm', 'n_points', 'qc_flag')}
                          for lbl, st in self.results.items()},
                'warnings': [l['text'] for l in self.logs if l['level'] == 'warning'
                             and l['id'] > self.log_start][-200:],
            }
        if os.path.isdir(out):
            try:
                rp = os.path.join(out, 'run_report.json')
                with open(rp, 'w', encoding='utf-8') as f:
                    json.dump(report, f, indent=2, default=str)
                outputs.append({'name': 'run_report.json', 'path': rp, 'kind': 'report', 'label': ''})
            except OSError as e:
                self.log('warning', f"Could not write run_report.json: {e}")
        with self.lock:
            self.outputs = outputs
            self.report = report
            n_fp = sum(len(st.get('bhm') or []) for st in self.results.values())
            n_bhm = sum(sum(v is not None for v in st.get('bhm') or []) for st in self.results.values())
            self.history.insert(0, {
                'started': self.params.get('Started'), 'duration': elapsed, 'status': status,
                'mode': MODE_NAMES.get(self.mode, self.mode), 'input': self.params.get('Input folder'),
                'output': out, 'footprints': n_fp, 'with_height': n_bhm,
                'tiles_ok': len(summary['success']) if summary else 0,
                'tiles_failed': len(summary['failed']) if summary else 0,
            })
            del self.history[50:]
        self.set_status(status)
        hub.publish('done', {'status': status, 'message': message, 'error': bool(error),
                             'snapshot': self.snapshot(with_logs=False)})

    def allowed_paths(self):
        with self.lock:
            paths = {o['path'] for o in self.outputs}
            paths.update(h['output'] for h in self.history)
        return paths


job = Job()


class LogStream:
    """Mirrors print() from main.py to the console and the browser log."""

    def __init__(self, original):
        self.original = original
        self.buf = ''

    def write(self, text):
        if self.original is not None:
            self.original.write(text)
        self.buf += text
        while '\n' in self.buf:
            line, self.buf = self.buf.split('\n', 1)
            line = line.strip()
            if line.startswith('WARNING:'):
                job.log('warning', line[len('WARNING:'):].strip())
            elif line:
                job.log('info', line)

    def flush(self):
        if self.original is not None:
            self.original.flush()


# ====================================================================== #
# NATIVE HELPERS
# ====================================================================== #
_dialog_lock = threading.Lock()


def pick_path(initial='', kind='folder'):
    """Browsers never expose real paths, so the server opens the Windows
    folder/file dialog on the user's behalf. kind: 'folder' or 'raster'."""
    import tkinter as tk
    from tkinter import filedialog
    if initial and os.path.isfile(initial):
        initial = os.path.dirname(initial)
    initialdir = initial if initial and os.path.isdir(initial) else None
    with _dialog_lock:
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        if kind == 'raster':
            path = filedialog.askopenfilename(
                parent=root, title='Select DTM raster', initialdir=initialdir,
                filetypes=[('DTM raster', '*.tif *.tiff *.vrt *.img *.asc'), ('All files', '*.*')])
        else:
            path = filedialog.askdirectory(parent=root, title='Select folder', initialdir=initialdir)
        root.destroy()
    return os.path.normpath(path) if path else ''


# ====================================================================== #
# HTTP
# ====================================================================== #
class Handler(BaseHTTPRequestHandler):
    server_version = 'GeneratorLOD1'

    def log_message(self, *args):
        pass

    # -- guards --------------------------------------------------------- #
    def _host_ok(self):
        # Rejects DNS-rebinding: only our own localhost origin may talk to us.
        port = self.server.server_address[1]
        if self.headers.get('Host', '') in (f'127.0.0.1:{port}', f'localhost:{port}'):
            return True
        self.send_error(403)
        return False

    def _send(self, body, ctype, code=200, extra=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code=200):
        self._send(json.dumps(data, default=str).encode(), 'application/json', code)

    # -- GET ------------------------------------------------------------ #
    def do_GET(self):
        if not self._host_ok():
            return
        path = urlparse(self.path).path
        if path == '/api/events':
            return self._events()
        if path == '/api/state':
            return self._json(job.snapshot())
        if path == '/api/ping':
            return self._json({'app': APP_ID})
        self._static(path)

    def _static(self, path):
        root = self.server.web_root
        target = (root / (path.lstrip('/') or 'index.html')).resolve()
        # Stay inside the web folder (no ../ traversal).
        if root not in target.parents or not target.is_file():
            return self.send_error(404)
        ctype = mimetypes.guess_type(target.name)[0] or 'application/octet-stream'
        if ctype.startswith('text/') or ctype in ('application/javascript', 'image/svg+xml'):
            ctype += '; charset=utf-8'
        # no-store: edits in ./web show on a simple refresh.
        self._send(target.read_bytes(), ctype)

    def _events(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()
        q = hub.subscribe()
        try:
            self._send_event('state', job.snapshot())
            while True:
                try:
                    event, data = q.get(timeout=15)
                    self._send_event(event, data)
                except queue.Empty:
                    self.wfile.write(b': ping\n\n')
                    self.wfile.flush()
        except OSError:
            pass  # browser tab closed
        finally:
            hub.unsubscribe(q)

    def _send_event(self, event, data):
        self.wfile.write(f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n".encode())
        self.wfile.flush()

    # -- POST ----------------------------------------------------------- #
    def do_POST(self):
        if not self._host_ok():
            return
        # JSON-only: a cross-site HTML form cannot send this without CORS preflight.
        if not self.headers.get('Content-Type', '').startswith('application/json'):
            return self.send_error(415)
        try:
            length = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(length) or b'{}')
        except (ValueError, json.JSONDecodeError):
            return self.send_error(400)

        path = urlparse(self.path).path
        try:
            return self._route(path, data)
        except Exception as e:
            return self._json({'ok': False, 'error': str(e)}, 500)

    def _route(self, path, data):
        if path == '/api/start':
            err = job.start(data)
            if err:
                job.log('error' if 'not found' in err else 'warning', err)
            return self._json({'ok': err is None, 'error': err}, 200 if err is None else 400)
        if path == '/api/stop':
            job.stop()
            return self._json({'ok': True})
        if path == '/api/browse':
            return self._json({'path': pick_path(str(data.get('initial', '')),
                                                 str(data.get('kind', 'folder')))})
        if path == '/api/clear-log':
            job.clear_log()
            return self._json({'ok': True})
        if path == '/api/inspect':
            folder = str(data.get('input', '')).strip()
            if not folder or not os.path.isdir(folder):
                return self._json({'ok': False, 'error': 'Folder not found'}, 404)
            return self._json({'ok': True, **inspect_folder(folder)})
        if path == '/api/footprints':
            las, gpkg = str(data.get('las', '')), str(data.get('gpkg', ''))
            # Only pairs that a folder inspection or the current run listed.
            known = {(t['las'], t['gpkg']) for t in job.snapshot(with_logs=False)['tiles']}
            for _, res in _inspect_cache.values():
                known.update((t['las'], t['gpkg']) for t in res['tiles'])
            if (las, gpkg) not in known:
                return self._json({'ok': False, 'error': 'Unknown tile'}, 404)
            return self._json({'ok': True, **footprint_geometry(las, gpkg)})
        if path == '/api/dtm-preview':
            target = str(data.get('path', ''))
            if target not in job.allowed_paths() or not os.path.isfile(target):
                return self._json({'ok': False}, 404)
            png, bounds = dtm_hillshade_png(target)
            return self._send(png, 'image/png', extra={'X-Bounds': json.dumps(bounds)})
        if path == '/api/reveal':
            target = str(data.get('path', ''))
            # Only files and folders this app produced may be revealed.
            if target in job.allowed_paths() and os.path.exists(target):
                if os.path.isdir(target):
                    subprocess.Popen(['explorer', os.path.normpath(target)])
                else:
                    subprocess.Popen(['explorer', '/select,', os.path.normpath(target)])
                return self._json({'ok': True})
            return self._json({'ok': False}, 404)
        self.send_error(404)


def existing_instance(port):
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/ping', timeout=1) as r:
            return json.load(r).get('app') == APP_ID
    except Exception:
        return False


def main():
    port = PREFERRED_PORT
    try:
        server = ThreadingHTTPServer((HOST, port), Handler)
    except OSError:
        if existing_instance(port):
            console(f"Generator LOD1 is already running. Opening http://localhost:{port}/")
            webbrowser.open(f'http://localhost:{port}/')
            return
        server = ThreadingHTTPServer((HOST, 0), Handler)
        port = server.server_address[1]
    server.daemon_threads = True
    server.web_root = web_root()

    sys.stdout = LogStream(sys.__stdout__)
    sys.stderr = LogStream(sys.__stderr__)
    job.log('info', "Generator LOD1 initialized.")
    job.log('info', "Ready. Choose the input and output folders, then start processing.")

    url = f'http://localhost:{port}/'
    console('=' * 56)
    console('  Generator LOD1 - local server')
    console(f'  UI      : {url}')
    console(f'  Web dir : {server.web_root}')
    console('  Close this window (or press Ctrl+C) to quit.')
    console('=' * 56)
    if '--no-browser' not in sys.argv:
        threading.Timer(0.6, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
