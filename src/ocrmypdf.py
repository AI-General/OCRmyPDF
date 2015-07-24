#!/usr/bin/env python3

from contextlib import suppress
from tempfile import NamedTemporaryFile
import sys
import os
import fileinput
import re
import shutil
import warnings
import multiprocessing

import PyPDF2 as pypdf
from parse import parse

from subprocess import Popen, check_call, PIPE, CalledProcessError, \
    TimeoutExpired
try:
    from subprocess import DEVNULL
except ImportError:
    DEVNULL = open(os.devnull, 'wb')


from ruffus import transform, suffix, merge, active_if, regex, jobs_limit, \
    mkdir, formatter, follows, subdivide, collate, check_if_uptodate
import ruffus.cmdline as cmdline

from .hocrtransform import HocrTransform
from .pageinfo import pdf_get_all_pageinfo
from .pdfa import generate_pdfa_def
from . import tesseract


warnings.simplefilter('ignore', pypdf.utils.PdfReadWarning)


BASEDIR = os.path.dirname(os.path.realpath(__file__))
JHOVE_PATH = os.path.realpath(os.path.join(BASEDIR, '..', 'jhove'))
JHOVE_JAR = os.path.join(JHOVE_PATH, 'bin', 'JhoveApp.jar')
JHOVE_CFG = os.path.join(JHOVE_PATH, 'conf', 'jhove.conf')

EXIT_BAD_ARGS = 1
EXIT_BAD_INPUT_FILE = 2
EXIT_MISSING_DEPENDENCY = 3
EXIT_INVALID_OUTPUT_PDFA = 4
EXIT_FILE_ACCESS_ERROR = 5
EXIT_OTHER_ERROR = 15

# -------------
# External dependencies

MINIMUM_TESS_VERSION = '3.02.02'

if tesseract.VERSION < MINIMUM_TESS_VERSION:
    print(
        "Please install tesseract {0} or newer "
        "(currently installed version is {1})".format(
            MINIMUM_TESS_VERSION, tesseract.VERSION),
        file=sys.stderr)
    sys.exit(EXIT_MISSING_DEPENDENCY)


# -------------
# Parser

parser = cmdline.get_argparse(
    prog="OCRmyPDF",
    description="Generate searchable PDF file from an image-only PDF file.")

parser.add_argument(
    'input_file',
    help="PDF file containing the images to be OCRed")
parser.add_argument(
    'output_file',
    help="output searchable PDF file")
parser.add_argument(
    '-l', '--language', action='append',
    help="language of the file to be OCRed")

preprocessing = parser.add_argument_group(
    "Preprocessing options",
    "Improve OCR quality and final image")
preprocessing.add_argument(
    '-d', '--deskew', action='store_true',
    help="deskew each page before performing OCR")
preprocessing.add_argument(
    '-c', '--clean', action='store_true',
    help="clean pages with unpaper before performing OCR")
preprocessing.add_argument(
    '-i', '--clean-final', action='store_true',
    help="incorporate the cleaned image in the final PDF file")
preprocessing.add_argument(
    '--oversample', metavar='DPI', type=int,
    help="oversample images to improve OCR results slightly")

parser.add_argument(
    '--force-ocr', action='store_true',
    help="Force to OCR, even if the page already contains fonts")
parser.add_argument(
    '--skip-text', action='store_true',
    help="Skip OCR on pages that contain fonts and include the page anyway")
parser.add_argument(
    '--skip-big', action='store_true',
    help="Skip OCR for pages that are very large")
parser.add_argument(
    '--exact-image', action='store_true',
    help="Use original page from PDF without re-rendering")

advanced = parser.add_argument_group(
    "Advanced",
    "Advanced options for power users and debugging")
advanced.add_argument(
    '--deskew-provider', choices=['imagemagick', 'leptonica'],
    default='leptonica')
advanced.add_argument(
    '--temp-folder', default='', type=str,
    help="folder where the temporary files should be placed")
