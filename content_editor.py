"""Original-content editing UI with disk-backed undo and explicit PDF saves."""
import os
import tempfile
import uuid
from pathlib import Path
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox
import pymupdf as fitz
from pdf_editing import apply_text, apply_image
from reader_core import document_key


class ContentEditor:
    def init_content_editor(self):
        self.content_temp = None
        self.content_snapshot = None
        self.saved_snapshot = None
        self.render_path = None
        self.source_stamp = None

    def content_dirty(self):
        return self.content_snapshot != self.saved_snapshot

    def load_edit_copy(self, path):
        doc = fitz.open(path)
        if doc.is_encrypted and not doc.authenticate(self.password):
            doc.close()
            raise ValueError('无法打开加密文档的编辑副本。')
        return doc

    def new_snapshot_path(self):
        if self.content_temp is None:
            self.content_temp = tempfile.TemporaryDirectory(prefix='quietpdf-edit-')
        return str(Path(self.content_temp.name)/(uuid.uuid4().hex+'.pdf'))

    def stop_renderer(self):
        if self.poll_job:
            self.root.after_cancel(self.poll_job)
            self.poll_job = None
        if self.renderer:
            self.renderer.close()
            self.renderer = None

    def switch_snapshot(self, path):
        candidate = self.load_edit_copy(path)
        self.stop_renderer()
        self.doc.close()
        self.doc = candidate
        self.content_snapshot = self.render_path = path
        self.layout_signature = None
        self.render()
        self.root.title(f'{"* " if self.content_dirty() else ""}{Path(self.path).name} — 轻阅')

    def change_content(self, change):
        if self.content_snapshot is None:
            before = self.new_snapshot_path()
            self.doc.save(before, encryption=fitz.PDF_ENCRYPT_KEEP)
            self.content_snapshot = self.saved_snapshot = before
        before = self.content_snapshot
        candidate = self.load_edit_copy(before)
        after = self.new_snapshot_path()
        try:
            change(candidate)
            candidate.save(after, encryption=fitz.PDF_ENCRYPT_KEEP, deflate=True)
        finally:
            candidate.close()
        self.switch_snapshot(after)
        self.record('content', dict(before=before, after=after))
        self.status.set('原文已修改，尚未写入 PDF · Ctrl+S 保存 · Ctrl+Z 撤销')

    def direct_edit(self, event):
        if (self.tool.get() not in ('hand', 'content') or self.space_held
                or self.alt_held or self.right_held or getattr(event, 'state', 0) & 0x400):
            return self.pointer_down(event)
        if not self.doc or not self.finish_text():
            return 'break'
        self.cancel_pointer()
        return self.edit_content_at(event)

    def edit_content_at(self, event):
        self.stop_scroll()
        point = self.canvas_point(event)
        if not self.inside(point):
            return 'break'
        page_number = self.page
        page = self.doc[page_number]
        location = fitz.Point(point[0]/self.image_width*page.rect.width,
                              point[1]/self.image_height*page.rect.height) * page.derotation_matrix
        for block in page.get_text('dict', flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES)['blocks']:
            for line in block.get('lines', []):
                for span in line['spans']:
                    if span['text'].strip() and location in fitz.Rect(span['bbox']):
                        if abs(line['dir'][0]-1)>0.01 or abs(line['dir'][1])>0.01:
                            self.status.set('暂不支持倾斜或竖排文字，请选择横排文字。')
                            return 'break'
                        self.original_text_editor(page_number, span)
                        return 'break'
        for item in reversed(page.get_image_info(xrefs=True)):
            if location in fitz.Rect(item['bbox']):
                menu = tk.Menu(self.root, tearoff=False)
                def perform(replace):
                    filename = None
                    if replace:
                        filename = filedialog.askopenfilename(parent=self.root, title='替换选中图片',
                            filetypes=[('图片', '*.png *.jpg *.jpeg *.bmp *.tif *.tiff'), ('所有文件', '*.*')])
                        if not filename:
                            return
                    try:
                        self.change_content(lambda doc: apply_image(doc, page_number, item, filename))
                    except Exception as error:
                        messagebox.showerror('图片修改失败', str(error), parent=self.root)
                menu.add_command(label='替换图片…', command=lambda: perform(True))
                menu.add_command(label='删除图片', command=lambda: perform(False))
                try:
                    menu.tk_popup(event.x_root, event.y_root)
                finally:
                    menu.grab_release()
                return 'break'
        self.status.set('此处没有可编辑文字或图片；扫描页中的文字需要 OCR，本版暂不支持。')
        return 'break'

    def original_text_editor(self, page_number, span):
        page = self.doc[page_number]
        box = fitz.Rect(span['bbox']) * page.rotation_matrix
        geom = self.layout.pages[page_number]
        sx, sy = geom['width']/page.rect.width, geom['height']/page.rect.height
        text = tk.Text(self.canvas, font=('Microsoft YaHei UI', -max(12, round(span['size']*sx))),
                       wrap='word', undo=True, borderwidth=0, highlightthickness=0,
                       padx=0, pady=0, background='white',
                       foreground='#%06x' % span.get('color', 0))
        text.bindtags((str(text), 'Text', 'all'))
        item = self.canvas.create_window(geom['x']+box.x0*sx, geom['y']+box.y0*sy, anchor='nw',
            window=text, width=max(24, box.width*sx+8), height=max(20, box.height*sy+4), tags='text-input')
        self.text_editor = text
        text.insert('1.0', span['text'])
        font = tkfont.Font(font=text['font'])
        def resize_editor(event=None):
            available = max(24, (page.rect.width-box.x0)*sx)
            lines = text.get('1.0', 'end-1c').split('\n')
            width = min(available, max(box.width*sx+8, max(map(font.measure, lines))+8))
            rows = sum(max(1, int((font.measure(line)+width-1)//width)) for line in lines)
            self.canvas.itemconfigure(item, width=width,
                height=max(box.height*sy+4, font.metrics('linespace')*rows+4))
        resize_editor()
        text.bind('<KeyRelease>', resize_editor)
        saving = False
        def dismiss(focus=True):
            self.text_editor = self.save_text = None
            self.canvas.delete(item)
            text.destroy()
            if focus:
                self.canvas.focus_set()
        def save(event=None):
            nonlocal saving
            if self.text_editor is not text or saving:
                return 'break'
            focus = event is None or event.type != tk.EventType.FocusOut
            value = text.get('1.0', 'end-1c')
            if value == span['text']:
                dismiss(focus)
                return 'break'
            # Rendering the committed snapshot must not recursively submit this editor.
            self.save_text = None
            self.text_editor = None
            saving = True
            try:
                self.change_content(lambda doc: apply_text(doc[page_number], span, value))
            except Exception as error:
                self.text_editor, self.save_text = text, save
                messagebox.showerror('文字修改失败', str(error), parent=self.root)
                text.focus_set()
                return 'break'
            finally:
                saving = False
            dismiss(focus)
            return 'break'
        self.save_text = save
        text.bind('<Return>', save)
        text.bind('<FocusOut>', save)
        text.bind('<Control-s>', lambda e: (save(), self.save_now() if self.text_editor is None else None, 'break')[-1])
        text.bind('<Shift-Return>', lambda e: (text.insert('insert', '\n'), 'break')[-1])
        text.bind('<Escape>', lambda e: (dismiss(), 'break')[-1])
        text.mark_set('insert', 'end-1c')
        text.focus_set()
        self.status.set('直接修改原文 · 点击别处或 Enter 应用 · Shift+Enter 换行 · Esc 取消 · Ctrl+S 保存 PDF')

    def confirm_content_changes(self):
        if not self.content_dirty():
            return True
        choice = messagebox.askyesnocancel('保存 PDF 修改', '原文字或图片有未保存修改，是否保存？', parent=self.root)
        if choice is None:
            return False
        return self.save_pdf_content() if choice else True

    def save_pdf_content(self, save_as=False):
        if not self.doc or not self.finish_text():
            return False
        target = self.path
        if save_as:
            target = filedialog.asksaveasfilename(parent=self.root, title='另存 PDF',
                initialfile=Path(self.path).stem+'-编辑.pdf', defaultextension='.pdf', filetypes=[('PDF', '*.pdf')])
            if not target:
                return False
        target = Path(target).resolve()
        same = target == Path(self.path).resolve()
        temporary = target.with_name('.'+target.stem+'-'+uuid.uuid4().hex+'.pdf')
        try:
            if same and self.source_stamp != (Path(self.path).stat().st_mtime_ns, Path(self.path).stat().st_size):
                raise ValueError('原文件已被其他程序修改，请使用“另存 PDF”。')
            self.save_state()
            self.doc.save(temporary, encryption=fitz.PDF_ENCRYPT_KEEP, deflate=True)
            check = self.load_edit_copy(temporary)
            check.close()
            # Keep our open document on a private snapshot so Windows can replace the original.
            if self.content_snapshot is None:
                snapshot = self.new_snapshot_path()
                self.doc.save(snapshot, encryption=fitz.PDF_ENCRYPT_KEEP)
                self.switch_snapshot(snapshot)
            self.stop_renderer()
            os.replace(temporary, target)
            old_key = self.key
            new_key = document_key(target)
            self.library.transfer_document(old_key, new_key, str(target), copy=not same)
            self.path, self.key = str(target), new_key
            self.source_stamp = (target.stat().st_mtime_ns, target.stat().st_size)
            self.saved_snapshot = self.content_snapshot
            # A save-as establishes a new annotation identity.
            if not same:
                self.undo_stack.clear()
                self.redo_stack.clear()
            self.layout_signature = None
            self.render()
            self.save_state()
            self.root.title(f'{target.name} — 轻阅')
            self.status.set('PDF 原文修改已保存 · 如需包含本机标注，请使用“导出标注 PDF”')
            return True
        except Exception as error:
            messagebox.showerror('保存 PDF 失败', str(error), parent=self.root)
            return False
        finally:
            if temporary.exists():
                temporary.unlink()

    def reset_content_editor(self):
        self.stop_renderer()
        if self.content_temp:
            self.content_temp.cleanup()
        self.init_content_editor()
