"""AI cover letter generation + CV text storage.

Flow:
  1. User uploads CV once (web or /upload_cv) — stored encrypted at rest
     (gosha/cv_crypto.py; plaintext only in local development).
  2. User requests a cover letter for a specific job.
  3. CV + job details go to the configured LLM provider (gosha/llm.py).
  4. The letter is stored in the DB so it can be retrieved later.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select

from gosha import cv_crypto, llm
from gosha.database import get_session
from gosha.models import CoverLetter, Job

log = logging.getLogger(__name__)

# CV storage directory
CV_DIR = Path(__file__).resolve().parent.parent / "data" / "cvs"

# How much of a CV is sent to the external LLM per cover-letter request.
# This is the single largest disclosure surface in the product, so the
# number is named rather than inlined and is quoted verbatim by the privacy
# notice (gosha/api/legal.py) — the two can never drift.
CV_CHARS_TO_LLM = 15000


# ---------------------------------------------------------------------------
# CV management
# ---------------------------------------------------------------------------


# Layout: <uid>.enc (Fernet token) when a key is configured, <uid>.txt
# (plaintext) only in development. A .txt found while a key is configured
# is a pre-encryption file: it is encrypted on first read, and in bulk at
# startup by encrypt_plaintext_cvs().


def _encrypted_path(user_id: int) -> Path:
    return CV_DIR / f"{user_id}.enc"


def _plaintext_path(user_id: int) -> Path:
    return CV_DIR / f"{user_id}.txt"


def get_cv_path(user_id: int) -> Path:
    """The file holding a user's CV (its mtime is the upload time)."""
    encrypted = _encrypted_path(user_id)
    if encrypted.exists():
        return encrypted
    return _plaintext_path(user_id)


def _write_atomic(path: Path, data: bytes) -> None:
    """Write via a temp file + rename, owner-readable only.

    Two processes (bot and api) share this directory; a reader must never
    see half a ciphertext, which would fail authentication.
    """
    CV_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CV_DIR, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def save_cv(user_id: int, text: str) -> Path:
    """Store CV text, encrypted when a key is configured."""
    if cv_crypto.cipher() is not None:
        path = _encrypted_path(user_id)
        _write_atomic(path, cv_crypto.encrypt(text))
        _plaintext_path(user_id).unlink(missing_ok=True)
        return path

    cv_crypto.require_key_configured()  # raises outside development
    path = _plaintext_path(user_id)
    _write_atomic(path, text.encode("utf-8"))
    _encrypted_path(user_id).unlink(missing_ok=True)
    return path


def load_cv(user_id: int) -> str | None:
    """Load a user's CV text. Returns None if not uploaded."""
    encrypted = _encrypted_path(user_id)
    if encrypted.exists():
        return cv_crypto.decrypt(encrypted.read_bytes())

    plaintext = _plaintext_path(user_id)
    if not plaintext.exists():
        return None
    text = plaintext.read_text(encoding="utf-8")
    if cv_crypto.cipher() is not None:
        _migrate_plaintext(user_id, text)
    return text


def _migrate_plaintext(user_id: int, text: str) -> None:
    """Replace a pre-encryption .txt with its encrypted form, keeping mtime."""
    plaintext = _plaintext_path(user_id)
    try:
        mtime = plaintext.stat().st_mtime
    except FileNotFoundError:
        return  # another process migrated it first
    encrypted = _encrypted_path(user_id)
    _write_atomic(encrypted, cv_crypto.encrypt(text))
    # Keep the upload time: it drives "uploaded at" and CV retention.
    os.utime(encrypted, (mtime, mtime))
    plaintext.unlink(missing_ok=True)


def encrypt_plaintext_cvs() -> int:
    """Encrypt every plaintext CV left from before encryption; returns count.

    Run at startup by both processes. Safe to run concurrently: writes are
    atomic, and a file another process already migrated is skipped.
    """
    if cv_crypto.cipher() is None or not CV_DIR.exists():
        return 0
    migrated = 0
    for path in sorted(CV_DIR.glob("*.txt")):
        if not path.stem.isdigit():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        _migrate_plaintext(int(path.stem), text)
        migrated += 1
    if migrated:
        log.info("Encrypted %d plaintext CV file(s) at rest", migrated)
    return migrated