advanced.add_argument(
    '--tesseract-config', default=[], type=list, action='append',
    help="Tesseract configuration")

debugging = parser.add_argument_group(
    "Debugging",
    "Arguments to help with troubleshooting and debugging")
debugging.add_argument(
    '-k', '--keep-temporary-files', action='store_true',
    help="keep temporary files (helpful for debugging)")
debugging.add_argument(
    '-g', '--debug-rendering', action='store_true',
    help="render each page twice with debug information on second page")


options = parser.parse_args()

# ----------
# Languages

if not options.language:
    options.language = ['eng']  # Enforce English hegemony

# Support v2.x "eng+deu" language syntax
if '+' in options.language[0]:
    options.language = options.language[0].split('+')

if not set(options.language).issubset(tesseract.LANGUAGES):
    print(
        "The installed version of tesseract does not have language "
        "data for the following requested languages: ",
        file=sys.stderr)
    for lang in (set(options.language) - tesseract.LANGUAGES):
        print(lang, file=sys.stderr)
    sys.exit(EXIT_BAD_ARGS)


# ----------
# Logging


_logger, _logger_mutex = cmdline.setup_logging(__name__, options.log_file,
                                               options.verbose)


class WrappedLogger:

    def __init__(self, my_logger, my_mutex):
        self.logger = my_logger
        self.mutex = my_mutex

    def log(self, *args, **kwargs):
        with self.mutex:
            self.logger.log(*args, **kwargs)

    def debug(self, *args, **kwargs):
        with self.mutex:
            self.logger.debug(*args, **kwargs)

    def info(self, *args, **kwargs):
        with self.mutex:
            self.logger.info(*args, **kwargs)

    def warning(self, *args, **kwargs):
        with self.mutex:
            self.logger.warning(*args, **kwargs)

    def error(self, *args, **kwargs):
        with self.mutex:
            self.logger.error(*args, **kwargs)

    def critical(self, *args, **kwargs):
        with self.mutex:
            self.logger.critical(*args, **kwargs)

_log = WrappedLogger(_logger, _logger_mutex)


def re_symlink(input_file, soft_link_name, log=_log):
    """
    Helper function: relinks soft symbolic link if necessary
    """
    # Guard against soft linking to oneself
    if input_file == soft_link_name:
        log.debug("Warning: No symbolic link made. You are using " +
                  "the original data directory as the working directory.")
        return

    # Soft link already exists: delete for relink?
    if os.path.lexists(soft_link_name):
        # do not delete or overwrite real (non-soft link) file
        if not os.path.islink(soft_link_name):
            raise Exception("%s exists and is not a link" % soft_link_name)
        try:
            os.unlink(soft_link_name)
        except:
            log.debug("Can't unlink %s" % (soft_link_name))

    if not os.path.exists(input_file):
        raise Exception("trying to create a broken symlink to %s" % input_file)

    log.debug("os.symlink(%s, %s)" % (input_file, soft_link_name))

    # Create symbolic link using absolute path
    os.symlink(
        os.path.abspath(input_file),
        soft_link_name
    )


# -------------
# The Pipeline

manager = multiprocessing.Manager()
_pdfinfo = manager.list()
_pdfinfo_lock = manager.Lock()

if options.temp_folder == '':
    options.temp_folder = 'tmp'


@follows(mkdir(options.temp_folder))
@transform(
    input=options.input_file,
    filter=suffix('.pdf'),
    output='.repaired.pdf',
    output_dir=options.temp_folder,
    extras=[_log, _pdfinfo, _pdfinfo_lock])
def repair_pdf(
        input_file,
        output_file,
        log,
        pdfinfo,
        pdfinfo_lock):
    args_mutool = [
        'mutool', 'clean',
        input_file, output_file
    ]
    check_call(args_mutool)

    with pdfinfo_lock:
        pdfinfo.extend(pdf_get_all_pageinfo(output_file))
        log.info(pdfinfo)


