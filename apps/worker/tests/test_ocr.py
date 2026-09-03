"""Proof for the bounded page-level OCR boundary and native adapters."""

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from openwikirag.application.ocr import (
    CommandResult,
    InvalidOcrRequestError,
    OcrExecutionError,
    OcrOptions,
    OcrPageResult,
    OcrTimeoutError,
    OcrUnavailableError,
    PdfOcrFallback,
    PdfRenderError,
    PdfRenderTimeoutError,
    PdfRenderUnavailableError,
    PopplerPageRenderer,
    SubprocessCommandRunner,
    TesseractOcrEngine,
)

PNG = b"\x89PNG\r\n\x1a\nrendered"


class RecordingRenderer:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, int, int, float]] = []

    def render(
        self,
        *,
        pdf_data: bytes,
        page_number: int,
        dpi: int,
        timeout_seconds: float,
    ) -> bytes:
        self.calls.append((pdf_data, page_number, dpi, timeout_seconds))
        return f"page-{page_number}".encode()


class RecordingEngine:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, str, int, float]] = []

    def recognize(
        self,
        *,
        image_data: bytes,
        language: str,
        page_segmentation_mode: int,
        timeout_seconds: float,
    ) -> str:
        self.calls.append((image_data, language, page_segmentation_mode, timeout_seconds))
        return f"OCR {image_data.decode()}"


class RecordingCommandRunner:
    def __init__(
        self,
        result: CommandResult | None = None,
        error: BaseException | None = None,
        render_output: bytes | None = PNG,
    ) -> None:
        self.result = result or CommandResult(returncode=0, stdout=PNG, stderr=b"")
        self.error = error
        self.render_output = render_output
        self.calls: list[tuple[str, ...]] = []
        self.paths_present_during_call: list[bool] = []

    def run(self, args: Sequence[str], *, timeout_seconds: float) -> CommandResult:
        del timeout_seconds
        call = tuple(args)
        self.calls.append(call)
        path_index = 1 if call[0].startswith("tesseract") else -2
        self.paths_present_during_call.append(Path(call[path_index]).exists())
        if self.error is not None:
            raise self.error
        if call[0].startswith("pdftoppm") and self.render_output is not None:
            Path(f"{call[-1]}.png").write_bytes(self.render_output)
        return self.result


def test_ocr_fallback_processes_selected_pages_in_order_with_bounded_options() -> None:
    renderer = RecordingRenderer()
    engine = RecordingEngine()
    options = OcrOptions(
        dpi=300,
        language="eng+fra",
        page_segmentation_mode=4,
        timeout_seconds=4.5,
        max_pages=3,
    )
    fallback = PdfOcrFallback(renderer=renderer, engine=engine, options=options)

    results = fallback.extract(pdf_data=b"%PDF-test", page_numbers=(1, 3))

    assert results == (
        OcrPageResult(page_number=1, text="OCR page-1"),
        OcrPageResult(page_number=3, text="OCR page-3"),
    )
    assert [(call[1], call[2], call[3]) for call in renderer.calls] == [
        (1, 300, 4.5),
        (3, 300, 4.5),
    ]
    assert [(call[1], call[2], call[3]) for call in engine.calls] == [
        ("eng+fra", 4, 4.5),
        ("eng+fra", 4, 4.5),
    ]


def test_empty_page_selection_does_no_provider_work() -> None:
    renderer = RecordingRenderer()
    engine = RecordingEngine()

    results = PdfOcrFallback(renderer=renderer, engine=engine).extract(
        pdf_data=b"",
        page_numbers=(),
    )

    assert results == ()
    assert renderer.calls == []
    assert engine.calls == []


