# SPDX-FileCopyrightText: 2022 James R. Barlow
# SPDX-License-Identifier: MPL-2.0

"""Interface to Ghostscript executable."""

from __future__ import annotations

import logging
import os
import re
import sys
from io import BytesIO
from os import fspath
from pathlib import Path
from subprocess import PIPE, CalledProcessError

from packaging.version import Version
from PIL import Image, UnidentifiedImageError

from ocrmypdf.exceptions import SubprocessOutputError
from ocrmypdf.helpers import Resolution
from ocrmypdf.subprocess import get_version, run, run_polling_stderr

# Remove this workaround when we require Pillow >= 10
try:
    Transpose = Image.Transpose  # type: ignore
except AttributeError:
    # Pillow 9 shim
    Transpose = Image  # type: ignore


COLOR_CONVERSION_STRATEGIES = frozenset(
    [
        'CMYK',
        'Gray',
        'LeaveColorUnchanged',
        'RGB',
        'UseDeviceIndependentColor',
    ]
)

log = logging.getLogger(__name__)


class DuplicateFilter(logging.Filter):
    """Filter out duplicate log messages."""

    def __init__(self, logger: logging.Logger):
        self.last: logging.LogRecord | None = None
        self.count = 0
        self.logger = logger

    def filter(self, record):
        if self.last and record.msg == self.last.msg:
            self.count += 1
            return False
        else:
            if self.count >= 1:
                rep_msg = f"(previous message repeated {self.count} times)"
                self.count = 0  # Avoid infinite recursion
                self.logger.log(self.last.levelno, rep_msg)
            self.last = record
            return True


log.addFilter(DuplicateFilter(log))


# Ghostscript executable - gswin32c is not supported
GS = 'gswin64c' if os.name == 'nt' else 'gs'


def version() -> Version:
    return Version(get_version(GS))


def _gs_error_reported(stream) -> bool:
    match = re.search(r'error', stream, flags=re.IGNORECASE)
    return bool(match)


def rasterize_pdf(
    input_file: os.PathLike,
    output_file: os.PathLike,
    *,
    raster_device: str,
    raster_dpi: Resolution,
    pageno: int = 1,
    page_dpi: Resolution | None = None,
    rotation: int | None = None,
    filter_vector: bool = False,
    stop_on_error: bool = False,
):
    """Rasterize one page of a PDF at resolution raster_dpi in canvas units."""
    raster_dpi = raster_dpi.round(6)
    if not page_dpi:
        page_dpi = raster_dpi
    # args_gs = (
    #     [
    #         GS,
    #         '-dQUIET',
    #         '-dSAFER',
    #         '-dBATCH',
    #         '-dNOPAUSE',
    #         '-dInterpolateControl=-1',
    #         f'-sDEVICE={raster_device}',
    #         f'-dFirstPage={pageno}',
    #         f'-dLastPage={pageno}',
    #         f'-r{raster_dpi.x:f}x{raster_dpi.y:f}',
    #     ]
    #     + (['-dFILTERVECTOR'] if filter_vector else [])
    #     + [
    #         '-o',
    #         '-',
    #         '-sstdout=%stderr',  # Literal %s, not string interpolation
    #         '-dAutoRotatePages=/None',  # Probably has no effect on raster
    #         '-f',
    #         fspath(input_file),
    #     ]
    # )

    # try:
    #     # p = run(args_gs, stdout=PIPE, stderr=PIPE, check=True)
    #     p = pdfbox.PDFBox()
    #     print(input_file)
    #     p.pdf_to_images("/Users/soumyadasgupta/Documents/clm-projects/ai/test.pdf",page_number=1)
    #     print("After")
    # except CalledProcessError as e:
    #     print("Error")
    #     log.error(e.stderr.decode(errors='replace'))
    #     raise SubprocessOutputError('Ghostscript rasterizing failed') from e
    # else:
        # stderr = p.stderr.decode(errors='replace')
        # if _gs_error_reported(stderr):
        #     log.error(stderr)

    try:
        print("We are here")
        path = os.path.dirname(str(input_file))+"/PDFOCROutput"+str(pageno)+".jpg"
        print("This is updated path for testing should have /" +path)
        with Image.open(path) as im:
            if rotation is not None:
                log.debug("Rotating output by %i", rotation)
                # rotation is a clockwise angle and Image.ROTATE_* is
                # counterclockwise so this cancels out the rotation
                if rotation == 90:
                    im = im.transpose(Transpose.ROTATE_90)
                elif rotation == 180:
                    im = im.transpose(Transpose.ROTATE_180)
                elif rotation == 270:
                    im = im.transpose(Transpose.ROTATE_270)
                if rotation % 180 == 90:
                    page_dpi = page_dpi.flip_axis()
            im.save(fspath(output_file), dpi=[300,300])
    except UnidentifiedImageError:
        log.error(
            f"Ghostscript (using {raster_device} at {raster_dpi} dpi) produced "
            "an invalid page image file."
        )
        raise