# pageno, width_pt, height_pt = map(int, options.page_info.split(' ', 3))
# pageinfo = pdf_get_pageinfo(options.input_file, pageno, width_pt, height_pt)

# if not pageinfo['images']:
#     # If the page has no images, then it contains vector content or text
#     # or both. It seems quite unlikely that one would find meaningful text
#     # from rasterizing vector content. So skip the page.
#     log.info(
#         "Page {0} has no images - skipping OCR".format(pageno)
#     )
# elif pageinfo['has_text']:
#     s = "Page {0} already has text! – {1}"

#     if not options.force_ocr and not options.skip_text:
#         log.error(s.format(pageno,
#                      "aborting (use -f or -s to force OCR)"))
#         sys.exit(1)
#     elif options.force_ocr:
#         log.info(s.format(pageno,
#                     "rasterizing text and running OCR anyway"))
#     elif options.skip_text:
#         log.info(s.format(pageno,
#                     "skipping all processing on this page"))

# ocr_required = pageinfo['images'] and \
#     (options.force_ocr or
#         (not (pageinfo['has_text'] and options.skip_text)))

# if ocr_required and options.skip_big:
#     area = pageinfo['width_inches'] * pageinfo['height_inches']
#     pixel_count = pageinfo['width_pixels'] * pageinfo['height_pixels']
#     if area > (11.0 * 17.0) or pixel_count > (300.0 * 300.0 * 11 * 17):
#         ocr_required = False
#         log.info(
#             "Page {0} is very large; skipping due to -b".format(pageno))


@subdivide(
    repair_pdf,
    formatter(),
    "{path[0]}/*.page.pdf",
    "{path[0]}/",
    _log,
    _pdfinfo,
    _pdfinfo_lock)
def split_pages(
        input_file,
        output_files,
        output_file_name_root,
        log,
        pdfinfo,
        pdfinfo_lock):

    for oo in output_files:
        with suppress(FileNotFoundError):
            os.unlink(oo)
    args_pdfseparate = [
        'pdfseparate',
        input_file,
        output_file_name_root + '%06d.page.pdf'
    ]
    check_call(args_pdfseparate)


def get_pageinfo(input_file, pdfinfo, pdfinfo_lock):
    pageno = int(os.path.basename(input_file)[0:6]) - 1
    print(pageno)
    with pdfinfo_lock:
        pageinfo = pdfinfo[pageno].copy()
    return pageinfo


@transform(
    input=split_pages,
    filter=suffix('.page.pdf'),
    output='.page.png',
    output_dir=options.temp_folder,
    extras=[_log, _pdfinfo, _pdfinfo_lock])
def rasterize_with_ghostscript(
        input_file,
        output_file,
        log,
        pdfinfo,
        pdfinfo_lock):

    pageinfo = get_pageinfo(input_file, pdfinfo, pdfinfo_lock)

    device = 'png16m'  # 24-bit
    if all(image['comp'] == 1 for image in pageinfo['images']):
        if all(image['bpc'] == 1 for image in pageinfo['images']):
            device = 'pngmono'
        elif not any(image['color'] == 'color'
                     for image in pageinfo['images']):
            device = 'pnggray'

    with NamedTemporaryFile(delete=True) as tmp:
        args_gs = [
            'gs',
            '-dBATCH', '-dNOPAUSE',
            '-sDEVICE=%s' % device,
            '-o', tmp.name,
            '-r{0}x{1}'.format(
                str(pageinfo['xres_render']), str(pageinfo['yres_render'])),
            input_file
        ]

        p = Popen(args_gs, close_fds=True, stdout=PIPE, stderr=PIPE,
                  universal_newlines=True)
        stdout, stderr = p.communicate()
        if stdout:
            log.debug(stdout)
        if stderr:
            log.error(stderr)

        if p.returncode == 0:
            shutil.copy(tmp.name, output_file)
        else:
            log.error('Ghostscript rendering failed')