@pytest.mark.parametrize(
    "page_numbers",
    [(0,), (2, 1), (1, 1), (1, 2, 3)],
)
def test_ocr_fallback_rejects_invalid_or_unbounded_page_selection(
    page_numbers: tuple[int, ...],
) -> None:
    fallback = PdfOcrFallback(
        renderer=RecordingRenderer(),
        engine=RecordingEngine(),
        options=OcrOptions(max_pages=2),
    )

    with pytest.raises(InvalidOcrRequestError):
        fallback.extract(pdf_data=b"%PDF-test", page_numbers=page_numbers)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"dpi": 71}, "DPI"),
        ({"language": "eng;rm -rf"}, "language"),
        ({"page_segmentation_mode": 14}, "segmentation"),
        ({"timeout_seconds": 0}, "timeout"),
        ({"max_pages": 0}, "max_pages"),
    ],
)
def test_ocr_options_reject_unsafe_or_unbounded_values(
    kwargs: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(InvalidOcrRequestError, match=message):
        OcrOptions(**kwargs)


def test_poppler_renderer_uses_one_temporary_pdf_and_validates_png() -> None:
    runner = RecordingCommandRunner()
    renderer = PopplerPageRenderer(command_runner=runner, executable="pdftoppm-test")

    output = renderer.render(
        pdf_data=b"%PDF-test",
        page_number=4,
        dpi=250,
        timeout_seconds=2.0,
    )

    assert output == PNG
    assert runner.calls[0][0:2] == ("pdftoppm-test", "-f")
    assert runner.calls[0][2:6] == ("4", "-l", "4", "-png")
    assert runner.calls[0][-1].endswith("/page")
    assert runner.paths_present_during_call == [True]
    assert not Path(runner.calls[0][-2]).exists()

    bad_runner = RecordingCommandRunner(
        render_output=b"not-png",
    )
    with pytest.raises(PdfRenderError, match="invalid PNG"):
        PopplerPageRenderer(command_runner=bad_runner).render(
            pdf_data=b"%PDF-test",
            page_number=1,
            dpi=200,
            timeout_seconds=1.0,
        )


def test_poppler_renderer_maps_unavailable_timeout_and_exit_failures() -> None:
    with pytest.raises(PdfRenderUnavailableError):
        PopplerPageRenderer(
            command_runner=RecordingCommandRunner(error=FileNotFoundError())
        ).render(pdf_data=b"%PDF-test", page_number=1, dpi=200, timeout_seconds=1.0)

    with pytest.raises(PdfRenderTimeoutError):
        PopplerPageRenderer(
            command_runner=RecordingCommandRunner(
                error=subprocess.TimeoutExpired(cmd="pdftoppm", timeout=1.0)
            )
        ).render(pdf_data=b"%PDF-test", page_number=1, dpi=200, timeout_seconds=1.0)

    with pytest.raises(PdfRenderError, match="failed"):
        PopplerPageRenderer(
            command_runner=RecordingCommandRunner(
                result=CommandResult(returncode=1, stdout=b"", stderr=b"bad PDF")
            )
        ).render(pdf_data=b"%PDF-test", page_number=1, dpi=200, timeout_seconds=1.0)


def test_tesseract_engine_uses_temporary_image_and_returns_stdout() -> None:
    runner = RecordingCommandRunner(
        result=CommandResult(returncode=0, stdout="héllo\n".encode(), stderr=b"warning")
    )
    engine = TesseractOcrEngine(command_runner=runner, executable="tesseract-test")

    output = engine.recognize(
        image_data=PNG,
        language="eng",
        page_segmentation_mode=6,
        timeout_seconds=3.0,
    )

    assert output == "héllo\n"
    assert runner.calls[0][0:5] == (
        "tesseract-test",
        runner.calls[0][1],
        "stdout",
        "-l",
        "eng",
    )
    assert runner.calls[0][-2:] == ("--psm", "6")
    assert runner.paths_present_during_call == [True]
    assert not Path(runner.calls[0][1]).exists()


@pytest.mark.parametrize(
    ("error", "error_type"),
    [
        (FileNotFoundError(), OcrUnavailableError),
        (PermissionError(), OcrUnavailableError),
        (subprocess.TimeoutExpired(cmd="tesseract", timeout=1.0), OcrTimeoutError),
    ],
)
def test_tesseract_engine_maps_provider_start_failures(
    error: BaseException,
    error_type: type[Exception],
) -> None:
    engine = TesseractOcrEngine(command_runner=RecordingCommandRunner(error=error))

    with pytest.raises(error_type):
        engine.recognize(
            image_data=PNG,
            language="eng",
            page_segmentation_mode=6,
            timeout_seconds=1.0,
        )


def test_tesseract_engine_maps_nonzero_exit_and_allows_empty_text() -> None:
    failed = TesseractOcrEngine(
        command_runner=RecordingCommandRunner(
            result=CommandResult(returncode=1, stdout=b"", stderr=b"OCR failed")
        )
    )
    with pytest.raises(OcrExecutionError, match="OCR failed"):
        failed.recognize(
            image_data=PNG,
            language="eng",
            page_segmentation_mode=6,
            timeout_seconds=1.0,
        )

    empty = TesseractOcrEngine(
        command_runner=RecordingCommandRunner(
            result=CommandResult(returncode=0, stdout=b"", stderr=b"")
        )
    )
    assert empty.recognize(
        image_data=PNG,
        language="eng",
        page_segmentation_mode=6,
        timeout_seconds=1.0,
    ) == ""


def test_subprocess_runner_uses_argument_array_and_disables_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_args: list[str] = []
    seen_shell: bool | None = None

    def fake_run(
        args: list[str],
        *,
        capture_output: bool,
        check: bool,
        shell: bool,
        timeout: float,
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal seen_shell
        seen_args.extend(args)
        seen_shell = shell
        assert capture_output is True
        assert check is False
        assert timeout == 2.0
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=b"ok", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = SubprocessCommandRunner().run(("tool", "--flag", "value"), timeout_seconds=2.0)

    assert result == CommandResult(returncode=0, stdout=b"ok", stderr=b"")
    assert seen_args == ["tool", "--flag", "value"]
    assert seen_shell is False
