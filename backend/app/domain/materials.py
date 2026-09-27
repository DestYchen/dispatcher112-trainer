import io
import zipfile
from pathlib import PurePosixPath
from xml.etree import ElementTree

from pypdf import PdfReader
from pypdf.errors import PdfReadError

MAX_FILE_SIZE = 10 * 1024 * 1024
DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def validate_document(filename: str, data: bytes) -> str:
    if not data or len(data) > MAX_FILE_SIZE:
        raise ValueError("Файл должен содержать от 1 байта до 10 МиБ.")
    suffix = PurePosixPath(filename.lower()).suffix
    if suffix == ".pdf":
        if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-4096:]:
            raise ValueError("Файл не является завершённым PDF.")
        try:
            reader = PdfReader(io.BytesIO(data), strict=True)
            if reader.is_encrypted or not 1 <= len(reader.pages) <= 2000:
                raise ValueError("Нужен незашифрованный PDF, от 1 до 2000 страниц.")
        except (PdfReadError, KeyError, ValueError, TypeError) as error:
            raise ValueError(
                "Не удалось прочитать PDF: проверьте документ и защиту паролем."
            ) from error
        return "application/pdf"
    if suffix != ".docx":
        raise ValueError("Поддерживаются документы PDF и DOCX.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 1000 or sum(item.file_size for item in entries) > 50 * 1024 * 1024:
                raise ValueError("Документ превышает допустимый объём распаковки.")
            if not {"[Content_Types].xml", "word/document.xml"} <= set(archive.namelist()):
                raise ValueError("В документе отсутствуют обязательные части DOCX.")
            for entry in entries:
                if (
                    ".." in PurePosixPath(entry.filename).parts
                    or entry.filename.startswith("/")
                    or "vbaproject" in entry.filename.lower()
                    or entry.file_size > max(1, entry.compress_size) * 200
                ):
                    raise ValueError("Документ содержит недопустимое вложение.")
                content = archive.read(entry)
                if entry.filename.endswith((".xml", ".rels")):
                    if b"<!DOCTYPE" in content or b"<!ENTITY" in content:
                        raise ValueError("Документ содержит внешние определения XML.")
                    root = ElementTree.fromstring(content)
                    if entry.filename.endswith(".rels") and any(
                        node.get("TargetMode") == "External" for node in root
                    ):
                        raise ValueError(
                            "Документ содержит внешние связи; сохраните локальную копию."
                        )
    except (zipfile.BadZipFile, ElementTree.ParseError, RuntimeError, KeyError) as error:
        raise ValueError("Не удалось прочитать DOCX: файл повреждён или зашифрован.") from error
    return DOCX_TYPE
