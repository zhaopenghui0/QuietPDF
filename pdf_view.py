"""Continuous page geometry and an isolated PDF raster worker."""
from bisect import bisect_right
import math
import multiprocessing as mp
from queue import Empty

import pymupdf as fitz
from reader_core import render_page


class PageLayout:
    def __init__(self, sizes, zoom, viewport_width, pixel_limit=3_000_000):
        self.pages = []
        self.tops = []
        top = 20
        widths = []
        for width, height in sizes:
            scale = max(.1, (viewport_width-48)/width) if zoom == 'fit' else float(zoom)
            scale = min(scale, math.sqrt(pixel_limit/(width*height)))
            w, h = math.ceil(width*scale), math.ceil(height*scale)
            self.tops.append(top)
            self.pages.append(dict(y=top, width=w, height=h, scale=scale))
            widths.append(w)
            top += h + 24
        self.height = top
        self.width = max(viewport_width, max(widths, default=0)+48)
        for page in self.pages:
            page['x'] = (self.width-page['width'])/2

    def page_at(self, y):
        return max(0, min(len(self.pages)-1, bisect_right(self.tops, y)-1))

    def visible(self, top, height):
        return list(range(self.page_at(top), self.page_at(top+height)+1))


def raster_worker(requests, results):
    doc = None
    identity = None
    try:
        while True:
            request = requests.get()
            if request is None:
                break
            try:
                if identity != request['key']:
                    if doc:
                        doc.close()
                    doc = fitz.open(request['path'])
                    if doc.needs_pass and not doc.authenticate(request['password']):
                        raise ValueError('无法解锁 PDF 页面。')
                    identity = request['key']
                pix, _, _ = render_page(doc, request['page'], request['zoom'], request['width'],
                                         request['annotations'], pixel_limit=3_000_000)
                result = dict(token=request['token'], page=request['page'],
                              width=pix.width, height=pix.height, samples=pix.samples)
                del pix
                fitz.TOOLS.store_shrink(100)
                results.put(result)
            except Exception as error:
                results.put(dict(token=request['token'], page=request['page'], error=str(error)))
    finally:
        if doc:
            doc.close()


class PageRenderer:
    """One in-flight job prevents a fast scrollbar drag from queuing obsolete pages."""
    def __init__(self):
        context = mp.get_context('spawn')
        self.requests = context.Queue(maxsize=1)
        self.results = context.Queue(maxsize=1)
        self.process = context.Process(target=raster_worker, args=(self.requests, self.results), daemon=True)
        self.process.start()
        self.busy = False

    def submit(self, request):
        if not self.busy:
            self.requests.put_nowait(request)
            self.busy = True

    def poll(self):
        try:
            result = self.results.get_nowait()
            self.busy = False
            return result
        except Empty:
            if self.busy and not self.process.is_alive():
                self.busy = False
                return dict(error='页面渲染进程已退出，请重新打开 PDF。', token=-1, page=-1)
            return None

    def close(self):
        # Worker only reads PDFs; stopping it cannot lose annotations or reader state.
        if self.process.is_alive():
            self.process.terminate()
        self.process.join(timeout=1)
        for queue in (self.requests, self.results):
            queue.cancel_join_thread()
            queue.close()
