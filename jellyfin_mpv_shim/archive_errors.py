"""What reading an entry out of a zip can raise, for the two readers that
open books as zips (``comic.py``, ``epub/archive.py``).

One tuple because the two lists drifted: both caught ``BadZipFile`` and
neither caught the decompressors' own errors, so a corrupt deflate stream
reached the reader's error banner as a raw, untranslated ``zlib.error``.
``RuntimeError`` is zipfile's answer for an encrypted entry.
"""

import lzma
import zipfile
import zlib

ZIP_READ_ERRORS = (OSError, EOFError, RuntimeError, zipfile.BadZipFile,
                   zlib.error, lzma.LZMAError)