@transform(
    input=rasterize_with_ghostscript,
    filter=suffix(".page.png"),
    output=".hocr",
    extras=[_log, _pdfinfo, _pdfinfo_lock])
def ocr_tesseract(
        input_file,
        output_file,
        log,
        pdfinfo,
        pdfinfo_lock):

    pageinfo = get_pageinfo(input_file, pdfinfo, pdfinfo_lock)

    args_tesseract = [
        'tesseract',
        '-l', '+'.join(options.language),
        input_file,
        output_file,
        'hocr'
    ] + options.tesseract_config
    p = Popen(args_tesseract, close_fds=True, stdout=PIPE, stderr=PIPE,
              universal_newlines=True)
    try:
        stdout, stderr = p.communicate(timeout=180)
    except TimeoutExpired:
        p.kill()
        stdout, stderr = p.communicate()
        # Generate a HOCR file with no recognized text if tesseract times out
        # Temporary workaround to hocrTransform not being able to function if
        # it does not have a valid hOCR file.
        with open(output_file, 'w', encoding="utf-8") as f:
            f.write(tesseract.HOCR_TEMPLATE.format(
                pageinfo['width_pixels'],
                pageinfo['height_pixels']))
    else:
        if stdout:
            log.info(stdout)
        if stderr:
            log.error(stderr)

        if p.returncode != 0:
            raise CalledProcessError(p.returncode, args_tesseract)

        if os.path.exists(output_file + '.html'):
            # Tesseract 3.02 appends suffix ".html" on its own (.hocr.html)
            shutil.move(output_file + '.html', output_file)
        elif os.path.exists(output_file + '.hocr'):
            # Tesseract 3.03 appends suffix ".hocr" on its own (.hocr.hocr)
            shutil.move(output_file + '.hocr', output_file)

        # Tesseract 3.03 inserts source filename into hocr file without
        # escaping it, creating invalid XML and breaking the parser.
        # As a workaround, rewrite the hocr file, replacing the filename
        # with a space.
        regex_nested_single_quotes = re.compile(
            r"""title='image "([^"]*)";""")
        with fileinput.input(files=(output_file,), inplace=True) as f:
            for line in f:
                line = regex_nested_single_quotes.sub(
                    r"""title='image " ";""", line)
                print(line, end='')  # fileinput.input redirects stdout


@collate(
    input=[rasterize_with_ghostscript, ocr_tesseract],
    filter=regex(r".*/(\d{6})(?:\.page\.png|\.hocr)"),
    output=r'\1.rendered.pdf',
    extras=[_log, _pdfinfo, _pdfinfo_lock])
def render_page(
        infiles,
        output_file,
        log,
        pdfinfo,
        pdfinfo_lock):
    hocr = next(ii for ii in infiles if ii.endswith('.hocr'))
    image = next(ii for ii in infiles if ii.endswith('.page.png'))

    pageinfo = get_pageinfo(image, pdfinfo, pdfinfo_lock)
    dpi = round(max(pageinfo['xres'], pageinfo['yres']))

    hocrtransform = HocrTransform(hocr, dpi)
    hocrtransform.to_pdf(output_file, imageFileName=image,
                         showBoundingboxes=False, invisibleText=True)


@transform(
    input=repair_pdf,
    filter=suffix('.repaired.pdf'),
    output='.pdfa_def.ps',
    output_dir=options.temp_folder,
    extras=[_log])
def generate_postscript_stub(
        input_file,
        output_file,
        log):
    generate_pdfa_def(output_file)


@merge(
    input=[render_page, generate_postscript_stub],
    output=os.path.join(options.temp_folder, 'merged.pdf'),
    extras=[_log, _pdfinfo, _pdfinfo_lock])
