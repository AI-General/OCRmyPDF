OCRmyPDF
========

OCRmyPDF adds an OCR text layer to scanned PDF files, allowing them to be searched

To get the script usage, call: ./OCmyPDF.sh -h

Features
--------

- Generate a searchable PDF/A file from a PDF file containing only images
- Keep the exact resolution of the original embedded images
- If requested deskew and / or clean the image before performing OCR
- Validate the generated file against the PDF/A specification using jhove
- Provides debug mode to enable easy verification of the OCR results

Motivation
----------

I searched the web for a free tool to OCR PDF files on linux/unix:
I found many, but none of them was satisfying.
- Either they produced PDF files with misplaced text below the image (making copy/paste impossible)
- Or they changed the resolution of the embedded images
- Or they generated PDF file having a rediculous big size
- Or they crashed when trying to OCR some of my PDF files
- Or they did not produce valid PDF files (even though they were readable with my current PDF reader) 
On top of that none of them produced PDF/A files (format dedicated for long time storage / archiving)

... so I decided to develop my own tool (using various existing scripts as an inspiration)

Install
--------

TODO

Install jhove:
download jhove from here: http://sourceforge.net/projects/jhove/files/jhove/
After extracting the JHOVE files to some directory "jhove", you have to edit the file "jhove/conf/jhove.conf" and change something in "something" to the actual directory (ending in "/jhove").