def stored_cv_user_ids() -> list[int]:
    """User ids with a CV file on disk (either format)."""
    if not CV_DIR.exists():
        return []
    ids = {
        int(p.stem) for p in CV_DIR.iterdir()
        if p.suffix in (".enc", ".txt") and p.stem.isdigit()
    }
    return sorted(ids)


def delete_cv(user_id: int) -> bool:
    """Delete a user's stored CV (both formats). Returns True if it existed."""
    existed = False
    for path in (_encrypted_path(user_id), _plaintext_path(user_id)):
        if path.exists():
            path.unlink()
            existed = True
    return existed


def _clean_pdf_text(text: str) -> str:
    """Clean up common PDF extraction artifacts from LaTeX CVs."""
    # Remove LaTeX \csuse{...} icon commands
    text = re.sub(r"\\csuse\s*\{[^}]*\}:?\s*", "", text)
    # Remove Unicode Private Use Area chars (icon fonts like FontAwesome)
    text = re.sub(r"[\ue000-\uf8ff\U000f0000-\U000ffffd]", "", text)
    # Remove stray icon-font remnants (single non-ASCII symbols on contact lines)
    text = re.sub(r"^[#§ï*]\s+", "", text, flags=re.MULTILINE)
    # Collapse multiple blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def extract_text_from_attachment(attachment) -> str | None:
    """Extract text from a Discord attachment (PDF or plain text)."""
    content = await attachment.read()
    return extract_text(attachment.filename, content)


def extract_text(filename: str, content: bytes) -> str | None:
    """Extract CV text from raw file bytes (PDF/DOCX/TXT/MD).

    Shared by the Discord upload path and the web API upload path.
    Returns None on failure or unsupported extension.
    """
    filename = filename.lower()

    if filename.endswith(".txt") or filename.endswith(".md"):
        return content.decode("utf-8", errors="replace")

    if filename.endswith(".pdf"):
        try:
            import pymupdf

            doc = pymupdf.open(stream=content, filetype="pdf")
            pages = [page.get_text() for page in doc]
            text = "\n".join(pages).strip()
            text = _clean_pdf_text(text)
            return text if text else None
        except ImportError:
            log.warning("pymupdf not installed — cannot extract PDF text. Install with: pip install pymupdf")
            return None
        except Exception as exc:
            log.error("PDF extraction failed: %s", exc)
            return None

    if filename.endswith(".docx"):
        try:
            return _extract_docx_text(content)
        except Exception as exc:
            log.error("DOCX extraction failed: %s", exc)
            return None

    return None


# A .docx is a zip, so an attacker controls the DECOMPRESSED size — a few
# hundred KB of upload can expand to gigabytes. The 5 MB upload cap does
# nothing about that, so the expansion is bounded here as well. A real CV's
# document.xml is tens of KB; 32 MB is absurdly generous.
MAX_DOCX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_DOCX_TOTAL_BYTES = 96 * 1024 * 1024

# Second primitive in the same three lines: ET.fromstring honours internal
# entity definitions, so a DTD with nested entities ("billion laughs") turns
# a small file into unbounded memory. Word never emits a DTD, so any DOCTYPE
# or ENTITY declaration is a refusal rather than something to parse safely.
_XML_DTD_RE = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)

_DOCX_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _extract_docx_text(content: bytes) -> str | None:
    """Minimal .docx text extraction, bounded against zip bombs and DTDs."""
    import io
    import xml.etree.ElementTree as ET
    import zipfile

    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        declared_total = sum(info.file_size for info in zf.infolist())
        if declared_total > MAX_DOCX_TOTAL_BYTES:
            raise ValueError(
                f"DOCX expands to {declared_total} bytes — refusing to unpack"
            )

        try:
            info = zf.getinfo("word/document.xml")
        except KeyError:
            raise ValueError("DOCX has no word/document.xml") from None
        if info.file_size > MAX_DOCX_MEMBER_BYTES:
            raise ValueError(
                f"word/document.xml declares {info.file_size} bytes — too large"
            )

        # Read with a hard cap too: the header size is attacker-controlled
        # and may understate what the stream actually produces.
        with zf.open(info) as handle:
            xml_content = handle.read(MAX_DOCX_MEMBER_BYTES + 1)
        if len(xml_content) > MAX_DOCX_MEMBER_BYTES:
            raise ValueError("word/document.xml exceeded the decompression cap")

    if _XML_DTD_RE.search(xml_content):
        raise ValueError("DOCX XML carries a DTD — refusing to parse")

    tree = ET.fromstring(xml_content)
    texts = [node.text for node in tree.iter(f"{{{_DOCX_NS}}}t") if node.text]
    return " ".join(texts).strip() or None


