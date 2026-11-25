"""Fabrique des « scans » de test : une page de texte rendue en image, sans dépendance Python.

Le rendu passe par pdftoppm (poppler, déjà requis par NEXUS). Sert aux tests de la
reconnaissance de texte et à son banc de mesure.
"""
from __future__ import annotations

from pathlib import Path
import struct
import subprocess
import tempfile
import zlib

Gray = tuple[int, int, bytes]     # largeur, hauteur, un octet de gris par point


def rasterize(pdf: Path, page: int = 1, dpi: int = 150) -> Gray:
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(['pdftoppm', '-r', str(dpi), '-gray', '-singlefile', '-f', str(page), '-l', str(page),
                        str(pdf), str(Path(tmp) / 'page')], check=True, capture_output=True)
        data = (Path(tmp) / 'page.pgm').read_bytes()
    magic, width, height, _maximum, pixels = data.split(maxsplit=4)     # en-tête PGM binaire
    assert magic == b'P5'
    width, height = int(width), int(height)
    return width, height, pixels[-width * height:]


def write_png(path: Path, image: Gray) -> None:
    width, height, pixels = image

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body))

    rows = b''.join(b'\x00' + pixels[y * width:(y + 1) * width] for y in range(height))
    path.write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 0, 0, 0, 0))
                     + chunk(b'IDAT', zlib.compress(rows, 6)) + chunk(b'IEND', b''))


def write_scan_pdf(path: Path, images: list[Gray]) -> None:
    """Un PDF fait uniquement d'images (une par page), comme en produit un scanner."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    pages_id = 3 * len(images) + 1
    page_ids = []
    for width, height, pixels in images:
        packed = zlib.compress(pixels, 6)
        image_id = add(b'<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceGray '
                       b'/BitsPerComponent 8 /Filter /FlateDecode /Length %d >>\nstream\n' % (width, height, len(packed))
                       + packed + b'\nendstream')
        content = b'q 595 0 0 842 0 0 cm /Im0 Do Q'
        content_id = add(b'<< /Length %d >>\nstream\n' % len(content) + content + b'\nendstream')
        page_ids.append(add(b'<< /Type /Page /Parent %d 0 R /MediaBox [0 0 595 842] '
                            b'/Resources << /XObject << /Im0 %d 0 R >> >> /Contents %d 0 R >>'
                            % (pages_id, image_id, content_id)))
    kids = b' '.join(b'%d 0 R' % i for i in page_ids)
    assert add(b'<< /Type /Pages /Kids [%s] /Count %d >>' % (kids, len(page_ids))) == pages_id
    catalog = add(b'<< /Type /Catalog /Pages %d 0 R >>' % pages_id)
    out = bytearray(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n')
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b'%d 0 obj\n' % number + body + b'\nendobj\n'
    xref = len(out)
    out += b'xref\n0 %d\n0000000000 65535 f \n' % (len(objects) + 1)
    out += b''.join(b'%010d 00000 n \n' % o for o in offsets)
    out += b'trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n' % (len(objects) + 1, catalog, xref)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