class GhostscriptFollower:
    """Parses the output of Ghostscript and uses it to update the progress bar."""

    re_process = re.compile(r"Processing pages \d+ through (\d+).")
    re_page = re.compile(r"Page (\d+)")

    def __init__(self, progressbar_class):
        self.count = 0
        self.progressbar_class = progressbar_class
        self.progressbar = None

    def __call__(self, line):
        if not self.progressbar_class:
            return
        if not self.progressbar:
            m = self.re_process.match(line.strip())
            if m:
                self.count = int(m.group(1))
                self.progressbar = self.progressbar_class(
                    total=self.count, desc="PDF/A conversion", unit='page'
                )
                return
        else:
            if self.re_page.match(line.strip()):
                self.progressbar.update()


def generate_pdfa(
    pdf_pages,
    output_file: os.PathLike,
    *,
    compression: str,
    color_conversion_strategy: str,
    pdf_version: str = '1.5',
    pdfa_part: str = '2',
    progressbar_class=None,
    stop_on_error: bool = False,
):
    # Ghostscript's compression is all or nothing. We can either force all images
    # to JPEG, force all to Flate/PNG, or let it decide how to encode the images.
    # In most case it's best to let it decide.
    compression_args = []
    if compression == 'jpeg':
        compression_args = [
            "-dAutoFilterColorImages=false",
            "-dColorImageFilter=/DCTEncode",
            "-dAutoFilterGrayImages=false",
            "-dGrayImageFilter=/DCTEncode",
        ]
    elif compression == 'lossless':
        compression_args = [
            "-dAutoFilterColorImages=false",
            "-dColorImageFilter=/FlateEncode",
            "-dAutoFilterGrayImages=false",
            "-dGrayImageFilter=/FlateEncode",
        ]
    else:
        compression_args = [
            "-dAutoFilterColorImages=true",
            "-dAutoFilterGrayImages=true",
        ]

    gs_version = version()
    if gs_version == Version('9.56.0'):
        # 9.56.0 breaks our OCR, should be fixed in 9.56.1
        # https://bugs.ghostscript.com/show_bug.cgi?id=705187
        compression_args.append('-dNEWPDF=false')

    if os.name == 'nt':
        # Windows has lots of fatal "permission denied" errors
        stop_on_error = False

    # nb no need to specify ProcessColorModel when ColorConversionStrategy
    # is set; see:
    # https://bugs.ghostscript.com/show_bug.cgi?id=699392
    args_gs = (
        [
            GS,
            "-dBATCH",
            "-dNOPAUSE",
            "-dSAFER",
            f"-dCompatibilityLevel={str(pdf_version)}",
            "-sDEVICE=pdfwrite",
            "-dAutoRotatePages=/None",
            f"-sColorConversionStrategy={color_conversion_strategy}",
        ]
        + (['-dPDFSTOPONERROR'] if stop_on_error else [])
        + compression_args
        + [
            "-dJPEGQ=95",
            f"-dPDFA={pdfa_part}",
            "-dPDFACompatibilityPolicy=1",
            "-o",
            "-",
            "-sstdout=%stderr",  # Literal %s, not string interpolation
        ]
    )
    args_gs.extend(fspath(s) for s in pdf_pages)  # Stringify Path objs

    try:
        with Path(output_file).open('wb') as output:
            p = run_polling_stderr(
                args_gs,
                stdout=output,
                stderr=PIPE,
                check=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                callback=GhostscriptFollower(progressbar_class),
            )
    except CalledProcessError as e:
        # Ghostscript does not change return code when it fails to create
        # PDF/A - check PDF/A status elsewhere
        log.error(e.stderr)
        raise SubprocessOutputError('Ghostscript PDF/A rendering failed') from e
    else:
        stderr = p.stderr
        # If there is an error we log the whole stderr, except for filtering
        # duplicates.
        if _gs_error_reported(stderr):
            # Ghostscript outputs the pattern **** Error: ....  frequently.
            # Occasionally the error message is spammed many times. We filter
            # out duplicates of this message using the filter above. We use
            # the **** pattern to split the stderr into parts.
            for part in stderr.split('****'):
                log.error(part)