def merge_pages(
        input_files,
        output_file,
        log,
        pdfinfo,
        pdfinfo_lock):

    ocr_pages, postscript = input_files[0:-1], input_files[-1]

    with NamedTemporaryFile(delete=True) as gs_pdf:
        args_gs = [
            "gs",
            "-dQUIET",
            "-dBATCH",
            "-dNOPAUSE",
            "-sDEVICE=pdfwrite",
            "-sColorConversionStrategy=/RGB",
            "-sProcessColorModel=DeviceRGB",
            "-dPDFA",
            "-sPDFACompatibilityPolicy=2",
            "-sOutputICCProfile=srgb.icc",
            "-sOutputFile=" + gs_pdf.name,
            postscript,  # the PDF/A definition header
        ]
        args_gs.extend(ocr_pages)
        check_call(args_gs)
        shutil.copy(gs_pdf.name, output_file)


@transform(
    input=merge_pages,
    filter=formatter(),
    output=options.output_file,
    extras=[_log, _pdfinfo, _pdfinfo_lock])
def validate_pdfa(
        input_file,
        output_file,
        log,
        pdfinfo,
        pdfinfo_lock):

    args_jhove = [
        'java',
        '-jar', JHOVE_JAR,
        '-c', JHOVE_CFG,
        '-m', 'PDF-hul',
        input_file
    ]
    p_jhove = Popen(args_jhove, close_fds=True, universal_newlines=True,
                    stdout=PIPE, stderr=DEVNULL)
    stdout, _ = p_jhove.communicate()

    log.debug(stdout)
    if p_jhove.returncode != 0:
        log.error(stdout)
        raise RuntimeError(
            "Unexpected error while checking compliance to PDF/A file.")

    pdf_is_valid = True
    if re.search(r'ErrorMessage', stdout,
                 re.IGNORECASE | re.MULTILINE):
        pdf_is_valid = False
    if re.search(r'^\s+Status.*not valid', stdout,
                 re.IGNORECASE | re.MULTILINE):
        pdf_is_valid = False
    if re.search(r'^\s+Status.*Not well-formed', stdout,
                 re.IGNORECASE | re.MULTILINE):
        pdf_is_valid = False

    pdf_is_pdfa = False
    if re.search(r'^\s+Profile:.*PDF/A-1', stdout,
                 re.IGNORECASE | re.MULTILINE):
        pdf_is_pdfa = True

    if not pdf_is_valid:
        log.warning('Output file: The generated PDF/A file is INVALID')
    elif pdf_is_valid and not pdf_is_pdfa:
        log.warning('Output file: Generated file is a VALID PDF but not PDF/A')
    elif pdf_is_valid and pdf_is_pdfa:
        log.info('Output file: The generated PDF/A file is VALID')
    shutil.copy(input_file, output_file)


# @active_if(ocr_required)
# @active_if(options.preprocess_deskew != 0
#            and options.deskew_provider == 'imagemagick')
# @transform(convert_to_png, suffix(".png"), ".deskewed.png")
# def deskew_imagemagick(input_file, output_file):
#     args_convert = [
#         'convert',
#         input_file,
#         '-deskew', '40%',
#         '-gravity', 'center',
#         '-extent', '{width_pixels}x{height_pixels}'.format(**pageinfo),
#         '+repage',
#         output_file
#     ]

#     p = Popen(args_convert, close_fds=True, stdout=PIPE, stderr=PIPE,
#               universal_newlines=True)
#     stdout, stderr = p.communicate()

#     if stdout:
#         log.info(stdout)
#     if stderr:
#         log.error(stderr)

#     if p.returncode != 0:
#         raise CalledProcessError(p.returncode, args_convert)


# @active_if(ocr_required)
# @active_if(options.preprocess_deskew != 0
#            and options.deskew_provider == 'leptonica')
# @transform(convert_to_png, suffix(".png"), ".deskewed.png")
# def deskew_leptonica(input_file, output_file):
#     from .leptonica import deskew
#     deskew(input_file, output_file,
#            min(pageinfo['xres'], pageinfo['yres']))


