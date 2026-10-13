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


def new_stroke(page, points, color, width):
    return dict(id=uuid.uuid4().hex, page=page, points=points, color=color, width=width)


def distance_to_segment(point, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = dx * dx + dy * dy
    t = max(0, min(1, ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / length)) if length else 0
    return math.hypot(point[0] - a[0] - t * dx, point[1] - a[1] - t * dy)


def hit_stroke(stroke, point, width, height, radius):
    points = [(p[0] * width, p[1] * height) for p in stroke['points']]
    if len(points) == 1:
        return math.dist(point, points[0]) <= radius
    return any(distance_to_segment(point, a, b) <= radius for a, b in zip(points, points[1:]))


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
