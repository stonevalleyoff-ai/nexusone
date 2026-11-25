"""Générateur de PDF texte minimal (sans dépendance), pour fabriquer des documents de test.

Police Helvetica en WinAnsiEncoding : accents français et « € » pris en charge.
"""
from __future__ import annotations

from pathlib import Path


def _escape(line: str) -> bytes:
    raw = line.encode('cp1252', errors='replace')
    return raw.replace(b'\\', b'\\\\').replace(b'(', b'\\(').replace(b')', b'\\)')


def write_pdf(path: Path, pages: list[list[str]], title: str = '') -> None:
    """Écrit un PDF : une liste de pages, chaque page étant une liste de lignes."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font = add(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>')
    bold = add(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>')
    pages_id = len(objects) + 1 + 2 * len(pages)  # réservé après les pages
    page_ids = []
    for lines in pages:
        stream = [b'BT', b'/F1 11 Tf', b'14 TL', b'50 800 Td']
        for index, line in enumerate(lines):
            if line.startswith('# '):
                stream += [b'/F2 15 Tf', b'(' + _escape(line[2:]) + b') Tj', b'T*', b'/F1 11 Tf']
            else:
                stream += [b'(' + _escape(line) + b') Tj', b'T*']
        stream.append(b'ET')
        content = b'\n'.join(stream)
        content_id = add(b'<< /Length %d >>\nstream\n' % len(content) + content + b'\nendstream')
        page_ids.append(add(b'<< /Type /Page /Parent %d 0 R /MediaBox [0 0 595 842] '
                            b'/Resources << /Font << /F1 %d 0 R /F2 %d 0 R >> >> /Contents %d 0 R >>'
                            % (pages_id, font, bold, content_id)))
    kids = b' '.join(b'%d 0 R' % i for i in page_ids)
    assert add(b'<< /Type /Pages /Kids [%s] /Count %d >>' % (kids, len(page_ids))) == pages_id
    info = add(b'<< /Title (' + _escape(title) + b') /Producer (NEXUS tests) >>')
    catalog = add(b'<< /Type /Catalog /Pages %d 0 R >>' % pages_id)
    out = bytearray(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n')
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b'%d 0 obj\n' % number + body + b'\nendobj\n'
    xref = len(out)
    out += b'xref\n0 %d\n0000000000 65535 f \n' % (len(objects) + 1)
    out += b''.join(b'%010d 00000 n \n' % o for o in offsets)
    out += b'trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n' % (
        len(objects) + 1, catalog, info, xref)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