# @active_if(ocr_required)
# @active_if(options.preprocess_clean != 0)
# @merge([unpack_with_pdftoppm, unpack_with_ghostscript,
#         deskew_imagemagick, deskew_leptonica],
#        os.path.join(options.temp_folder, "%04i.for_clean.pnm" % pageno))
# def select_image_for_cleaning(infiles, output_file):
#     input_file = infiles[-1]
#     args_convert = [
#         'convert',
#         input_file,
#         output_file
#     ]
#     check_call(args_convert)


# @active_if(ocr_required)
# @active_if(options.preprocess_clean != 0)
# @transform(select_image_for_cleaning, suffix(".pnm"), ".cleaned.pnm")
# def clean_unpaper(input_file, output_file):
#     args_unpaper = [
#         'unpaper',
#         '--dpi', str(int(round((pageinfo['xres'] * pageinfo['yres']) ** 0.5))),
#         '--mask-scan-size', '100',
#         '--no-deskew',
#         '--no-grayfilter',
#         '--no-blackfilter',
#         '--no-mask-center',
#         '--no-border-align',
#         input_file,
#         output_file
#     ]

#     p = Popen(args_unpaper, close_fds=True, stdout=PIPE, stderr=PIPE,
#               universal_newlines=True)
#     stdout, stderr = p.communicate()

#     if stdout:
#         log.info(stdout)
#     if stderr:
#         log.error(stderr)

#     if p.returncode != 0:
#         raise CalledProcessError(p.returncode, args_unpaper)


# @active_if(ocr_required)
# @transform(clean_unpaper, suffix(".cleaned.pnm"), ".cleaned.png")
# def cleaned_to_png(input_file, output_file):
#     args_convert = [
#         'convert',
#         input_file,
#         output_file
#     ]
#     check_call(args_convert)


# @active_if(ocr_required)
# @merge([unpack_with_ghostscript, convert_to_png, deskew_imagemagick,
#         deskew_leptonica, cleaned_to_png],
#        os.path.join(options.temp_folder, "%04i.for_ocr.png" % pageno))
# def select_ocr_image(infiles, output_file):
#     re_symlink(infiles[-1], output_file)




# @active_if(ocr_required)
# @transform(select_ocr_image, suffix(".for_ocr.png"), ".hocr")
# def ocr_tesseract(
#         input_file,
#         output_file):

#     args_tesseract = [
#         'tesseract',
#         '-l', options.language,
#         input_file,
#         output_file,
#         'hocr',
#         options.tess_cfg_files
#     ]
#     p = Popen(args_tesseract, close_fds=True, stdout=PIPE, stderr=PIPE,
#               universal_newlines=True)
#     try:
#         stdout, stderr = p.communicate(timeout=180)
#     except TimeoutExpired:
#         p.kill()
#         stdout, stderr = p.communicate()
#         # Generate a HOCR file with no recognized text if tesseract times out
#         # Temporary workaround to hocrTransform not being able to function if
#         # it does not have a valid hOCR file.
#         with open(output_file, 'w', encoding="utf-8") as f:
#             f.write(hocr_template.format(pageinfo['width_pixels'],
#                                          pageinfo['height_pixels']))
#     else:
#         if stdout:
#             log.info(stdout)
#         if stderr:
#             log.error(stderr)

#         if p.returncode != 0:
#             raise CalledProcessError(p.returncode, args_tesseract)

#         if os.path.exists(output_file + '.html'):
#             # Tesseract 3.02 appends suffix ".html" on its own (.hocr.html)
#             shutil.move(output_file + '.html', output_file)
#         elif os.path.exists(output_file + '.hocr'):
#             # Tesseract 3.03 appends suffix ".hocr" on its own (.hocr.hocr)
#             shutil.move(output_file + '.hocr', output_file)