# ---------------------------------------------------------------------------
# Monthly usage tracking
# ---------------------------------------------------------------------------


async def get_monthly_usage(user_id: int) -> int:
    """Return the number of cover letters generated this calendar month."""
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    async with get_session() as session:
        result = await session.execute(
            select(func.count(CoverLetter.id)).where(
                CoverLetter.user_id == user_id,
                CoverLetter.created_at >= month_start,
            )
        )
        return result.scalar() or 0


# ---------------------------------------------------------------------------
# Cover letter generation
# ---------------------------------------------------------------------------


def _build_prompt(cv_text: str, job: Job) -> str:
    """Build the prompt for Gemini combining CV + job details."""
    job_info = f"Title: {job.title}\nCompany: {job.company}\nLocation: {job.location or 'Not specified'}"
    if job.description:
        # Limit description to prevent token overflow
        desc = job.description[:3000]
        job_info += f"\n\nJob Description:\n{desc}"
    if job.salary_min or job.salary_max:
        parts = []
        if job.salary_min:
            parts.append(f"{job.salary_min:,.0f}")
        if job.salary_max:
            parts.append(f"{job.salary_max:,.0f}")
        salary = " - ".join(parts)
        if job.salary_currency:
            salary += f" {job.salary_currency}"
        job_info += f"\nSalary: {salary}"

    return f"""You are a professional career coach helping a computer science student write a cover letter.

Given the candidate's CV and a job posting, write a concise, professional cover letter (250-350 words).

Rules:
- Be specific: reference actual skills/experience from the CV that match the job requirements
- Be natural: avoid generic phrases like "I am excited to apply" or "I am a passionate individual"
- Be honest: don't fabricate experience, but highlight transferable skills
- Match the tone to the company (startup = casual, corporate = formal)
- Include a clear opening, 2-3 body paragraphs connecting CV to job, and a brief closing
- Do NOT include the date, addresses, or "Dear Hiring Manager" header — just the letter body
- Write in English unless the job description is in another language

---

CANDIDATE CV:
{cv_text[:CV_CHARS_TO_LLM]}

---

JOB POSTING:
{job_info}

---

Write the cover letter now:"""


async def generate_cover_letter(
    user_id: int, job_id: int, *, force_regenerate: bool = False,
) -> tuple[str | None, bool]:
    """Generate a cover letter for a user + job pair.

    Returns (content, was_cached) — content is None on failure.
    Stores the result in the DB for later retrieval.
    If force_regenerate is True, deletes any existing cached letter first.
    """
    # Check for existing cover letter
    async with get_session() as session:
        existing = await session.execute(
            select(CoverLetter).where(
                CoverLetter.user_id == user_id,
                CoverLetter.job_id == job_id,
            )
        )
        found = existing.scalar_one_or_none()
        if found:
            # Auto-expire cached letters older than 30 days.
            # SQLite returns naive datetimes — normalize to UTC before math.
            created_at = found.created_at
            if created_at is not None and created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - created_at).days if created_at else 999
            if not force_regenerate and age_days < 30:
                return found.content, True
            # Stale or force-regenerate: delete and recreate
            await session.delete(found)
            await session.commit()

    # Load CV
    cv_text = load_cv(user_id)
    if not cv_text:
        return None, False

    # Load job
    async with get_session() as session:
        result = await session.execute(select(Job).where(Job.id == job_id))
        job = result.scalar_one_or_none()
        if not job:
            return None, False

    # Generate
    prompt = _build_prompt(cv_text, job)
    content = await llm.generate(prompt)
    if not content:
        return None, False

    # Cap at 3900 chars to stay within Discord's 4096 embed description limit
    if len(content) > 3900:
        content = content[:3900] + "\n\n[Trimmed for length]"

    # Store
    async with get_session() as session:
        cl = CoverLetter(user_id=user_id, job_id=job_id, content=content)
        session.add(cl)
        await session.commit()

    log.info("Generated cover letter for user %d, job %d (%d chars)", user_id, job_id, len(content))
    return content, False
