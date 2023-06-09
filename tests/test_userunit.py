# SPDX-FileCopyrightText: 2022 James R. Barlow
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

from math import isclose

import pytest

from ocrmypdf.exceptions import ExitCode
from ocrmypdf.pdfinfo import PdfInfo

from .conftest import check_ocrmypdf, run_ocrmypdf_api

# pylint: disable=redefined-outer-name


@pytest.fixture
def poster(resources):
    return resources / 'poster.pdf'


def test_userunit_ghostscript_fails(poster, no_outpdf, caplog):
    result = run_ocrmypdf_api(poster, no_outpdf, '--output-type=pdfa')
    assert result == ExitCode.input_file
    assert 'not supported by Ghostscript' in caplog.text


def test_userunit_pdf_passes(poster, outpdf):
    before = PdfInfo(poster)
    check_ocrmypdf(
        poster,
        outpdf,
        '--output-type=pdf',
        '--plugin',
        'tests/plugins/tesseract_cache.py',
    )

    after = PdfInfo(outpdf)
    assert isclose(before[0].width_inches, after[0].width_inches)


def test_rotate_interaction(poster, outpdf):
    check_ocrmypdf(
        poster,
        outpdf,
        '--output-type=pdf',
        '--rotate-pages',
        '--plugin',
        'tests/plugins/tesseract_cache.py',
    )