#         # Tesseract inserts source filename into hocr file without escaping
#         # it. This could break the XML parser. Rewrite the hocr file,
#         # replacing the filename with a space.
#         regex_nested_single_quotes = re.compile(
#             r"""title='image "([^"]*)";""")
#         with fileinput.input(files=(output_file,), inplace=True) as f:
#             for line in f:
#                 line = regex_nested_single_quotes.sub(
#                     r"""title='image " ";""", line)
#                 print(line, end='')  # fileinput.input redirects stdout


# @active_if(ocr_required and not options.exact_image)
# @merge([unpack_with_ghostscript, convert_to_png,
#         deskew_imagemagick, deskew_leptonica, cleaned_to_png],
#        os.path.join(options.temp_folder, "%04i.image_for_pdf" % pageno))
# def select_image_for_pdf(infiles, output_file):
#     if options.preprocess_clean != 0 and options.preprocess_cleantopdf != 0:
#         input_file = infiles[-1]
#     elif options.preprocess_deskew != 0 and options.preprocess_clean != 0:
#         input_file = infiles[-2]
#     elif options.preprocess_deskew != 0 and options.preprocess_clean == 0:
#         input_file = infiles[-1]
#     else:
#         input_file = infiles[0]

#     if all(image['enc'] == 'jpeg' for image in pageinfo['images']):
#         # If all images were JPEGs originally, produce a JPEG as output
#         check_call(['convert', input_file, 'jpg:' + output_file])
#     else:
#         re_symlink(input_file, output_file)


# @active_if(ocr_required and not options.exact_image)
# @merge([ocr_tesseract, select_image_for_pdf],
#        os.path.join(options.temp_folder, '%04i.rendered.pdf' % pageno))
# def render_page(infiles, output_file):
#     hocr, image = infiles[0], infiles[1]

#     dpi = round(max(pageinfo['xres'], pageinfo['yres']))

#     hocrtransform = HocrTransform(hocr, dpi)
#     hocrtransform.to_pdf(output_file, imageFileName=image,
#                          showBoundingboxes=False, invisibleText=True)


# @active_if(ocr_required and options.pdf_noimg)
# @transform(ocr_tesseract, suffix(".hocr"), ".ocred.todebug.pdf")
# def render_text_output_page(input_file, output_file):
#     dpi = round(max(pageinfo['xres'], pageinfo['yres']))

#     hocrtransform = HocrTransform(input_file, dpi)
#     hocrtransform.to_pdf(output_file, imageFileName=None,
#                          showBoundingboxes=True, invisibleText=False)


# @active_if(ocr_required and options.exact_image)
# @transform(ocr_tesseract, suffix(".hocr"), ".hocr.pdf")
# def render_hocr_blank_page(input_file, output_file):
#     dpi = round(max(pageinfo['xres'], pageinfo['yres']))

#     hocrtransform = HocrTransform(input_file, dpi)
#     hocrtransform.to_pdf(output_file, imageFileName=None,
#                          showBoundingboxes=False, invisibleText=True)


# @active_if(ocr_required and options.exact_image)
# @merge([render_hocr_blank_page, extract_single_page],
#        os.path.join(options.temp_folder, "%04i.merged.pdf") % pageno)
# def merge_hocr_with_original_page(infiles, output_file):
#     with open(infiles[0], 'rb') as hocr_input, \
#             open(infiles[1], 'rb') as page_input, \
#             open(output_file, 'wb') as output:
#         hocr_reader = pypdf.PdfFileReader(hocr_input)
#         page_reader = pypdf.PdfFileReader(page_input)
#         writer = pypdf.PdfFileWriter()

#         the_page = hocr_reader.getPage(0)
#         the_page.mergePage(page_reader.getPage(0))
#         writer.addPage(the_page)
#         writer.write(output)


# @merge([render_page, merge_hocr_with_original_page, extract_single_page],
#        os.path.join(options.temp_folder, '%04i.ocred.pdf' % pageno))
# def select_final_page(infiles, output_file):
#     re_symlink(infiles[-1], output_file)


if __name__ == '__main__':
    cmdline.run(options)


