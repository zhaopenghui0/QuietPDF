"""轻阅 — a small, local PDF reader with persistent mouse ink."""
from __future__ import annotations

import ctypes
import os
import multiprocessing
from pathlib import Path
import sys
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

import pymupdf as fitz
from PIL import Image, ImageTk
from tkinterdnd2 import COPY, DND_FILES, REFUSE_DROP, TkinterDnD

from reader_core import Library, document_key, export_pdf, hit_stroke, new_stroke, render_page
from continuous_view import ContinuousView

BASE = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent


class Reader(ContinuousView):
    def __init__(self, root, data_dir=None):
        self.root = root
        root.title('轻阅 · PDF 阅读与手绘')
        root.geometry('1180x840')
        root.minsize(840, 580)
        self.library = Library(Path(data_dir or BASE / '.reader-data') / 'library.sqlite3')
        self.doc = None
        self.path = self.key = ''
        self.page = 0
        self.zoom = 'fit'
        self.scale = 1
        self.image_width = self.image_height = 1
        self.origin = (0, 0)
        self.photo = None
        self.strokes = []
        self.undo_stack, self.redo_stack = [], []
        self.points = []
        self.segment_start = None
        self.render_job = self.save_job = None
        self.drop_job = None
        self.space_held = False
        self.left_held = False
        self.drag_cancelled = False
        self.toolbar_visible = False
        self.toolbar_hide_job = None
        self.layout = None
        self.layout_signature = None
        self.page_sizes = []
        self.tiles = {}
        self.page_strokes = {}
        self.renderer = None
        self.raster_token = 0
        self.poll_job = self.refresh_job = self.scroll_job = None
        self.scroll_target = None
        self.password = ''
        self.layout_busy = False
        self.stroke_page = None
        self.tool = tk.StringVar(value='hand')
        self.ink_color = '#e24b4b'
        self.ink_width = tk.StringVar(value='2.5')
        self.show_ink = tk.BooleanVar(value=True)
        self.page_text = tk.StringVar(value='1')
        self.zoom_text = tk.StringVar(value='适合宽度')
        self.status = tk.StringVar(value='打开一本 PDF，开始阅读。')
        self._build_ui()
        for target in (root, self.canvas):
            target.drop_target_register(DND_FILES)
            target.dnd_bind('<<Drop>>', self.drop_files)
        root.protocol('WM_DELETE_WINDOW', self.close)
        for key, callback in [('o', self.open_dialog), ('s', self.save_now),
                              ('z', self.undo), ('y', self.redo)]:
            root.bind('<Control-' + key + '>', lambda event, fn=callback: fn())
        root.bind('<Control-Shift-S>', lambda e: self.export())
        root.bind('<Prior>', lambda e: self.navigate(-1))
        root.bind('<Next>', lambda e: self.navigate(1))
        root.bind('<Escape>', lambda e: self.set_tool('hand'))
        root.bind('<KeyPress>', self.shortcut)
        root.bind('<KeyPress-space>', self.space_down)
        root.bind('<KeyRelease-space>', self.space_up)
        self._welcome()

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI', 10), background='#f7f8fa')
        style.configure('TButton', padding=(10, 7), background='#ffffff', borderwidth=1)
        style.map('TButton', background=[('active', '#e9eef8')])
        style.configure('Tool.TRadiobutton', padding=(10, 7))
        style.map('Tool.TRadiobutton', background=[('selected', '#dce8fc')])
        style.configure('Brand.TLabel', foreground='#245dcc', font=('Microsoft YaHei UI', 15, 'bold'))
        # Overlay controls never change the page geometry or the reading position.
        self.toolbar = ttk.Frame(self.root, relief='solid', borderwidth=1)
        top = ttk.Frame(self.toolbar, padding=(16, 10))
        top.pack(fill='x')
        ttk.Label(top, text='轻阅', style='Brand.TLabel').pack(side='left', padx=(0, 20))
        ttk.Button(top, text='打开 PDF', command=self.open_dialog).pack(side='left')
        recent = ttk.Menubutton(top, text='最近阅读 ▾', padding=(12, 7))
        recent.pack(side='left', padx=6)
        self.recent_menu = tk.Menu(recent, tearoff=False)
        recent.configure(menu=self.recent_menu)
        self.recent_menu.configure(postcommand=self.refresh_recent)
        ttk.Button(top, text='导出带标注 PDF', command=self.export).pack(side='right')
        tools = ttk.Frame(self.toolbar, padding=(16, 4, 16, 10))
        tools.pack(fill='x')
        for label, value in [('阅读 / 拖动  V', 'hand'), ('画笔  B', 'pen'), ('擦除整笔  E', 'erase')]:
            ttk.Radiobutton(tools, text=label, value=value, variable=self.tool,
                            style='Tool.TRadiobutton', command=self.tool_changed).pack(side='left', padx=(0, 4))
        ttk.Separator(tools, orient='vertical').pack(side='left', fill='y', padx=10)
        self.color_buttons = {}
        for color in ['#e24b4b', '#2563eb', '#18a078', '#e7a21b', '#242b36']:
            button = tk.Button(tools, bg=color, activebackground=color, width=2, relief='flat',
                               bd=2, command=lambda c=color: self.select_color(c))
            button.pack(side='left', padx=3, ipady=2)
            self.color_buttons[color] = button
        ttk.Label(tools, text=' 粗细').pack(side='left', padx=(10, 2))
        width = ttk.Combobox(tools, textvariable=self.ink_width, values=['1', '2.5', '4', '7'],
                             width=4, state='readonly')
        width.pack(side='left')
        self.undo_button = ttk.Button(tools, text='撤销', command=self.undo)
        self.undo_button.pack(side='left', padx=(12, 3))
        self.redo_button = ttk.Button(tools, text='重做', command=self.redo)
        self.redo_button.pack(side='left')
        ttk.Checkbutton(tools, text='显示标注', variable=self.show_ink, command=self.toggle_ink).pack(side='right')
        nav = ttk.Frame(self.toolbar, padding=(16, 7))
        nav.pack(fill='x')
        ttk.Button(nav, text='‹ 上页', command=lambda: self.navigate(-1)).pack(side='left')
        self.page_entry = ttk.Entry(nav, textvariable=self.page_text, width=5, justify='center')
        self.page_entry.pack(side='left', padx=(10, 4))
        self.page_entry.bind('<Return>', lambda e: self.go_page())
        self.page_count = ttk.Label(nav, text='/ — 页')
        self.page_count.pack(side='left', padx=(0, 10))
        ttk.Button(nav, text='下页 ›', command=lambda: self.navigate(1)).pack(side='left')
        self.title_label = ttk.Label(nav, text='本地阅读 · 自动记忆进度', foreground='#617087')
        self.title_label.pack(side='left', padx=20)
        zoom = ttk.Combobox(nav, textvariable=self.zoom_text, width=10, state='readonly',
                           values=['适合宽度', '50%', '75%', '100%', '125%', '150%', '200%', '300%'])
        zoom.pack(side='right')
        zoom.bind('<<ComboboxSelected>>', lambda e: self.change_zoom())
        area = ttk.Frame(self.root)
        area.pack(fill='both', expand=True)
        self.canvas = tk.Canvas(area, bg='#e9edf3', highlightthickness=0, yscrollincrement=1)
        self.canvas.grid(row=0, column=0, sticky='nsew')
        self.vertical = ttk.Scrollbar(area, orient='vertical', command=self.scroll_y)
        self.vertical.grid(row=0, column=1, sticky='ns')
        self.bottom_panel = ttk.Frame(self.root)
        self.horizontal = ttk.Scrollbar(self.bottom_panel, orient='horizontal', command=self.scroll_x)
        self.canvas.configure(yscrollcommand=self.on_yview, xscrollcommand=self.on_xview)
        area.rowconfigure(0, weight=1)
        area.columnconfigure(0, weight=1)
        footer = ttk.Frame(self.bottom_panel, padding=(16, 7))
        self.footer = footer
        footer.pack(fill='x')
        ttk.Label(footer, textvariable=self.status, foreground='#536176').pack(side='left')
        ttk.Label(footer, text='空格 + 左键拖动标注   ·   PgUp / PgDn 翻页', foreground='#7d899a').pack(side='right')
        self.annotation_button = ttk.Button(self.root, text='标注 ▾', command=self.toggle_toolbar)
        self.annotation_button.place(relx=1, x=-26, y=12, anchor='ne')
        self.annotation_button.lift()
        self.canvas.bind('<ButtonPress-1>', self.pointer_down)
        self.canvas.bind('<B1-Motion>', self.pointer_move)
        self.canvas.bind('<ButtonRelease-1>', self.pointer_up)
        self.canvas.bind('<MouseWheel>', self.wheel)
        self.canvas.bind('<Configure>', self.resize)
        self.canvas.bind('<Motion>', self.space_motion)
        self.canvas.bind('<Enter>', self.schedule_toolbar_hide)
        self.canvas.bind('<Leave>', self.leave_page)
        self.canvas.bind('<FocusOut>', self.cancel_pointer)
        self.root.bind('<Motion>', self.update_bottom_panel, add='+')
        self.root.bind('<Leave>', self.update_bottom_panel, add='+')
        self.select_color(self.ink_color)
        self.history_buttons()

    def on_xview(self, first, last):
        self.horizontal.set(first, last)
        if float(last) - float(first) < 0.999999:
            self.horizontal.pack(fill='x', before=self.footer)
        else:
            self.horizontal.pack_forget()

    def update_bottom_panel(self, event):
        x = event.x_root - self.root.winfo_rootx()
        y = event.y_root - self.root.winfo_rooty()
        height = self.root.winfo_height()
        reveal_height = self.bottom_panel.winfo_reqheight() if self.bottom_panel.winfo_ismapped() else 10
        if (not self.left_held and 0 <= x < self.root.winfo_width()
                and height - reveal_height <= y < height):
            self.bottom_panel.place(x=0, rely=1, anchor='sw', relwidth=1,
                                    width=-self.vertical.winfo_width())
            self.bottom_panel.lift()
        else:
            self.bottom_panel.place_forget()

    def toggle_toolbar(self):
        self.space_up()
        if self.toolbar_visible:
            self.hide_toolbar()
        else:
            self.toolbar_visible = True
            self.toolbar.place(x=8, y=54, relwidth=1, width=-16)
            self.toolbar.lift()
            self.annotation_button.configure(text='标注 ▴')
            self.annotation_button.lift()
        self.canvas.focus_set()

    def hide_toolbar(self):
        if self.toolbar_hide_job:
            self.root.after_cancel(self.toolbar_hide_job)
            self.toolbar_hide_job = None
        if self.toolbar_visible:
            self.toolbar.place_forget()
            self.toolbar_visible = False
            self.annotation_button.configure(text='标注 ▾')

    def schedule_toolbar_hide(self, event=None):
        if not self.toolbar_visible:
            return
        if self.toolbar_hide_job:
            self.root.after_cancel(self.toolbar_hide_job)
        def check_pointer():
            self.toolbar_hide_job = None
            widget = self.root.winfo_containing(self.root.winfo_pointerx(), self.root.winfo_pointery())
            while widget is not None:
                if widget in (self.toolbar, self.annotation_button):
                    return
                widget = getattr(widget, 'master', None)
            self.hide_toolbar()
        self.toolbar_hide_job = self.root.after(220, check_pointer)

    def space_down(self, event):
        if event.widget not in (self.canvas, self.root) or not self.doc:
            return
        if self.space_held:
            return 'break'
        self.stop_scroll()
        self.finish_stroke()
        if self.points:
            return 'break'
        self.hide_toolbar()
        if self.render_job:
            self.render()
        if not self.show_ink.get():
            self.show_ink.set(True)
            self.render()
        self.space_held = True
        self.canvas.configure(cursor='crosshair')
        self.status.set('标注就绪 · 按住鼠标左键拖动，松开左键或空格保存')
        x = self.canvas.winfo_pointerx() - self.canvas.winfo_rootx()
        y = self.canvas.winfo_pointery() - self.canvas.winfo_rooty()
        if self.left_held and not self.drag_cancelled and 0 <= x < self.canvas.winfo_width() and 0 <= y < self.canvas.winfo_height():
            self.select_page_at(self.canvas.canvasx(x), self.canvas.canvasy(y))
            point = (self.canvas.canvasx(x)-self.origin[0], self.canvas.canvasy(y)-self.origin[1])
            if self.inside(point):
                self.begin_stroke(point)
        return 'break'

    def space_up(self, event=None):
        if not self.space_held:
            return
        self.space_held = False
        self.drag_cancelled = self.left_held
        self.finish_stroke()
        self.canvas.configure(cursor={'hand': 'hand2', 'pen': 'crosshair', 'erase': 'circle'}[self.tool.get()])
        self.schedule_save()
        return 'break'

    def cancel_pointer(self, event=None):
        self.space_up()
        self.finish_stroke()
        self.left_held = False
        self.drag_cancelled = True

    def begin_stroke(self, point):
        self.stroke_page = self.page
        self.points = [[point[0]/self.image_width, point[1]/self.image_height]]
        self.segment_start = point

    def space_motion(self, event):
        if not self.space_held or not self.left_held or self.drag_cancelled or not self.doc:
            return
        point = self.canvas_point(event)
        if not self.inside(point):
            self.finish_stroke()
        elif not self.points:
            self.begin_stroke(point)
        else:
            self.extend_stroke(point)
        return 'break'

    def leave_page(self, event):
        if self.space_held:
            self.finish_stroke()

    def _welcome(self):
        c = self.canvas
        c.delete('all')
        w, h = max(c.winfo_width(), 800), max(c.winfo_height(), 400)
        c.create_text(w/2, h/2-60, text='安静阅读，随手圈画。', font=('Microsoft YaHei UI', 26, 'bold'), fill='#253a58')
        c.create_text(w/2, h/2, text='拖入 PDF 即可打开 · 按住空格和鼠标左键拖动即可圈画。',
                      font=('Microsoft YaHei UI', 12), fill='#67788f')
        c.create_text(w/2, h/2+36, text='自动保存笔迹和阅读位置  ·  随时隐藏标注  ·  连续阅读，按需加载',
                      font=('Microsoft YaHei UI', 11), fill='#8190a4')

    def refresh_recent(self):
        self.recent_menu.delete(0, 'end')
        for (path,) in self.library.recent():
            self.recent_menu.add_command(label=Path(path).name, command=lambda p=path: self.open_document(p))
        if self.recent_menu.index('end') is None:
            self.recent_menu.add_command(label='暂无记录', state='disabled')

    def open_dialog(self):
        path = filedialog.askopenfilename(parent=self.root, title='打开 PDF', filetypes=[('PDF 文档', '*.pdf')])
        if path:
            self.open_document(path)

    def drop_files(self, event):
        # Windows supplies a Tcl list: splitting on spaces breaks Chinese/space/braced paths.
        try:
            paths = self.root.tk.splitlist(event.data)
            pdfs = [str(Path(path).resolve()) for path in paths
                    if Path(path).suffix.lower() == '.pdf' and Path(path).is_file()]
        except (tk.TclError, OSError, ValueError):
            pdfs = []
        if not pdfs:
            self.status.set('请拖入本地 PDF 文件；当前阅读内容保持不变。')
            return REFUSE_DROP
        if self.drop_job:
            self.root.after_cancel(self.drop_job)
        # Return from the native drag loop before rendering or requesting a PDF password.
        def open_dropped():
            self.drop_job = None
            if self.open_document(pdfs[0]):
                self.canvas.focus_set()
                if len(pdfs) > 1:
                    self.status.set(f'已打开第一个 PDF（共拖入 {len(pdfs)} 个）；可继续拖入其他文件切换。')
        self.drop_job = self.root.after_idle(open_dropped)
        return COPY

    def open_document(self, path):
        self.stop_scroll()
        self.space_up()
        self.finish_stroke()
        if self.points or not self.save_state():
            return False
        candidate = None
        password = ''
        try:
            path = str(Path(path).resolve())
            candidate = fitz.open(path)
            if not candidate.is_pdf or not candidate.page_count:
                raise ValueError('请选择包含页面的 PDF 文件。')
            while candidate.needs_pass:
                password = simpledialog.askstring('文档密码', '请输入 PDF 密码：', show='*', parent=self.root)
                if password is None:
                    candidate.close()
                    return False
                if candidate.authenticate(password):
                    break
                messagebox.showerror('密码错误', '密码不正确，请重试。', parent=self.root)
            key = document_key(path)
            state = self.library.state(key)
            sizes = [(page.rect.width, page.rect.height) for page in candidate]
        except Exception as error:
            if candidate:
                candidate.close()
            messagebox.showerror('无法打开 PDF', str(error), parent=self.root)
            return False
        if self.doc:
            self.doc.close()
        self.doc, self.path, self.key = candidate, path, key
        self.password, self.page_sizes = password, sizes
        self.page = max(0, min(state.get('page', 0), self.doc.page_count - 1))
        self.zoom = state.get('zoom', 'fit')
        self.zoom_text.set('适合宽度' if self.zoom == 'fit' else f'{float(self.zoom)*100:g}%')
        self.show_ink.set(state.get('show_ink', True))
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.title_label.configure(text=Path(path).name[:48])
        self.root.title(f'{Path(path).name} — 轻阅')
        self.render((state.get('x', 0), state.get('page_offset', state.get('y', 0))))
        self.save_state()
        self.hide_toolbar()
        self.canvas.focus_set()
        return True

    def canvas_point(self, event):
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        self.select_page_at(x, y)
        return x-self.origin[0], y-self.origin[1]

    def inside(self, point):
        return 0 <= point[0] <= self.image_width and 0 <= point[1] <= self.image_height

    def pointer_down(self, event):
        self.canvas.focus_set()
        self.left_held = True
        self.drag_cancelled = False
        if not self.doc:
            return
        if self.space_held:
            return self.space_motion(event)
        if self.tool.get() == 'hand':
            self.stop_scroll()
            self.canvas.scan_mark(event.x, event.y)
        elif self.tool.get() == 'pen':
            point = self.canvas_point(event)
            if self.inside(point):
                self.begin_stroke(point)
        else:
            self.erase_at(event)

    def pointer_move(self, event):
        if not self.doc or not self.left_held or self.drag_cancelled:
            return
        if self.space_held:
            return self.space_motion(event)
        if self.tool.get() == 'hand':
            self.canvas.scan_dragto(event.x, event.y, gain=1)
            self.refresh_view()
            self.schedule_save()
        elif self.tool.get() == 'erase':
            self.erase_at(event)
        elif self.points:
            point = self.canvas_point(event)
            if self.points:
                self.extend_stroke(point)
            elif self.inside(point):
                self.begin_stroke(point)

    def extend_stroke(self, point):
        point = (max(0, min(self.image_width, point[0])), max(0, min(self.image_height, point[1])))
        previous = self.segment_start
        if abs(point[0]-previous[0])+abs(point[1]-previous[1]) < 1.5:
            return
        self.points.append([point[0]/self.image_width, point[1]/self.image_height])
        ox, oy = self.origin
        self.canvas.create_line(previous[0]+ox, previous[1]+oy, point[0]+ox, point[1]+oy,
                                width=max(1, float(self.ink_width.get())*self.scale),
                                fill=self.ink_color, capstyle='round', tags='draft')
        self.segment_start = point

    def pointer_up(self, event):
        if self.points:
            self.pointer_move(event)
        self.finish_stroke()
        self.left_held = False
        self.drag_cancelled = False
        self.schedule_save()

    def finish_stroke(self):
        if not self.points:
            return
        stroke = new_stroke(self.stroke_page, self.points, self.ink_color, float(self.ink_width.get()))
        try:
            self.library.put(self.key, stroke)
        except Exception as error:
            messagebox.showerror('笔迹保存失败', f'请检查磁盘空间和写入权限。\n{error}', parent=self.root)
            return
        self.points = []
        self.stroke_page = None
        self.canvas.delete('draft')
        self.strokes.append(stroke)
        self.record('add', stroke)
        self.draw_strokes()
        self.status.set('笔迹已自动保存 · 导出 PDF 可在其他软件中查看')

    def erase_at(self, event):
        if not self.show_ink.get():
            return
        point = self.canvas_point(event)
        for stroke in reversed(self.strokes):
            if hit_stroke(stroke, point, self.image_width, self.image_height,
                          8 + stroke['width']*self.scale/2):
                try:
                    self.library.remove(stroke)
                except Exception as error:
                    messagebox.showerror('擦除失败', str(error), parent=self.root)
                    return
                self.strokes.remove(stroke)
                self.record('remove', stroke)
                self.draw_strokes()
                self.status.set('已擦除一整笔 · Ctrl+Z 可撤销')
                break

    def record(self, operation, stroke):
        self.undo_stack.append((operation, stroke))
        self.undo_stack = self.undo_stack[-100:]
        self.redo_stack.clear()
        self.history_buttons()

    def undo(self):
        self.finish_stroke()
        self.replay(self.undo_stack, self.redo_stack, reverse=True)

    def redo(self):
        self.replay(self.redo_stack, self.undo_stack, reverse=False)

    def replay(self, source, target, reverse):
        if not source:
            return
        operation, stroke = source[-1]
        try:
            remove = (operation == 'add') if reverse else (operation == 'remove')
            self.library.remove(stroke) if remove else self.library.put(self.key, stroke)
        except Exception as error:
            messagebox.showerror('操作失败', str(error), parent=self.root)
            return
        target.append(source.pop())
        self.page_strokes[stroke['page']] = self.library.strokes(self.key, stroke['page'])
        self.page = stroke['page']
        self.render((self.canvas.xview()[0], 0))
        self.schedule_save()
        self.status.set('已撤销 · 自动保存' if reverse else '已重做 · 自动保存')

    def history_buttons(self):
        self.undo_button.configure(state='normal' if self.undo_stack else 'disabled')
        self.redo_button.configure(state='normal' if self.redo_stack else 'disabled')

    def select_color(self, color):
        self.finish_stroke()
        self.ink_color = color
        for value, button in self.color_buttons.items():
            button.configure(relief='sunken' if value == color else 'flat')

    def set_tool(self, tool):
        self.space_up()
        self.finish_stroke()
        self.tool.set(tool)
        self.tool_changed()

    def tool_changed(self):
        self.finish_stroke()
        if self.tool.get() != 'hand' and not self.show_ink.get():
            self.show_ink.set(True)
            self.render()
        self.canvas.configure(cursor={'hand': 'hand2', 'pen': 'crosshair', 'erase': 'circle'}[self.tool.get()])
        self.status.set({'hand': '阅读模式 · 拖动页面，滚轮滚动',
                         'pen': '画笔模式 · 按住鼠标左键自由圈画，松开自动保存',
                         'erase': '擦除模式 · 点击或划过笔迹，擦除本软件添加的一整笔'}[self.tool.get()])

    def toggle_ink(self):
        self.finish_stroke()
        if not self.show_ink.get():
            self.tool.set('hand')
            self.canvas.configure(cursor='hand2')
        self.render()
        self.schedule_save()

    def shortcut(self, event):
        if self.space_held:
            return 'break'
        if isinstance(event.widget, (ttk.Entry, ttk.Combobox, tk.Entry)):
            return
        if event.state & 0x4:
            return
        tool = {'b': 'pen', 'v': 'hand', 'e': 'erase'}.get(event.keysym.lower())
        if tool:
            self.set_tool(tool)

    def navigate(self, delta):
        if self.doc:
            self.go_to(self.page+delta)

    def go_page(self):
        if not self.doc:
            return
        try:
            page = int(self.page_text.get())-1
            if not 0 <= page < self.doc.page_count:
                raise ValueError()
            self.go_to(page)
            self.canvas.focus_set()
        except ValueError:
            self.page_text.set(str(self.page+1))

    def go_to(self, page):
        self.space_up()
        self.stop_scroll()
        page = max(0, min(page, self.doc.page_count-1))
        if page != self.page:
            self.finish_stroke()
            if self.points:
                return
            self.page = page
            self.render((0, 0))
            self.save_state()

    def change_zoom(self):
        self.finish_stroke()
        value = self.zoom_text.get()
        self.zoom = 'fit' if value == '适合宽度' else float(value.rstrip('%'))/100
        self.render()
        self.schedule_save()

    def resize(self, event):
        if self.render_job:
            self.root.after_cancel(self.render_job)
        self.render_job = self.root.after(200, self.render if self.doc else self._welcome)

    def scroll_y(self, *args):
        self.space_up()
        self.stop_scroll()
        if args[0] == 'scroll' and args[2] == 'units':
            self.canvas.yview_scroll(int(args[1])*40, 'units')
        else:
            self.canvas.yview(*args)
        self.refresh_view()
        self.schedule_save()

    def scroll_x(self, *args):
        self.canvas.xview(*args)
        self.schedule_save()

    def wheel(self, event):
        if not self.doc or self.points or self.space_held:
            return
        if event.state & 0x4:
            current = self.scale if self.zoom == 'fit' else self.zoom
            self.zoom = max(.25, min(3, current*(1.15 if event.delta > 0 else 1/1.15)))
            self.zoom_text.set(f'{self.zoom*100:.0f}%')
            if self.render_job:
                self.root.after_cancel(self.render_job)
            self.render_job = self.root.after(120, self.render)
        else:
            current = self.scroll_target if self.scroll_target is not None else self.canvas.canvasy(0)
            self.scroll_target = max(0, min(self.layout.height-self.canvas.winfo_height(), current-event.delta))
            if self.scroll_job is None:
                self.scroll_job = self.root.after(1, self.animate_scroll)
        self.schedule_save()

    def schedule_save(self):
        if self.save_job:
            self.root.after_cancel(self.save_job)
        self.save_job = self.root.after(450, self.save_state)

    def save_state(self):
        if self.save_job:
            self.root.after_cancel(self.save_job)
            self.save_job = None
        if self.doc:
            try:
                page, offset, x = self.view_anchor()
                self.library.save_state(self.key, self.path, dict(page=page, zoom=self.zoom,
                    x=x, y=offset, page_offset=offset, show_ink=self.show_ink.get()))
            except Exception as error:
                messagebox.showerror('阅读位置保存失败', str(error), parent=self.root)
                return False
        return True

    def save_now(self):
        self.finish_stroke()
        if self.save_state():
            self.status.set('阅读位置和笔迹已保存到本机 · 分享请使用右上角“导出带标注 PDF”')

    def export(self):
        if not self.doc:
            return
        self.finish_stroke()
        if self.points:
            return
        target = filedialog.asksaveasfilename(parent=self.root, title='导出带标注 PDF',
            initialfile=Path(self.path).stem+'-标注.pdf', defaultextension='.pdf', filetypes=[('PDF', '*.pdf')])
        if not target:
            return
        try:
            self.root.configure(cursor='watch')
            self.root.update_idletasks()
            export_pdf(self.doc, self.library.strokes(self.key), target)
            self.status.set('已导出：'+Path(target).name)
            messagebox.showinfo('导出完成', '所有笔迹已保存为标准 PDF 标注。\n'+target, parent=self.root)
        except Exception as error:
            messagebox.showerror('导出失败', str(error), parent=self.root)
        finally:
            self.root.configure(cursor='')

    def close(self):
        self.space_up()
        self.finish_stroke()
        if self.points or not self.save_state():
            return
        for job in (self.render_job, self.save_job, self.drop_job, self.toolbar_hide_job,
                    self.poll_job, self.refresh_job, self.scroll_job):
            if job:
                self.root.after_cancel(job)
        if self.doc:
            self.doc.close()
        if self.renderer:
            self.renderer.close()
        self.library.close()
        self.root.destroy()


def main():
    if os.name == 'nt':
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    root = TkinterDnD.Tk()
    try:
        app = Reader(root)
    except Exception as error:
        messagebox.showerror('启动失败', str(error), parent=root)
        root.destroy()
        return
    if len(sys.argv) > 1:
        root.after(150, lambda: app.open_document(sys.argv[1]) if app.doc is None else None)
    else:
        recent = app.library.recent()
        if recent and Path(recent[0][0]).is_file():
            root.after(150, lambda: app.open_document(recent[0][0]) if app.doc is None else None)
    root.mainloop()


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
