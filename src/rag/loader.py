import json
import re
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

from pypdf import PdfReader

from rag.models import DocumentSpec, RAGError, Section


def read_manifest(root: Path) -> tuple[dict, list[DocumentSpec]]:
    raw = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    specs = [DocumentSpec.model_validate(item) for item in raw["documents"]]
    if len({s.doc_id for s in specs}) != len(specs) or len({s.path for s in specs}) != len(specs):
        raise RAGError("duplicate_document")
    for spec in specs:
        if spec.source_type == "public_summary" and (not spec.url or not spec.accessed):
            raise RAGError("public_source_missing")
    return raw, specs


def load_document(root: Path, spec: DocumentSpec) -> list[Section]:
    path = (root / spec.path).resolve()
    if not path.is_relative_to(root.resolve()) or any(
        part.startswith(".") or part.lower() == "personal" for part in Path(spec.path).parts
    ):
        raise RAGError("unsafe_document_path")
    suffix = path.suffix.lower()
    sections = []
    if suffix in {".md", ".txt"}:
        text = path.read_text(encoding="utf-8-sig")
        lines = text.splitlines()
        title, paragraph, start = "正文", [], 1
        for number, line in enumerate([*lines, ""], 1):
            if not line.strip() or re.match(r"^#{1,6}\s", line):
                if paragraph:
                    sections.append(
                        Section(
                            text="\n".join(paragraph), location=f"{title}；行 {start}-{number - 1}"
                        )
                    )
                    paragraph = []
                if line.strip():
                    title = line.lstrip("# ")
            else:
                if not paragraph:
                    start = number
                paragraph.append(line)
    elif suffix == ".pdf":
        for page, item in enumerate(PdfReader(path).pages, 1):
            text = item.extract_text() or ""
            if text.strip():
                sections.append(Section(text=text, location=f"页 {page}"))
            else:
                raise RAGError("pdf_page_without_text")
    elif suffix == ".docx":
        with ZipFile(path) as archive:
            document = ElementTree.fromstring(archive.read("word/document.xml"))
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        for number, paragraph in enumerate(document.findall(".//w:body/w:p", ns), 1):
            text = "".join(p.text or "" for p in paragraph.findall(".//w:t", ns))
            if text.strip():
                sections.append(Section(text=text, location=f"段落 {number}"))
    else:
        raise RAGError("unsupported_format")
    for section in sections:
        section.text = section.text.replace("\r\n", "\n").strip()
        if "\ufffd" in section.text or "\x00" in section.text:
            raise RAGError("invalid_text_encoding")
    if not sections:
        raise RAGError("empty_document")
    return sections
