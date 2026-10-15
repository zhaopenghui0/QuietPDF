"""Persistence and PDF operations, independent of the desktop interface."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import uuid

import pymupdf as fitz


def document_key(path: str) -> str:
    """Include file identity so changed PDFs never silently inherit old ink."""
    source = Path(path).resolve()
    digest = hashlib.sha256(os.path.normcase(str(source)).encode('utf-8'))
    size = source.stat().st_size
    digest.update(str(size).encode())
    with source.open('rb') as stream:
        digest.update(stream.read(65536))
        stream.seek(max(0, size - 65536))
        digest.update(stream.read(65536))
    return digest.hexdigest()


class Library:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS documents (
                key TEXT PRIMARY KEY, path TEXT NOT NULL, state TEXT NOT NULL,
                touched TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS strokes (
                id TEXT PRIMARY KEY, doc TEXT NOT NULL, page INTEGER NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS strokes_page ON strokes(doc, page);
        ''')

    def state(self, key):
        row = self.db.execute('SELECT state FROM documents WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else {}

    def save_state(self, key, path, state):
        with self.db:
            self.db.execute('''INSERT INTO documents(key,path,state) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET state=excluded.state,
                path=excluded.path,touched=CURRENT_TIMESTAMP''',
                (key, path, json.dumps(state)))

    def recent(self):
        return self.db.execute('SELECT path FROM documents GROUP BY path ORDER BY MAX(touched) DESC LIMIT 12').fetchall()

    def strokes(self, key, page=None):
        query = 'SELECT payload FROM strokes WHERE doc=?'
        params = [key]
        if page is not None:
            query += ' AND page=?'
            params.append(page)
        return [json.loads(row[0]) for row in self.db.execute(query + ' ORDER BY rowid', params)]

    def put(self, key, stroke):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO strokes VALUES(?,?,?,?)',
                            (stroke['id'], key, stroke['page'], json.dumps(stroke)))

    def remove(self, stroke):
        with self.db:
            self.db.execute('DELETE FROM strokes WHERE id=?', (stroke['id'],))

    def close(self):
        self.db.close()

    def replace_strokes(self, key, removed, added):
        with self.db:
            self.db.executemany('DELETE FROM strokes WHERE id=?', [(s['id'],) for s in removed])
            self.db.executemany('INSERT OR REPLACE INTO strokes VALUES(?,?,?,?)',
                               [(s['id'], key, s['page'], json.dumps(s)) for s in added])


def new_stroke(page, points, color, width):
    return dict(id=uuid.uuid4().hex, page=page, points=points, color=color, width=width)


def distance_to_segment(point, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = dx * dx + dy * dy
    t = max(0, min(1, ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / length)) if length else 0
    return math.hypot(point[0] - a[0] - t * dx, point[1] - a[1] - t * dy)


def hit_stroke(stroke, point, width, height, radius):
    if stroke.get('type') == 'text':
        x, y, w, h = stroke['box']
        return x*width-radius <= point[0] <= (x+w)*width+radius and y*height-radius <= point[1] <= (y+h)*height+radius
    points = [(p[0] * width, p[1] * height) for p in stroke['points']]
    if len(points) == 1:
        return math.dist(point, points[0]) <= radius
    return any(distance_to_segment(point, a, b) <= radius for a, b in zip(points, points[1:]))


def erase_stroke(stroke, start, end, width, height, radius):
    """Clip a polyline against a swept circular eraser. None means unchanged."""
    if stroke.get('type') == 'text':
        x, y, w, h = stroke['box']
        lo, hi = 0., 1.
        for value, delta, lower, upper in ((start[0], end[0]-start[0], x*width-radius, (x+w)*width+radius),
                                           (start[1], end[1]-start[1], y*height-radius, (y+h)*height+radius)):
            if abs(delta) < 1e-12:
                if not lower <= value <= upper:
                    return None
            else:
                a, b = sorted(((lower-value)/delta, (upper-value)/delta))
                lo, hi = max(lo, a), min(hi, b)
        return [] if lo <= hi else None
    points = [(p[0]*width, p[1]*height) for p in stroke['points']]
    if len(points) == 1:
        return [] if distance_to_segment(points[0], start, end) <= radius else None
    ex, ey = end[0]-start[0], end[1]-start[1]
    length = math.hypot(ex, ey)
    fragments, current, changed = [], [], False
    for a, b in zip(points, points[1:]):
        dx, dy = b[0]-a[0], b[1]-a[1]
        squared = dx*dx+dy*dy
        if squared < 1e-16:
            continue
        intervals = []
        for center in (start, end):
            ux, uy = a[0]-center[0], a[1]-center[1]
            dot = ux*dx+uy*dy
            disc = dot*dot-squared*(ux*ux+uy*uy-radius*radius)
            if disc >= 0:
                intervals.append((max(0, (-dot-math.sqrt(disc))/squared),
                                  min(1, (-dot+math.sqrt(disc))/squared)))
        if length > 1e-8:
            # The capsule is the union of its endpoint circles and this rectangle.
            ux, uy = ex/length, ey/length
            lo, hi = 0., 1.
            for value, delta, lower, upper in (
                ((a[0]-start[0])*ux+(a[1]-start[1])*uy, dx*ux+dy*uy, 0, length),
                (-(a[0]-start[0])*uy+(a[1]-start[1])*ux, -dx*uy+dy*ux, -radius, radius)):
                if abs(delta) < 1e-12:
                    if not lower <= value <= upper:
                        hi = -1
                else:
                    t0, t1 = sorted(((lower-value)/delta, (upper-value)/delta))
                    lo, hi = max(lo, t0), min(hi, t1)
            intervals.append((lo, hi))
        cuts = sorted((lo, hi) for lo, hi in intervals if hi-lo > 1e-10)
        merged = []
        for lo, hi in cuts:
            if merged and lo <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        changed |= bool(merged)
        position = 0.
        for lo, hi in merged + [[1., 1.]]:
            if lo-position > 1e-10:
                p = (a[0]+dx*position, a[1]+dy*position)
                q = (a[0]+dx*lo, a[1]+dy*lo)
                if current and math.dist(current[-1], p) > 1e-6:
                    fragments.append(current)
                    current = []
                if not current:
                    current.append(p)
                current.append(q)
            if hi > lo and current:
                fragments.append(current)
                current = []
            position = hi
    if current:
        fragments.append(current)
    if not changed:
        return None
    return [new_stroke(stroke['page'], [[x/width, y/height] for x, y in part],
                       stroke['color'], stroke['width']) for part in fragments]


def render_page(doc, page_number, zoom, viewport_width, annotations=True, pixel_limit=4_000_000):
    page = doc[page_number]
    rect = page.rect
    requested = max(0.1, (viewport_width - 48) / rect.width) if zoom == 'fit' else float(zoom)
    # Bound the raster even at high zoom and on enormous architectural pages.
    scale = min(requested, math.sqrt(pixel_limit / (rect.width * rect.height)))
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB,
                          alpha=False, annots=annotations)
    return pix, scale, requested > scale + 0.001


def export_pdf(doc, strokes, destination):
    target = Path(destination).resolve()
    if doc.name and target == Path(doc.name).resolve():
        raise ValueError('请选择新文件名，保留原始 PDF。')
    temp = target.with_name('.' + target.stem + '-' + uuid.uuid4().hex + '.tmp.pdf')
    try:
        doc.save(str(temp), encryption=fitz.PDF_ENCRYPT_NONE)
        with fitz.open(temp) as output:
            for stroke in strokes:
                page = output[stroke['page']]
                rect = page.rect
                if stroke.get('type') == 'text':
                    x, y, w, h = stroke['box']
                    box = fitz.Rect(x*rect.width, y*rect.height, (x+w)*rect.width, (y+h)*rect.height) * page.derotation_matrix
                    color = tuple(int(stroke['color'][i:i+2], 16)/255 for i in (1, 3, 5))
                    annotation = page.add_freetext_annot(box, stroke['text'], fontsize=stroke['font_size'],
                        fontname='china-s', text_color=color, rotate=(360-page.rotation)%360)
                    annotation.set_info(title='轻阅', subject='文字标注', content=stroke['text'])
                    annotation.update()
                    continue
                points = [fitz.Point(x * rect.width, y * rect.height) * page.derotation_matrix
                          for x, y in stroke['points']]
                if len(points) == 1:
                    points.append(points[0] + fitz.Point(0.1, 0.1))
                annotation = page.add_ink_annot([[tuple(point) for point in points]])
                color = tuple(int(stroke['color'][i:i+2], 16) / 255 for i in (1, 3, 5))
                annotation.set_colors(stroke=color)
                annotation.set_border(width=stroke['width'])
                annotation.set_info(title='轻阅', subject='手绘标注')
                annotation.update()
                del annotation, page
            output.saveIncr()
        os.replace(temp, target)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass  # Preserve the original failure if Windows still holds a native PDF handle.
