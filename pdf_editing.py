"""Transactional changes to original PDF content (not annotation overlays)."""
import html
import pymupdf as fitz


def apply_text(page, span, value):
    if any(a.type[0] == fitz.PDF_ANNOT_REDACT for a in (page.annots() or [])):
        raise ValueError('该页含有未应用的涂黑标记，暂不支持编辑。')
    box = fitz.Rect(span['bbox'])
    # Keep the removal rectangle inside the selected span to avoid adjacent lines.
    cut = box + (0.1, 0.1, -0.1, -0.1)
    page.add_redact_annot(cut, fill=False, cross_out=False)
    page.apply_redactions(images=0, graphics=0, text=0)
    if value.strip():
        css = ('* {margin:0;padding:0} div {font-family:sans-serif;'
               f'font-size:{span["size"]}pt;color:#{span.get("color", 0):06x};line-height:1.1;white-space:pre-wrap}}')
        spare, _ = page.insert_htmlbox(box, '<div>'+html.escape(value).replace('\n', '<br>')+'</div>',
                                       css=css, scale_low=0.65)
        if spare < 0:
            raise ValueError('新文字超出原文字区域，请减少内容后再保存。')


def apply_image(doc, page_number, image, replacement=None):
    xref = image.get('xref', 0)
    if not xref:
        raise ValueError('这是一张内嵌图片，暂不支持直接替换。')
    uses = sum(sum(item['xref'] == xref for item in p.get_image_info(xrefs=True)) for p in doc)
    if uses != 1:
        raise ValueError('该图片被多处共用，暂不支持单独修改，以免影响其他位置。')
    page = doc[page_number]
    if replacement:
        page.replace_image(xref, filename=str(replacement))
    else:
        page.delete_image(xref)
