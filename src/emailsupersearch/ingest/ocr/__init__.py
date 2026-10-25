"""OCR for scanned PDF pages and image attachments, run in an isolated worker process.

Importing this package loads no engine: ``windows_ocr`` and ``rapid_ocr`` import winrt and rapidocr inside their
functions. ``base.select_engine`` names the engine that can run (the host asks it), and ``base.build_engine``
constructs it, in the OCR child process only.
"""
