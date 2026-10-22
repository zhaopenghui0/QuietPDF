"""Virtualized Tk canvas view; images are produced outside the UI process."""
from PIL import Image, ImageTk
from tkinter import messagebox
from pdf_view import PageLayout, PageRenderer


class ContinuousView:
    def render(self, position=None):
        if not self.finish_text():
            return
        if not self.doc:
            return
        if self.render_job:
            self.root.after_cancel(self.render_job)
            self.render_job = None
        self.finish_stroke()
        if self.points:
            return
        self.stop_scroll()
        anchor_page, offset, x_fraction = self.view_anchor() if position is None else (self.page, position[1], position[0])
        try:
            width = max(300, self.canvas.winfo_width())
            signature = (self.key, self.zoom, width, self.show_ink.get())
            self.layout_busy = True
            if signature != self.layout_signature:
                self.canvas.delete('all')
                self.tiles.clear()
                self.page_strokes.clear()
                self.layout = PageLayout(self.page_sizes, self.zoom, width)
                self.layout_signature = signature
                self.raster_token += 1
            self.canvas.configure(scrollregion=(0, 0, self.layout.width, self.layout.height))
            anchor_page = max(0, min(anchor_page, len(self.layout.pages)-1))
            geom = self.layout.pages[anchor_page]
            self.canvas.xview_moveto(x_fraction)
            self.canvas.yview_moveto((geom['y']+offset*geom['height'])/self.layout.height)
            self.layout_busy = False
            self.page_strokes[self.page] = self.library.strokes(self.key, self.page)
            self.refresh_view()
            self.draw_strokes()
            self.history_buttons()
            self.status.set('连续阅读 · 空格 + 左键拖动标注')
        except Exception as error:
            self.layout_busy = False
            messagebox.showerror('页面显示失败', str(error), parent=self.root)

    def view_anchor(self):
        if not self.layout:
            return self.page, 0, 0
        top = self.canvas.canvasy(0)
        page = self.layout.page_at(top+1)
        geom = self.layout.pages[page]
        return page, (top-geom['y'])/geom['height'], self.canvas.xview()[0]

    def activate_page(self, page):
        self.page = page
        geom = self.layout.pages[page]
        self.origin = geom['x'], geom['y']
        self.image_width, self.image_height, self.scale = geom['width'], geom['height'], geom['scale']
        if page not in self.page_strokes:
            self.page_strokes[page] = self.library.strokes(self.key, page)
        self.strokes = self.page_strokes[page]
        self.page_text.set(str(page+1))
        self.page_count.configure(text=f'/ {self.doc.page_count} 页')

    def select_page_at(self, x, y):
        if not self.layout:
            return
        page = self.layout.page_at(y)
        if page != self.page:
            self.finish_stroke()
            if not self.points:
                self.activate_page(page)

    def on_yview(self, first, last):
        self.vertical.set(first, last)
        if self.doc and self.layout and not self.layout_busy and self.refresh_job is None:
            self.refresh_job = self.root.after_idle(self.refresh_view)

    def refresh_view(self):
        if self.refresh_job:
            self.root.after_cancel(self.refresh_job)
            self.refresh_job = None
        if not self.doc or not self.layout:
            return
        top, height = self.canvas.canvasy(0), self.canvas.winfo_height()
        visible = self.layout.visible(top, height)
        wanted = set(visible)
        # Visible pages take priority; prefetch at most one on each side, under 8M pixels.
        pixels = sum(self.layout.pages[i]['width']*self.layout.pages[i]['height'] for i in wanted)
        for neighbor in (visible[-1]+1, visible[0]-1):
            if 0 <= neighbor < self.doc.page_count:
                geom = self.layout.pages[neighbor]
                cost = geom['width']*geom['height']
                if pixels+cost <= 8_000_000:
                    wanted.add(neighbor)
                    pixels += cost
        for page in list(self.tiles):
            if page not in wanted:
                self.canvas.delete(f'tile-{page}')
                self.canvas.delete(f'ink-{page}')
                del self.tiles[page]
                if page != self.stroke_page:
                    self.page_strokes.pop(page, None)
        for page in sorted(wanted):
            if page in self.tiles:
                continue
            geom = self.layout.pages[page]
            x, y, w, h = geom['x'], geom['y'], geom['width'], geom['height']
            tag = f'tile-{page}'
            self.canvas.create_rectangle(x+3, y+3, x+w+3, y+h+3, fill='#d6dce6', outline='', tags=tag)
            self.canvas.create_rectangle(x, y, x+w, y+h, fill='white', outline='', tags=tag)
            label = self.canvas.create_text(x+w/2, y+min(h/2, 140), text=f'第 {page+1} 页 · 加载中',
                                            fill='#8896a8', tags=tag)
            self.tiles[page] = dict(photo=None, label=label, error=False)
            if page not in self.page_strokes:
                self.page_strokes[page] = self.library.strokes(self.key, page)
            self.draw_page_strokes(page)
        if not self.points and not self.space_held:
            self.activate_page(self.layout.page_at(top+1))
        if self.renderer is None:
            self.renderer = PageRenderer()
        self.request_raster(visible)
        if self.poll_job is None:
            self.poll_job = self.root.after(16, self.poll_raster)

    def request_raster(self, visible=None):
        if not self.renderer or self.renderer.busy or not self.renderer.process.is_alive():
            return
        visible = visible or self.layout.visible(self.canvas.canvasy(0), self.canvas.winfo_height())
        priority = visible + [i for i in self.tiles if i not in visible]
        for page in priority:
            tile = self.tiles.get(page)
            if tile and tile['photo'] is None and not tile['error']:
                self.renderer.submit(dict(token=self.raster_token, key=(self.key, self.render_path), path=self.render_path or self.path,
                    password=self.password, page=page, zoom=self.zoom,
                    width=self.layout_signature[2], annotations=self.show_ink.get()))
                return

    def poll_raster(self):
        self.poll_job = None
        result = self.renderer.poll()
        if result and result['token'] == -1:
            self.status.set(result['error'])
        if result and result['token'] == self.raster_token:
            page = result['page']
            tile = self.tiles.get(page)
            if tile:
                if 'error' in result:
                    tile['error'] = True
                    self.canvas.itemconfigure(tile['label'], text='此页无法显示：'+result['error'])
                else:
                    image = Image.frombytes('RGB', (result['width'], result['height']), result['samples'])
                    tile['photo'] = ImageTk.PhotoImage(image, master=self.root)
                    image.close()
                    geom = self.layout.pages[page]
                    self.canvas.delete(tile['label'])
                    self.canvas.create_image(geom['x'], geom['y'], anchor='nw', image=tile['photo'], tags=f'tile-{page}')
                    self.canvas.tag_raise(f'ink-{page}')
                    self.canvas.tag_raise('draft')
        self.request_raster()
        if self.renderer.busy:
            self.poll_job = self.root.after(16, self.poll_raster)

    def draw_strokes(self):
        self.canvas.delete('ink')
        for page in self.tiles:
            self.draw_page_strokes(page)

    def draw_page_strokes(self, page):
        self.canvas.delete(f'ink-{page}')
        if not self.show_ink.get():
            return
        geom = self.layout.pages[page]
        for stroke in self.page_strokes.get(page, []):
            coords = [(geom['x']+x*geom['width'], geom['y']+y*geom['height']) for x, y in stroke['points']]
            width = max(1, stroke['width']*geom['scale'])
            tags = ('ink', f'ink-{page}')
            if stroke.get('type') in ('rect', 'circle', 'ellipse'):
                x, y, w, h = stroke['box']
                draw = self.canvas.create_rectangle if stroke['type'] == 'rect' else self.canvas.create_oval
                draw(geom['x']+x*geom['width'], geom['y']+y*geom['height'],
                     geom['x']+(x+w)*geom['width'], geom['y']+(y+h)*geom['height'],
                     outline=stroke['color'], width=width, tags=tags)
                continue
            if stroke.get('type') == 'text':
                x, y, w, h = stroke['box']
                self.canvas.create_text(geom['x']+x*geom['width'], geom['y']+y*geom['height'],
                    text=stroke['text'], anchor='nw', fill=stroke['color'],
                    font=('Microsoft YaHei UI', -max(1, round(stroke['font_size']*geom['scale']))), tags=tags)
                continue
            if len(coords) == 1:
                x, y = coords[0]
                self.canvas.create_oval(x-width/2, y-width/2, x+width/2, y+width/2,
                                        fill=stroke['color'], outline='', tags=tags)
            else:
                self.canvas.create_line(*[v for p in coords for v in p], fill=stroke['color'],
                                        width=width, capstyle='round', joinstyle='round', tags=tags)

    def stop_scroll(self):
        if self.scroll_job:
            self.root.after_cancel(self.scroll_job)
        self.scroll_job = None
        self.scroll_target = None

    def animate_scroll(self):
        self.scroll_job = None
        if self.scroll_target is None or not self.layout:
            return
        current = self.canvas.canvasy(0)
        delta = self.scroll_target-current
        top = self.scroll_target if abs(delta) < 2 else current+delta*.35
        self.canvas.yview_moveto(top/self.layout.height)
        self.refresh_view()
        if abs(delta) >= 2:
            self.scroll_job = self.root.after(16, self.animate_scroll)
        else:
            self.scroll_target = None
