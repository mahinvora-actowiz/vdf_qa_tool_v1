"""Constant-memory reader for very large top-level JSON arrays of objects.

A data_file_YYYY_MM_DD.json can exceed 2 GB, so it is never loaded with
json.load(). The file is read in fixed-size chunks and each array element
is decoded with JSONDecoder.raw_decode(). Memory use is bounded by the
chunk size plus the largest single record.
"""
from __future__ import annotations

import json
from typing import Any, Iterator

_WS = " \t\r\n"


class JsonStreamError(ValueError):
    pass


def iter_json_array(path: str, chunk_size: int = 1 << 20) -> Iterator[Any]:
    """Yield each element of a top-level JSON array, one at a time."""
    decoder = json.JSONDecoder()
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
            else:
                eof = True

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

        while True:
            skip_ws()
            if pos >= len(buf):
                raise JsonStreamError(f"{path}: unexpected end of file")
            ch = buf[pos]
            if ch == "]":
                return
            if ch == ",":
                pos += 1
                continue
            while True:
                try:
                    obj, end = decoder.raw_decode(buf, pos)
                    break
                except json.JSONDecodeError:
                    if eof:
                        raise JsonStreamError(f"{path}: malformed JSON near offset {pos}")
                    refill()
            pos = end
            yield obj
