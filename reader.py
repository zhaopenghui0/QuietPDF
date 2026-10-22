"""轻阅 — a small, local PDF reader with persistent mouse ink."""
from __future__ import annotations

import ctypes
import math
import os
import multiprocessing
from pathlib import Path
import sys
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox, simpledialog

import pymupdf as fitz
from PIL import Image, ImageTk
from tkinterdnd2 import COPY, DND_FILES, REFUSE_DROP, TkinterDnD

from reader_core import Library, document_key, export_pdf, hit_stroke, new_stroke, render_page, erase_stroke
from continuous_view import ContinuousView
from content_editor import ContentEditor

BASE = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent


class Reader(ContentEditor, ContinuousView):
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
        self.alt_held = False
        self.eraser_previous = None
        self.eraser_before, self.eraser_after = {}, {}
        self.left_held = False
        self.right_held = False
        self.text_editor = None
        self.save_text = None
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
        self.init_content_editor()
        self.layout_busy = False
        self.stroke_page = None
        self.stroke_kind = 'ink'
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
        root.bind('<F1>', lambda event: self.toggle_shortcuts())
        root.bind('<KeyPress-space>', self.space_down)
        root.bind('<KeyRelease-space>', self.space_up)
        for key in ('Alt_L', 'Alt_R'):
            root.bind(f'<KeyPress-{key}>', self.alt_down)
            root.bind(f'<KeyRelease-{key}>', self.alt_up)
        self._welcome()

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI', 10), background='#f7f8fa')
        style.configure('TButton', padding=(6, 3), background='#ffffff', borderwidth=1)
        style.map('TButton', background=[('active', '#e9eef8')])
        style.configure('Tool.TRadiobutton', padding=(4, 3))
        style.configure('Compact.TButton', font=('Microsoft YaHei UI', 9), padding=(6, 3), width=4)
        style.map('Tool.TRadiobutton', background=[('selected', '#dce8fc')])
        style.configure('Brand.TLabel', foreground='#245dcc', font=('Microsoft YaHei UI', 15, 'bold'))
        # Overlay controls never change the page geometry or the reading position.
        self.toolbar = ttk.Frame(self.root, relief='solid', borderwidth=1)
        top = ttk.Frame(self.toolbar, padding=(8, 6, 8, 3))
        top.pack(fill='x')
        ttk.Button(top, text='打开 PDF', style='Compact.TButton', command=self.open_dialog).pack(side='left', expand=True, fill='x')
        recent = ttk.Menubutton(top, text='最近 ▾', padding=(4, 3))
        recent.pack(side='left', padx=(4, 0))
        self.recent_menu = tk.Menu(recent, tearoff=False)
        recent.configure(menu=self.recent_menu)
        self.recent_menu.configure(postcommand=self.refresh_recent)
        tools = ttk.Frame(self.toolbar, padding=(8, 3))
        tools.pack(fill='x')
        for label, value in [('阅读', 'hand'), ('编辑原文', 'content'), ('画笔', 'pen'), ('矩形', 'rect'), ('圆形', 'circle'), ('椭圆', 'ellipse'), ('整笔擦除', 'erase')]:
            ttk.Radiobutton(tools, text=label, value=value, variable=self.tool,
                            style='Tool.TRadiobutton', command=self.tool_changed).pack(fill='x', pady=1)
        ttk.Separator(tools).pack(fill='x', pady=5)
        palette = ttk.Frame(tools)
        palette.pack(fill='x')
        self.color_buttons = {}
        for color in ['#e24b4b', '#2563eb', '#18a078', '#e7a21b', '#242b36']:
            button = tk.Button(palette, bg=color, activebackground=color, width=1, height=1, font=('Microsoft YaHei UI', 8), relief='flat',
                               bd=1, command=lambda c=color: self.select_color(c))
            button.pack(side='left', padx=2, expand=True, fill='x')
            self.color_buttons[color] = button
        pen_options = ttk.Frame(tools)
        pen_options.pack(fill='x', pady=(5, 3))
        ttk.Label(pen_options, text='粗细').pack(side='left')
        width = ttk.Combobox(pen_options, textvariable=self.ink_width, values=['1', '2.5', '4', '7'],
                             width=4, state='readonly')
        width.pack(side='right')
        history = ttk.Frame(tools)
        history.pack(fill='x', pady=3)
        self.undo_button = ttk.Button(history, text='撤销', style='Compact.TButton', command=self.undo)
        self.undo_button.pack(side='left', fill='x', expand=True, padx=(0, 3))
        self.redo_button = ttk.Button(history, text='重做', style='Compact.TButton', command=self.redo)
        self.redo_button.pack(side='left', fill='x', expand=True)
        ttk.Checkbutton(tools, text='显示标注', variable=self.show_ink, command=self.toggle_ink).pack(anchor='w', pady=3)
        ttk.Separator(tools).pack(fill='x', pady=4)
        nav = ttk.Frame(self.toolbar, padding=(8, 3, 8, 6))
        nav.pack(fill='x')
        pages = ttk.Frame(nav)
        pages.pack(fill='x')
        ttk.Button(pages, text='‹', style='Compact.TButton', width=2, command=lambda: self.navigate(-1)).pack(side='left')
        self.page_entry = ttk.Entry(pages, textvariable=self.page_text, width=4, justify='center')
        self.page_entry.pack(side='left', padx=3)
        self.page_entry.bind('<Return>', lambda e: self.go_page())
        self.page_count = ttk.Label(pages, text='/ — 页')
        self.page_count.pack(side='left', padx=(0, 3))
        ttk.Button(pages, text='›', style='Compact.TButton', width=2, command=lambda: self.navigate(1)).pack(side='right')
        self.title_label = ttk.Label(nav, text='本地阅读 · 自动记忆进度', foreground='#617087')
        zoom = ttk.Combobox(nav, textvariable=self.zoom_text, width=10, state='readonly',
                           values=['适合宽度', '50%', '75%', '100%', '125%', '150%', '200%', '300%'])
        zoom.pack(fill='x', pady=5)
        zoom.bind('<<ComboboxSelected>>', lambda e: self.change_zoom())
        ttk.Button(nav, text='导出标注 PDF', style='Compact.TButton', command=self.export).pack(fill='x')
        save_row = ttk.Frame(nav)
        save_row.pack(fill='x', pady=(3, 0))
        ttk.Button(save_row, text='保存 PDF', style='Compact.TButton', command=self.save_pdf_content).pack(side='left', expand=True, fill='x')
        ttk.Button(save_row, text='另存 PDF', style='Compact.TButton', command=lambda: self.save_pdf_content(True)).pack(side='left', expand=True, fill='x')
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
        ttk.Label(footer, text='空格 + 左键标注 · Alt + 左键擦除 · Ctrl+Z 撤销', foreground='#7d899a').pack(side='right')
        self.annotation_button = ttk.Button(self.root, text='标注 ▾', style='Compact.TButton', width=6, command=self.show_toolbar)
        self.annotation_button.place(relx=1, x=-64, y=12, anchor='ne')
        self.annotation_button.lift()
        self.annotation_button.bind('<Enter>', self.show_toolbar)
        self.annotation_button.bind('<Leave>', self.schedule_toolbar_hide)
        self.root.bind('<Leave>', self.schedule_toolbar_hide, add='+')
        self.help_button = ttk.Button(self.root, text='?', style='Compact.TButton', width=2,
                                      command=self.toggle_shortcuts)
        self.help_button.place(relx=1, x=-26, y=12, anchor='ne')
        self.shortcuts_panel = ttk.Frame(self.root, padding=12, relief='solid', borderwidth=1)
        ttk.Label(self.shortcuts_panel, text='快捷键', font=('Microsoft YaHei UI', 10, 'bold')).grid(row=0, column=0, sticky='w', pady=(0, 8))
        ttk.Button(self.shortcuts_panel, text='×', style='Compact.TButton', width=2,
                   command=self.shortcuts_panel.place_forget).grid(row=0, column=1, sticky='e', pady=(0, 8))
        shortcuts = [
            ('画线标注', '空格 + 左键拖动'),
            ('局部擦除', 'Alt + 左键拖动'),
            ('矩形 / 圆形 / 椭圆', '选中工具后左键拖动'),
            ('输入文字', '左右键同时按下'),
            ('直接修改原文', '阅读模式双击文字'),
            ('保存文字 / 换行', 'Enter / Shift + Enter'),
            ('取消文字', 'Esc'),
            ('撤销 / 重做', 'Ctrl + Z / Ctrl + Y'),
            ('阅读 / 画笔 / 整笔擦', 'V / B / E'),
            ('翻页', 'PgUp / PgDn'),
            ('缩放', 'Ctrl + 鼠标滚轮'),
            ('打开 / 保存', 'Ctrl + O / Ctrl + S'),
            ('导出标注 PDF', 'Ctrl + Shift + S'),
            ('显示快捷键', 'F1 或右上角 ?'),
        ]
        for row, (label, keys) in enumerate(shortcuts, 1):
            ttk.Label(self.shortcuts_panel, text=label, foreground='#617087').grid(row=row, column=0, sticky='w', padx=(0, 20), pady=3)
            ttk.Label(self.shortcuts_panel, text=keys).grid(row=row, column=1, sticky='w', pady=3)
        ttk.Label(self.shortcuts_panel, text='画线、擦除：松开任意一个键即结束。', foreground='#617087').grid(
            row=len(shortcuts)+1, column=0, columnspan=2, sticky='w', pady=(10, 0))
        self.canvas.bind('<ButtonPress-1>', self.pointer_down)
        self.canvas.bind('<Double-Button-1>', self.direct_edit)
        self.canvas.bind('<B1-Motion>', self.pointer_move)
        self.canvas.bind('<ButtonRelease-1>', self.pointer_up)
        self.canvas.bind('<ButtonPress-3>', self.right_down)
        self.canvas.bind('<ButtonRelease-3>', self.right_up)
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
        self.shortcuts_panel.place_forget()
        self.alt_up()
        self.space_up()
        if self.toolbar_visible:
            self.hide_toolbar()
        else:
            self.toolbar_visible = True
            self.toolbar.place(relx=1, x=-26, y=44, anchor='ne')
            self.toolbar.lift()
            self.annotation_button.configure(text='标注 ▴')
            self.annotation_button.lift()
        self.canvas.focus_set()

    def show_toolbar(self, event=None):
        if self.left_held or self.space_held or self.alt_held:
            return
        if self.toolbar_hide_job:
            self.root.after_cancel(self.toolbar_hide_job)
            self.toolbar_hide_job = None
        self.shortcuts_panel.place_forget()
        self.toolbar_visible = True
        self.toolbar.place(relx=1, x=-26, y=44, anchor='ne')
        self.toolbar.lift()
        self.annotation_button.configure(text='标注 ▴')
        self.annotation_button.lift()

    def toggle_shortcuts(self):
        if self.shortcuts_panel.winfo_ismapped():
            self.shortcuts_panel.place_forget()
        else:
            self.hide_toolbar()
            self.shortcuts_panel.place(relx=1, x=-26, y=44, anchor='ne')
            self.shortcuts_panel.lift()
        return 'break'

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
        if self.alt_held:
            return 'break'
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
        self.canvas.configure(cursor={'hand': 'hand2', 'content': 'xterm', 'pen': 'crosshair', 'rect': 'crosshair', 'circle': 'crosshair', 'ellipse': 'crosshair', 'erase': 'circle'}[self.tool.get()])
        self.schedule_save()
        return 'break'

    def cancel_pointer(self, event=None):
        self.alt_up()
        self.space_up()
        self.finish_stroke()
        self.left_held = False
        self.right_held = False
        self.drag_cancelled = True

    def begin_stroke(self, point):
        self.stroke_kind = 'ink'
        self.stroke_page = self.page
        self.points = [[point[0]/self.image_width, point[1]/self.image_height]]
        self.segment_start = point

    def space_motion(self, event):
        if self.alt_held:
            self.show_eraser(event)
            return 'break'
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
        self.canvas.delete('eraser-cursor')
        self.eraser_previous = None
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
        if not self.finish_text():
            return False
        if not self.confirm_content_changes():
            return False
        self.alt_up()
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
        self.reset_content_editor()
        self.doc, self.path, self.key = candidate, path, key
        self.source_stamp = (Path(path).stat().st_mtime_ns, Path(path).stat().st_size)
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
        if not self.finish_text():
            return 'break'
        self.canvas.focus_set()
        self.left_held = True
        self.drag_cancelled = False
        if not self.doc:
            return
        if self.right_held or getattr(event, 'state', 0) & 0x400:
            return self.open_text_editor(event)
        if self.alt_held:
            return self.blackboard_erase(event)
        if self.space_held:
            return self.space_motion(event)
        if self.tool.get() == 'content':
            self.left_held = False
            return self.edit_content_at(event)
        if self.tool.get() == 'hand':
            self.stop_scroll()
            self.canvas.scan_mark(event.x, event.y)
        elif self.tool.get() in ('pen', 'rect', 'circle', 'ellipse'):
            self.stop_scroll()
            point = self.canvas_point(event)
            if self.inside(point):
                self.begin_stroke(point)
                if self.tool.get() != 'pen':
                    self.stroke_kind = self.tool.get()
        else:
            self.erase_at(event)

    def pointer_move(self, event):
        if not self.doc or not self.left_held or self.drag_cancelled:
            return
        if self.alt_held:
            return self.blackboard_erase(event)
        if self.space_held:
            return self.space_motion(event)
        if self.tool.get() == 'hand':
            self.canvas.scan_dragto(event.x, event.y, gain=1)
            self.refresh_view()
            self.schedule_save()
        elif self.tool.get() == 'erase':
            self.erase_at(event)
        elif self.points and self.stroke_kind in ('rect', 'circle', 'ellipse'):
            self.update_shape(event)
        elif self.points:
            point = self.canvas_point(event)
            if self.points:
                self.extend_stroke(point)
            elif self.inside(point):
                self.begin_stroke(point)

    def update_shape(self, event):
        ox, oy = self.origin
        ax, ay = self.segment_start
        bx = max(0, min(self.image_width, self.canvas.canvasx(event.x)-ox))
        by = max(0, min(self.image_height, self.canvas.canvasy(event.y)-oy))
        if self.stroke_kind == 'circle':
            radius = min(math.hypot(bx-ax, by-ay), ax, ay,
                         self.image_width-ax, self.image_height-ay)
            x0, x1 = ax-radius, ax+radius
            y0, y1 = ay-radius, ay+radius
        else:
            x0, x1 = sorted((ax, bx))
            y0, y1 = sorted((ay, by))
        if self.stroke_kind == 'rect':
            outline = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
        else:
            outline = [((x0+x1)/2+(x1-x0)/2*math.cos(i*math.tau/128),
                        (y0+y1)/2+(y1-y0)/2*math.sin(i*math.tau/128)) for i in range(129)]
        self.points = [[x/self.image_width, y/self.image_height] for x, y in outline]
        self.canvas.delete('draft')
        draw = self.canvas.create_rectangle if self.stroke_kind == 'rect' else self.canvas.create_oval
        draw(ox+x0, oy+y0, ox+x1, oy+y1, outline=self.ink_color,
             width=max(1, float(self.ink_width.get())*self.scale), tags='draft')

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
        if self.points or self.alt_held:
            self.pointer_move(event)
        self.finish_erase()
        self.finish_stroke()
        self.left_held = False
        self.drag_cancelled = False
        self.schedule_save()

    def right_down(self, event):
        self.right_held = True
        if self.left_held or getattr(event, 'state', 0) & 0x100:
            return self.open_text_editor(event)
        return 'break'

    def right_up(self, event):
        self.right_held = False
        return 'break'

    def open_text_editor(self, event):
        if not self.doc or self.text_editor is not None:
            return 'break'
        # Discard the just-started pen dot when the second mouse button arrives.
        if len(self.points) == 1:
            self.points = []
            self.stroke_page = None
            self.canvas.delete('draft')
        self.cancel_pointer()
        self.stop_scroll()
        point = self.canvas_point(event)
        if not self.inside(point):
            return 'break'
        page, geom = self.page, self.layout.pages[self.page]
        x, y = point[0]/geom['width'], point[1]/geom['height']
        if not self.show_ink.get():
            self.show_ink.set(True)
            self.render()
        font_size = 12
        font = tkfont.Font(family='Microsoft YaHei UI', size=-max(1, round(font_size*geom['scale'])))
        limit = max(1, min(320*geom['scale'], (1-x)*geom['width']-8))
        text = tk.Text(self.canvas, font=font, wrap='char', undo=True, borderwidth=0,
                       highlightthickness=0, padx=0, pady=0, background='white',
                       foreground=self.ink_color, insertbackground=self.ink_color)
        self.text_editor = text
        # Keep native text/IME bindings but exclude reader-wide keyboard shortcuts.
        text.bindtags((str(text), 'Text', 'all'))
        window = self.canvas.create_window(geom['x']+point[0], geom['y']+point[1],
                    window=text, anchor='nw', width=20, height=font.metrics('linespace')+4, tags='text-input')
        def wrapped(value):
            lines = []
            for paragraph in value.split('\n'):
                line = ''
                for char in paragraph:
                    if line and font.measure(line+char) > limit:
                        lines.append(line)
                        line = ''
                    line += char
                lines.append(line)
            return lines
        def resize_text(event=None):
            if not text.edit_modified():
                return
            text.edit_modified(False)
            lines = wrapped(text.get('1.0', 'end-1c'))
            width = min(limit+4, max(20, max(font.measure(line) for line in lines)+4))
            height = min(max(font.metrics('linespace')+4, len(lines)*font.metrics('linespace')+4),
                         max(font.metrics('linespace')+4, (1-y)*geom['height']))
            self.canvas.itemconfigure(window, width=width, height=height)
        def dismiss():
            self.text_editor = None
            self.save_text = None
            self.canvas.delete(window)
            text.destroy()
            self.left_held = self.right_held = False
        def save(event=None):
            if self.text_editor is not text:
                return 'break'
            value = text.get('1.0', 'end-1c').strip()
            if not value:
                dismiss()
                return 'break'
            # Persist explicit wrapping so PDF export and the canvas share line breaks.
            lines = wrapped(value)
            w = max(font.measure(line) for line in lines)+8
            h = max(font.metrics('linespace'), font_size*geom['scale']*1.4)*len(lines)+8
            if w > geom['width'] or h > geom['height']:
                self.status.set('文字超出一页范围，请减少内容后保存')
                text.focus_set()
                return 'break'
            box = [min(x, 1-w/geom['width']), min(y, 1-h/geom['height']), w/geom['width'], h/geom['height']]
            stroke = new_stroke(page, [box[:2]], self.ink_color, 1)
            stroke.update(type='text', text='\n'.join(lines), font_size=font_size, box=box)
            try:
                self.library.put(self.key, stroke)
            except Exception as error:
                self.status.set('文字保存失败：'+str(error))
                text.focus_set()
                return 'break'
            self.page_strokes[page] = self.library.strokes(self.key, page)
            self.strokes = self.page_strokes[self.page]
            self.record('add', stroke)
            self.show_ink.set(True)
            self.draw_strokes()
            self.status.set('文字标注已保存 · Ctrl+Z 撤销')
            dismiss()
            return 'break'
        self.save_text = save
        def complete(event=None):
            save()
            if self.text_editor is None:
                self.canvas.focus_set()
            return 'break'
        text.bind('<Return>', complete)
        text.bind('<Shift-Return>', lambda e: (text.insert('insert', '\n'), 'break')[-1])
        text.bind('<Escape>', lambda e: (dismiss(), self.canvas.focus_set(), 'break')[-1])
        text.bind('<Control-s>', complete)
        text.bind('<FocusOut>', save)
        text.bind('<<Modified>>', resize_text)
        text.focus_set()
        return 'break'

    def finish_text(self):
        if self.save_text is not None:
            self.save_text()
        return self.text_editor is None

    def finish_stroke(self):
        if not self.points:
            return
        stroke = new_stroke(self.stroke_page, self.points, self.ink_color, float(self.ink_width.get()))
        if self.stroke_kind in ('rect', 'circle', 'ellipse'):
            xs, ys = zip(*self.points)
            box = [min(xs), min(ys), max(xs)-min(xs), max(ys)-min(ys)]
            if box[2]*self.image_width < 2 or box[3]*self.image_height < 2:
                self.points = []
                self.stroke_page = None
                self.canvas.delete('draft')
                return
            stroke.update(type=self.stroke_kind, box=box)
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
        if stroke.get('type') in ('rect', 'circle', 'ellipse'):
            self.tool.set('hand')
            self.canvas.configure(cursor='hand2')
            self.status.set('形状已保存 · 已恢复阅读模式')

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

    def alt_down(self, event):
        if event.widget not in (self.canvas, self.root) or not self.doc:
            return
        if self.alt_held:
            return 'break'
        self.space_up()
        self.finish_stroke()
        if self.points:
            return 'break'
        self.stop_scroll()
        self.hide_toolbar()
        self.alt_held = True
        self.eraser_previous = None
        self.canvas.configure(cursor='circle')
        self.status.set('黑板擦 · Alt + 左键拖动擦除局部笔迹 · Ctrl+Z 撤销')
        return 'break'

    def alt_up(self, event=None):
        if not self.alt_held:
            return
        self.finish_erase()
        self.alt_held = False
        self.drag_cancelled = self.left_held
        self.canvas.delete('eraser-cursor')
        self.canvas.configure(cursor={'hand': 'hand2', 'content': 'xterm', 'pen': 'crosshair', 'rect': 'crosshair', 'circle': 'crosshair', 'ellipse': 'crosshair', 'erase': 'circle'}[self.tool.get()])
        return 'break'

    def show_eraser(self, event):
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        self.canvas.delete('eraser-cursor')
        self.canvas.create_oval(x-14, y-14, x+14, y+14, outline='#536176',
                                width=2, tags='eraser-cursor')

    def blackboard_erase(self, event):
        self.show_eraser(event)
        if not self.show_ink.get():
            return 'break'
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        start = self.eraser_previous or (x, y)
        removed, added = [], []
        # Use document coordinates so fast drags across page boundaries stay continuous.
        for page in self.layout.visible(min(start[1], y)-32, abs(y-start[1])+64):
            geom = self.layout.pages[page]
            if page not in self.page_strokes:
                self.page_strokes[page] = self.library.strokes(self.key, page)
            strokes = self.page_strokes[page]
            a = (start[0]-geom['x'], start[1]-geom['y'])
            b = (x-geom['x'], y-geom['y'])
            for stroke in strokes:
                parts = erase_stroke(stroke, a, b, geom['width'], geom['height'],
                                     14+stroke['width']*geom['scale']/2)
                if parts is not None:
                    removed.append(stroke)
                    added.extend(parts)
        if removed:
            try:
                self.library.replace_strokes(self.key, removed, added)
            except Exception as error:
                messagebox.showerror('擦除失败', str(error), parent=self.root)
                return 'break'
            for stroke in removed:
                if stroke['id'] in self.eraser_after:
                    del self.eraser_after[stroke['id']]
                else:
                    self.eraser_before[stroke['id']] = stroke
            self.eraser_after.update((s['id'], s) for s in added)
            for page in {s['page'] for s in removed}:
                self.page_strokes[page] = self.library.strokes(self.key, page)
            self.strokes = self.page_strokes[self.page]
            self.draw_strokes()
            self.canvas.tag_raise('eraser-cursor')
        self.eraser_previous = (x, y)
        return 'break'

    def finish_erase(self):
        if self.eraser_before:
            self.record('replace', dict(before=list(self.eraser_before.values()),
                                        after=list(self.eraser_after.values())))
            self.status.set('已擦除局部笔迹 · Ctrl+Z 可撤销本次擦除')
        self.eraser_before, self.eraser_after = {}, {}
        self.eraser_previous = None

    def record(self, operation, stroke):
        self.undo_stack.append((operation, stroke))
        self.undo_stack = self.undo_stack[-100:]
        self.redo_stack.clear()
        self.history_buttons()

    def undo(self):
        if not self.finish_text():
            return
        self.finish_erase()
        self.finish_stroke()
        self.replay(self.undo_stack, self.redo_stack, reverse=True)

    def redo(self):
        if not self.finish_text():
            return
        self.finish_erase()
        self.replay(self.redo_stack, self.undo_stack, reverse=False)

    def replay(self, source, target, reverse):
        if not source:
            return
        operation, stroke = source[-1]
        if operation == 'content':
            try:
                self.switch_snapshot(stroke['before'] if reverse else stroke['after'])
            except Exception as error:
                messagebox.showerror('撤销修改失败', str(error), parent=self.root)
                return
            target.append(source.pop())
            self.history_buttons()
            self.status.set('已撤销原文修改' if reverse else '已重做原文修改')
            return
        if operation == 'replace':
            removed, added = (stroke['after'], stroke['before']) if reverse else (stroke['before'], stroke['after'])
            try:
                self.library.replace_strokes(self.key, removed, added)
            except Exception as error:
                messagebox.showerror('操作失败', str(error), parent=self.root)
                return
            target.append(source.pop())
            for page in {s['page'] for s in removed+added}:
                self.page_strokes[page] = self.library.strokes(self.key, page)
            self.strokes = self.page_strokes[self.page]
            self.draw_strokes()
            self.history_buttons()
            self.status.set('已撤销擦除' if reverse else '已重做擦除')
            return
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
        self.alt_up()
        self.space_up()
        self.finish_stroke()
        self.tool.set(tool)
        self.tool_changed()

    def tool_changed(self):
        if not self.finish_text():
            return
        self.finish_stroke()
        if self.tool.get() != 'hand' and not self.show_ink.get():
            self.show_ink.set(True)
            self.render()
        self.canvas.configure(cursor={'hand': 'hand2', 'content': 'xterm', 'pen': 'crosshair', 'rect': 'crosshair', 'circle': 'crosshair', 'ellipse': 'crosshair', 'erase': 'circle'}[self.tool.get()])
        self.status.set({'hand': '阅读模式 · 拖动页面，滚轮滚动',
                         'pen': '画笔模式 · 按住鼠标左键自由圈画，松开自动保存',
                         'content': '编辑原文 · 点击文字修改，点击图片替换或删除 · Ctrl+S 保存 PDF',
                         'rect': '矩形 · 左键拖动确定大小，松开保存',
                         'circle': '圆形 · 按下确定圆心，拖动调整半径，松开保存',
                         'ellipse': '椭圆 · 左键拖动确定大小，松开保存',
                         'erase': '擦除模式 · 点击或划过笔迹，擦除本软件添加的一整笔'}[self.tool.get()])

    def toggle_ink(self):
        self.finish_stroke()
        if not self.show_ink.get():
            self.tool.set('hand')
            self.canvas.configure(cursor='hand2')
        self.render()
        self.schedule_save()

    def shortcut(self, event):
        if self.space_held or self.alt_held:
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
        if not self.doc or self.points or self.space_held or self.alt_held:
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
        if not self.finish_text():
            return
        if self.content_dirty():
            self.save_pdf_content()
            return
        self.finish_stroke()
        if self.save_state():
            self.status.set('阅读位置和笔迹已保存到本机 · 分享请使用右上角“导出带标注 PDF”')

    def export(self):
        if not self.doc:
            return
        if not self.finish_text():
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
        if not self.finish_text():
            return
        if not self.confirm_content_changes():
            return
        self.alt_up()
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
            self.renderer = None
        if self.content_temp:
            self.content_temp.cleanup()
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
