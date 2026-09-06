"""Exercise the real native OCR path with a deterministic image-only PDF."""

from __future__ import annotations

import subprocess
import tempfile
import zlib
from pathlib import Path

from openwikirag.application.extraction import NoTextExtractedError, PdfExtractor
from openwikirag.application.ocr import (
    OcrOptions,
    PdfOcrFallback,
    PopplerPageRenderer,
    TesseractOcrEngine,
)


def _digital_pdf(text: str) -> bytes:
    content = f"BT /F1 42 Tf 72 62 Td ({text}) Tj ET\n".encode()
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 144] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    )
    return _serialize_pdf(objects)


def _rasterize_digital_pdf(pdf_data: bytes) -> tuple[int, int, bytes]:
    with tempfile.TemporaryDirectory(prefix="openwikirag-ocr-fixture-") as directory:
        source_path = Path(directory) / "seed.pdf"
        output_prefix = Path(directory) / "seed"
        source_path.write_bytes(pdf_data)
        result = subprocess.run(
            (
                "pdftoppm",
                "-f",
                "1",
                "-l",
                "1",
                "-singlefile",
                "-r",
                "200",
                str(source_path),
                str(output_prefix),
            ),
            capture_output=True,
            check=False,
            shell=False,
            timeout=15.0,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Could not create OCR fixture: {result.stderr!r}")
        ppm = output_prefix.with_suffix(".ppm").read_bytes()
    header, pixels = ppm.split(b"\n255\n", maxsplit=1)
    header_parts = header.split()
    if header_parts[:1] != [b"P6"] or len(header_parts) != 3:
        raise RuntimeError("Native rasterizer returned an unexpected PPM fixture.")
    return int(header_parts[1]), int(header_parts[2]), pixels


def _image_only_pdf(text: str) -> bytes:
    width, height, pixels = _rasterize_digital_pdf(_digital_pdf(text))
    compressed = zlib.compress(pixels, level=9)
    page_height = max(96, round(612 * height / width))
    content = f"q\n612 0 0 {page_height} 0 0 cm\n/Im1 Do\nQ\n".encode()
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 {page_height}] ".encode()
            + b"/Resources << /XObject << /Im1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"endstream",
        (
            f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
            f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
            f"/Length {len(compressed)} >>"
        ).encode()
        + b"\nstream\n"
        + compressed
        + b"\nendstream",
    )

    return _serialize_pdf(objects)


def _serialize_pdf(objects: tuple[bytes, ...]) -> bytes:
    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_number, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{object_number} 0 obj\n".encode())
        pdf.extend(body)
        pdf.extend(b"\nendobj\n")
    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode())
    pdf.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode()
    )
    return bytes(pdf)


def main() -> None:
    expected = "OPENWIKI OCR"
    pdf_data = _image_only_pdf(expected)
    try:
        PdfExtractor().extract(pdf_data)
    except NoTextExtractedError:
        pass
    else:
        raise AssertionError("The OCR fixture unexpectedly contains extractable PDF text.")
    document = PdfExtractor(
        ocr_fallback=PdfOcrFallback(
            renderer=PopplerPageRenderer(),
            engine=TesseractOcrEngine(),
            options=OcrOptions(dpi=300, timeout_seconds=15, max_pages=1),
        )
    ).extract(pdf_data)

    if document.quality is None or document.quality.status != "sufficient":
        raise AssertionError(f"Unexpected OCR quality: {document.quality!r}")
    if document.ocr is None or document.ocr.recovered_page_numbers != (1,):
        raise AssertionError(f"Missing OCR provenance: {document.ocr!r}")
    if not any(span.kind == "ocr" and span.page_number == 1 for span in document.spans):
        raise AssertionError("Normalized document has no OCR page span.")

    observed = " ".join(document.text.upper().split())
    for token in expected.split():
        if token not in observed:
            raise AssertionError(f"OCR output {observed!r} does not contain {token!r}.")
    print(
        "native_ocr_smoke_passed "
        f"parser={document.parser_name} version={document.parser_version} "
        f"text={observed!r} bytes={len(pdf_data)}"
    )


if __name__ == "__main__":
    main()
