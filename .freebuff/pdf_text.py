#!/usr/bin/env python3
"""Minimal PDF text extractor (stdlib only): decompress Flate streams and
pull text-showing operators (Tj / TJ / ') out of content streams."""
import re
import sys
import zlib

def extract(path: str) -> str:
    data = open(path, "rb").read()
    chunks = []
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.S):
        raw = m.group(1)
        # strip trailing EOL before endstream
        raw = raw.rstrip(b"\r\n")
        try:
            content = zlib.decompress(raw)
        except Exception:
            continue
        chunks.append(content)
    out = []
    for content in chunks:
        text = b""
        # collect all strings in text-showing ops
        for sm in re.finditer(rb"\((?:[^()\\]|\\.)*\)\s*Tj|\[(?:[^\[\]]*)\]\s*TJ", content):
            seg = sm.group(0)
            strings = re.findall(rb"\(((?:[^()\\]|\\.)*)\)", seg)
            line = b"".join(s.decode("latin-1").encode("latin-1") for s in strings)
            # decode \ escapes
            line = re.sub(rb"\\([nrtbf()\\])", lambda e: {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f", b"(": b"(", b")": b")", b"\\": b"\\"}[e.group(1)], line)
            text += line + b"\n"
        out.append(text.decode("latin-1", "replace"))
    return "\n".join(out)

if __name__ == "__main__":
    print(extract(sys.argv[1]))
