"""Bounded, replaceable page-level OCR ports and process adapters."""

import math
import re
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_LANGUAGE_PATTERN = re.compile(r"[A-Za-z0-9_.+@-]+")


class OcrError(Exception):
    """Base error for OCR fallback failures."""


class InvalidOcrRequestError(OcrError):
    """Raised when the caller requests invalid or unbounded OCR work."""


class PdfRenderError(OcrError):
    """Raised when a PDF page cannot be rendered into an image."""


class PdfRenderUnavailableError(PdfRenderError):
    """Raised when the configured PDF renderer is unavailable."""


class PdfRenderTimeoutError(PdfRenderError):
    """Raised when rendering exceeds its per-page timeout."""


class OcrUnavailableError(OcrError):
    """Raised when the configured OCR executable is unavailable."""


class OcrTimeoutError(OcrError):
    """Raised when OCR exceeds its per-page timeout."""


class OcrExecutionError(OcrError):
    """Raised when the OCR provider exits unsuccessfully."""


@dataclass(frozen=True, slots=True)
class OcrOptions:
    """Validated resource and provider settings for one OCR request."""

    dpi: int = 200
    language: str = "eng"
    page_segmentation_mode: int = 6
    timeout_seconds: float = 30.0
    max_pages: int = 50

    def __post_init__(self) -> None:
        _validate_dpi(self.dpi)
        if not isinstance(self.language, str) or _LANGUAGE_PATTERN.fullmatch(self.language) is None:
            raise InvalidOcrRequestError("OCR language must be a safe Tesseract language value.")
        _validate_page_segmentation_mode(self.page_segmentation_mode)
        _validate_timeout(self.timeout_seconds)
        if (
            isinstance(self.max_pages, bool)
            or not isinstance(self.max_pages, int)
            or self.max_pages < 1
        ):
            raise InvalidOcrRequestError("OCR max_pages must be a positive integer.")


