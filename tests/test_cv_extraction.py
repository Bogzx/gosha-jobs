"""CV text extraction — including the hostile-file cases.

extract_text() runs inside the API process, which holds every stored CV and
the database credentials. The parsers it reaches are therefore attack
surface reachable by any signed-in user with one upload.
"""

from __future__ import annotations

import io
import zipfile

from gosha.cover_letter import (
    MAX_DOCX_MEMBER_BYTES,
    MAX_DOCX_TOTAL_BYTES,
    extract_text,
)

DOCX_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _docx(document_xml: bytes, extra: dict[str, bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", document_xml)
        for name, payload in (extra or {}).items():
            zf.writestr(name, payload)
    return buf.getvalue()


def test_plain_text_passthrough():
    assert extract_text("cv.txt", b"Python, React, SQL") == "Python, React, SQL"


def test_docx_happy_path():
    xml = (
        f'<?xml version="1.0"?><w:document xmlns:w="{DOCX_NS}"><w:body>'
        f"<w:p><w:r><w:t>Bogdan</w:t></w:r><w:r><w:t>Python</w:t></w:r></w:p>"
        f"</w:body></w:document>"
    ).encode()
    assert extract_text("cv.docx", _docx(xml)) == "Bogdan Python"


def test_docx_billion_laughs_is_refused():
    """Nested internal entities expand to gigabytes inside ET.fromstring."""
    bomb = (
        b'<?xml version="1.0"?>\n'
        b"<!DOCTYPE lolz [\n"
        b'  <!ENTITY lol "lol">\n'
        b'  <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">\n'
        b'  <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">\n'
        b'  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">\n'
        b"]>\n"
        b'<w:document xmlns:w="' + DOCX_NS.encode() + b'"><w:body>'
        b"<w:p><w:r><w:t>&lol3;</w:t></w:r></w:p>"
        b"</w:body></w:document>"
    )
    assert extract_text("cv.docx", _docx(bomb)) is None


def test_docx_external_entity_is_refused():
    """XXE: a DOCTYPE pointing at a local file."""
    xxe = (
        b'<?xml version="1.0"?>\n'
        b'<!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>\n'
        b'<w:document xmlns:w="' + DOCX_NS.encode() + b'"><w:body>'
        b"<w:p><w:r><w:t>&x;</w:t></w:r></w:p></w:body></w:document>"
    )
    assert extract_text("cv.docx", _docx(xxe)) is None


def test_docx_zip_bomb_is_refused():
    """Highly compressible members: small upload, enormous expansion."""
    filler = b"\0" * (MAX_DOCX_TOTAL_BYTES // 4 + 1)
    payload = _docx(
        b'<w:document xmlns:w="' + DOCX_NS.encode() + b'"><w:body/></w:document>',
        extra={f"bomb{i}.bin": filler for i in range(5)},
    )
    # The upload itself is small — this is exactly why the 5 MB cap alone
    # is not a defence.
    assert len(payload) < 1 * 1024 * 1024
    assert extract_text("cv.docx", payload) is None


def test_docx_oversized_document_member_is_refused():
    huge = (
        b'<w:document xmlns:w="' + DOCX_NS.encode() + b'"><w:body><w:p><w:r><w:t>'
        + b"a" * (MAX_DOCX_MEMBER_BYTES + 1024)
        + b"</w:t></w:r></w:p></w:body></w:document>"
    )
    assert extract_text("cv.docx", _docx(huge)) is None


def test_docx_without_document_xml_is_refused():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("not-a-word-file.txt", b"hello")
    assert extract_text("cv.docx", buf.getvalue()) is None


def test_garbage_bytes_do_not_raise():
    assert extract_text("cv.docx", b"not a zip at all") is None
    assert extract_text("cv.pdf", b"not a pdf at all") is None


def test_unsupported_extension_returns_none():
    assert extract_text("cv.exe", b"MZ") is None
