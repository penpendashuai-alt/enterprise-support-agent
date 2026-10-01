from rag.models import Chunk, DocumentSpec, Section, digest


def chunk_document(
    spec: DocumentSpec, sections: list[Section], size: int, overlap: int
) -> list[Chunk]:
    if not 0 <= overlap < size:
        raise ValueError("Invalid overlap")
    chunks = []
    for section in sections:
        for start in range(0, len(section.text), size - overlap):
            text = section.text[start : start + size]
            location = f"{section.location}；字符 {start + 1}-{start + len(text)}"
            content_hash = digest(text)
            chunk_id = f"{spec.doc_id}:{spec.version}:{digest([location, content_hash])[:20]}"
            chunks.append(
                Chunk(
                    doc_id=spec.doc_id,
                    chunk_id=chunk_id,
                    title=spec.title,
                    source_type=spec.source_type,
                    source_path=spec.path,
                    url=str(spec.url) if spec.url else None,
                    document_version=spec.version,
                    location=location,
                    text=text,
                    content_hash=content_hash,
                )
            )
            if start + size >= len(section.text):
                break
    return chunks