@dataclass(frozen=True, slots=True)
class OcrPageResult:
    """Text recovered from one source page by an OCR engine."""

    page_number: int
    text: str
    source: str = "ocr"

    def __post_init__(self) -> None:
        _validate_page_number(self.page_number)
        if not isinstance(self.text, str):
            raise InvalidOcrRequestError("OCR output must be text.")
        if self.source != "ocr":
            raise InvalidOcrRequestError("OCR page results must identify their source as OCR.")


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Small process result contract used by the native-tool adapters."""

    returncode: int
    stdout: bytes
    stderr: bytes


class CommandRunner(Protocol):
    """Run one structured native command without a shell."""

    def run(self, args: Sequence[str], *, timeout_seconds: float) -> CommandResult:
        """Execute arguments and return captured binary output."""


@dataclass(frozen=True, slots=True)
class SubprocessCommandRunner:
    """Production command runner with explicit argument and timeout boundaries."""

    def run(self, args: Sequence[str], *, timeout_seconds: float) -> CommandResult:
        completed = subprocess.run(
            list(args),
            capture_output=True,
            check=False,
            shell=False,
            timeout=timeout_seconds,
        )
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


class PdfPageRenderer(Protocol):
    """Render one PDF page into an image accepted by an OCR engine."""

    def render(
        self,
        *,
        pdf_data: bytes,
        page_number: int,
        dpi: int,
        timeout_seconds: float,
    ) -> bytes:
        """Return a PNG image for the requested 1-based page."""


@dataclass(frozen=True, slots=True)
class PopplerPageRenderer:
    """Render one page through Poppler's ``pdftoppm`` executable."""

    command_runner: CommandRunner = field(default_factory=SubprocessCommandRunner)
    executable: str = "pdftoppm"

    def render(
        self,
        *,
        pdf_data: bytes,
        page_number: int,
        dpi: int,
        timeout_seconds: float,
    ) -> bytes:
        _validate_pdf_data(pdf_data)
        _validate_page_number(page_number)
        _validate_dpi(dpi)
        _validate_timeout(timeout_seconds)
        if not self.executable.strip():
            raise InvalidOcrRequestError("A PDF renderer executable is required.")

        with tempfile.TemporaryDirectory(prefix="openwikirag-pdf-render-") as directory:
            pdf_path = Path(directory) / "source.pdf"
            output_prefix = Path(directory) / "page"
            pdf_path.write_bytes(pdf_data)
            args = (
                self.executable,
                "-f",
                str(page_number),
                "-l",
                str(page_number),
                "-png",
                "-singlefile",
                "-r",
                str(dpi),
                str(pdf_path),
                str(output_prefix),
            )
            try:
                result = self.command_runner.run(args, timeout_seconds=timeout_seconds)
            except FileNotFoundError as exc:
                raise PdfRenderUnavailableError(
                    f"PDF renderer '{self.executable}' was not found."
                ) from exc
            except PermissionError as exc:
                raise PdfRenderUnavailableError(
                    f"PDF renderer '{self.executable}' is not executable."
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise PdfRenderTimeoutError("PDF page rendering timed out.") from exc
            except OSError as exc:
                raise PdfRenderError("PDF page rendering could not start.") from exc

            if result.returncode != 0:
                raise PdfRenderError(
                    f"PDF page rendering failed: {_stderr_detail(result.stderr)}"
                )
            output_path = output_prefix.with_suffix(".png")
            try:
                image_data = output_path.read_bytes()
            except OSError as exc:
                raise PdfRenderError("PDF renderer did not produce a PNG image.") from exc
            if not image_data.startswith(_PNG_SIGNATURE):
                raise PdfRenderError("PDF renderer returned an invalid PNG image.")
            return image_data


class OcrEngine(Protocol):
    """Recognize text from one rendered page image."""

    def recognize(
        self,
        *,
        image_data: bytes,
        language: str,
        page_segmentation_mode: int,
        timeout_seconds: float,
    ) -> str:
        """Return OCR text; an empty string is a valid no-text observation."""


@dataclass(frozen=True, slots=True)
class TesseractOcrEngine:
    """Recognize one PNG page through the Tesseract command-line interface."""

    command_runner: CommandRunner = field(default_factory=SubprocessCommandRunner)
    executable: str = "tesseract"

    def recognize(
        self,
        *,
        image_data: bytes,
        language: str,
        page_segmentation_mode: int,
        timeout_seconds: float,
    ) -> str:
        if not image_data:
            raise InvalidOcrRequestError("An OCR engine requires a non-empty image.")
        _validate_language(language)
        _validate_page_segmentation_mode(page_segmentation_mode)
        _validate_timeout(timeout_seconds)
        if not self.executable.strip():
            raise InvalidOcrRequestError("An OCR engine executable is required.")

        with tempfile.TemporaryDirectory(prefix="openwikirag-ocr-") as directory:
            image_path = Path(directory) / "page.png"
            image_path.write_bytes(image_data)
            args = (
                self.executable,
                str(image_path),
                "stdout",
                "-l",
                language,
                "--psm",
                str(page_segmentation_mode),
            )
            try:
                result = self.command_runner.run(args, timeout_seconds=timeout_seconds)
            except FileNotFoundError as exc:
                raise OcrUnavailableError(
                    f"OCR engine '{self.executable}' was not found."
                ) from exc
            except PermissionError as exc:
                raise OcrUnavailableError(
                    f"OCR engine '{self.executable}' is not executable."
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise OcrTimeoutError("OCR recognition timed out.") from exc
            except OSError as exc:
                raise OcrExecutionError("OCR recognition could not start.") from exc

            if result.returncode != 0:
                raise OcrExecutionError(
                    f"OCR recognition failed: {_stderr_detail(result.stderr)}"
                )
            return result.stdout.decode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class PdfOcrFallback:
    """Run bounded page OCR through replaceable renderer and engine ports."""

    renderer: PdfPageRenderer
    engine: OcrEngine
    options: OcrOptions = field(default_factory=OcrOptions)

    def extract(
        self,
        *,
        pdf_data: bytes,
        page_numbers: Sequence[int],
    ) -> tuple[OcrPageResult, ...]:
        """Render and recognize selected pages in deterministic request order."""

        requested_pages = tuple(page_numbers)
        _validate_page_selection(requested_pages, max_pages=self.options.max_pages)
        if not requested_pages:
            return ()
        _validate_pdf_data(pdf_data)

        results: list[OcrPageResult] = []
        for page_number in requested_pages:
            image_data = self.renderer.render(
                pdf_data=pdf_data,
                page_number=page_number,
                dpi=self.options.dpi,
                timeout_seconds=self.options.timeout_seconds,
            )
            text = self.engine.recognize(
                image_data=image_data,
                language=self.options.language,
                page_segmentation_mode=self.options.page_segmentation_mode,
                timeout_seconds=self.options.timeout_seconds,
            )
            results.append(OcrPageResult(page_number=page_number, text=text))
        return tuple(results)


def _validate_pdf_data(pdf_data: bytes) -> None:
    if not isinstance(pdf_data, bytes) or not pdf_data:
        raise InvalidOcrRequestError("OCR requires non-empty PDF bytes.")


def _validate_page_number(page_number: int) -> None:
    if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
        raise InvalidOcrRequestError("OCR page numbers must be positive integers.")


def _validate_page_selection(page_numbers: Sequence[int], *, max_pages: int) -> None:
    if len(page_numbers) > max_pages:
        raise InvalidOcrRequestError(f"OCR request exceeds the {max_pages}-page limit.")
    for page_number in page_numbers:
        _validate_page_number(page_number)
    if tuple(page_numbers) != tuple(sorted(page_numbers)):
        raise InvalidOcrRequestError("OCR pages must be sorted in ascending order.")
    if len(set(page_numbers)) != len(page_numbers):
        raise InvalidOcrRequestError("OCR page selections cannot contain duplicates.")


def _validate_dpi(dpi: int) -> None:
    if isinstance(dpi, bool) or not isinstance(dpi, int) or dpi < 72 or dpi > 600:
        raise InvalidOcrRequestError("OCR DPI must be between 72 and 600.")


def _validate_language(language: str) -> None:
    if not isinstance(language, str) or _LANGUAGE_PATTERN.fullmatch(language) is None:
        raise InvalidOcrRequestError("OCR language must be a safe Tesseract language value.")


def _validate_page_segmentation_mode(page_segmentation_mode: int) -> None:
    if (
        isinstance(page_segmentation_mode, bool)
        or not isinstance(page_segmentation_mode, int)
        or page_segmentation_mode < 0
        or page_segmentation_mode > 13
    ):
        raise InvalidOcrRequestError("OCR page segmentation mode must be between 0 and 13.")


def _validate_timeout(timeout_seconds: float) -> None:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise InvalidOcrRequestError("OCR timeout must be a finite positive number.")


def _stderr_detail(stderr: bytes) -> str:
    detail = stderr.decode("utf-8", errors="replace").strip()
    return detail[:512] or "native command returned no diagnostic output"
