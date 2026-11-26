"""Constant-memory reader for very large top-level JSON arrays of objects.

A data_file_YYYY_MM_DD.json can exceed 2 GB, so it is never loaded with
json.load(). The file is read in fixed-size chunks and each array element
is decoded with JSONDecoder.raw_decode(). Memory use is bounded by the
chunk size plus the largest single record.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable, Iterator, Optional

_WS = " \t\r\n"


class JsonStreamError(ValueError):
    pass


def iter_json_array(path: str, chunk_size: int = 1 << 20,
                    on_progress: Optional[Callable[[float], None]] = None) -> Iterator[Any]:
    """Yield each element of a top-level JSON array, one at a time."""
    decoder = json.JSONDecoder()
    file_size = os.path.getsize(path)
    with open(path, "r", encoding="utf-8-sig") as fh:
        buf = ""
        pos = 0
        eof = False

        def refill() -> None:
            nonlocal buf, pos, eof
            data = fh.read(chunk_size)
            if data:
                buf = buf[pos:] + data
                pos = 0
                if on_progress:
                    on_progress(min(fh.tell() / file_size, 1.0) if file_size else 1.0)
            else:
                eof = True
                if on_progress:
                    on_progress(1.0)

        def skip_ws() -> None:
            nonlocal pos
            while True:
                while pos < len(buf) and buf[pos] in _WS:
                    pos += 1
                if pos < len(buf) or eof:
                    return
                refill()

        refill()
        skip_ws()
        if pos >= len(buf) or buf[pos] != "[":
            raise JsonStreamError(f"{path}: top-level JSON value must be an array")
        pos += 1

        expect_value = True
        first = True
        while True:
            skip_ws()
            if pos >= len(buf):
                raise JsonStreamError(f"{path}: unexpected end of file")
            ch = buf[pos]
            if ch == "]":
                if not first and expect_value:
                    raise JsonStreamError(f"{path}: trailing comma in array")
                pos += 1
                skip_ws()
                if pos < len(buf) or not eof:
                    while not eof and pos >= len(buf):
                        refill()
                        skip_ws()
                    if pos < len(buf):
                        raise JsonStreamError(f"{path}: trailing content after top-level array")
                return
            if not expect_value:
                if ch != ",":
                    raise JsonStreamError(f"{path}: expected ',' or ']' near offset {pos}")
                pos += 1
                expect_value = True
                continue
            if not first and ch == ",":
                raise JsonStreamError(f"{path}: unexpected ',' near offset {pos}")
            while True:
                try:
                    obj, end = decoder.raw_decode(buf, pos)
                    break
                except json.JSONDecodeError:
                    if eof:
                        raise JsonStreamError(f"{path}: malformed JSON near offset {pos}")
                    refill()
            pos = end
            first = False
            expect_value = False
            yield obj
