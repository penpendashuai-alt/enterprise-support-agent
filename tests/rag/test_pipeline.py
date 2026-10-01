import json
from pathlib import Path
from unittest.mock import AsyncMock
from zipfile import ZipFile

import pytest
from pydantic import ValidationError
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from qdrant_client import AsyncQdrantClient, models

from rag.chunker import chunk_document
from rag.config import RAGSettings
from rag.ingestion import build_index
from rag.loader import load_document
from rag.models import DocumentSpec, RAGError
from rag.retriever import Retriever
from rag.vector_store import VectorStore


def spec(path="guide.md", doc_id="guide"):
    return DocumentSpec(
        doc_id=doc_id,
        title="VPN 809",
        path=path,
        version="1",
        source_type="synthetic",
        usage="Original synthetic test",
    )


def corpus(root: Path):
    (root / "guide.md").write_text(
        "# VPN\n\n## 排查\n\n检查客户端配置与网络策略。错误码 809。\n", encoding="utf-8"
    )
    (root / "manifest.json").write_text(
        json.dumps({"version": "test", "documents": [spec().model_dump(mode="json")]}),
        encoding="utf-8",
    )


class FakeEmbedding:
    usage_tokens = 0
    requests = 0
    retries = 0

    async def embed(self, texts):
        return [[1.0] + [0.0] * 1023 for _ in texts]

    async def embed_with_usage(self, texts):
        return await self.embed(texts), {"embedding_tokens": 0}


@pytest.mark.parametrize(
    "field,value",
    [
        ("EMBED_DIMENSIONS", 384),
        ("QDRANT_VECTOR_SIZE", 512),
        ("EMBED_BATCH_SIZE", 11),
        ("RAG_CHUNK_OVERLAP", 800),
    ],
)
def test_invalid_config(field, value):
    with pytest.raises(ValidationError):
        RAGSettings(_env_file=None, **{field: value})


def test_text_chunking_keeps_exact_positions_and_ids(tmp_path):
    corpus(tmp_path)
    sections = load_document(tmp_path, spec())
    assert "行 5-5" in sections[0].location
    first = chunk_document(spec(), sections, 15, 3)
    assert first == chunk_document(spec(), sections, 15, 3)
    assert first[0].text[-3:] == first[1].text[:3]
    assert first[0].point_id != first[1].point_id
    assert "809" in "".join(c.text for c in first)


@pytest.mark.parametrize(
    "name,text,code",
    [
        ("empty.txt", "", "empty_document"),
        ("bad.txt", "\ufffd", "invalid_text_encoding"),
        ("file.bin", "text", "unsupported_format"),
    ],
)
def test_bad_documents_report_failure(tmp_path, name, text, code):
    (tmp_path / name).write_text(text, encoding="utf-8")
    with pytest.raises(RAGError, match=code):
        load_document(tmp_path, spec(name))


def test_private_and_traversal_paths_rejected(tmp_path):
    for name in ["../guide.md", ".env", "personal/plan.md"]:
        with pytest.raises(RAGError, match="unsafe_document_path"):
            load_document(tmp_path, spec(name))


def test_docx_paragraphs_and_pdf_pages(tmp_path):
    with ZipFile(tmp_path / "guide.docx", "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>VPN 809 guide</w:t></w:r></w:p></w:body></w:document>',
        )
    assert load_document(tmp_path, spec("guide.docx"))[0].location == "段落 1"
    writer = PdfWriter()
    page = writer.add_blank_page(width=500, height=500)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 30 400 Td (VPN error 809) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(tmp_path / "guide.pdf")
    sections = load_document(tmp_path, spec("guide.pdf"))
    assert sections[0].location == "页 1" and "809" in sections[0].text
    writer = PdfWriter()
    writer.add_blank_page(width=500, height=500)
    writer.write(tmp_path / "scan.pdf")
    with pytest.raises(RAGError, match="pdf_page_without_text"):
        load_document(tmp_path, spec("scan.pdf"))


@pytest.mark.asyncio
async def test_build_reuse_version_rejection_and_retrieval(tmp_path):
    corpus(tmp_path)
    settings = RAGSettings(_env_file=None)
    client = AsyncQdrantClient(":memory:")
    store = VectorStore(settings, client)
    fake = FakeEmbedding()
    try:
        first = await build_index(tmp_path, settings, store, fake)
        assert first["status"] == "ready" and not first["reused"]
        again = await build_index(tmp_path, settings, store, fake)
        assert again["reused"] and await store.chunk_count() == first["chunks"]
        result = await Retriever(settings, store, fake).retrieve("VPN")
        assert result.status == "ok" and result.evidence[0].doc_id == "guide"
        (tmp_path / "guide.md").write_text("# Changed\n\nReplacement document.\n", encoding="utf-8")
        refused = await build_index(tmp_path, settings, store, fake)
        assert refused["error"] == "immutable_index_requires_new_collection"
        assert (await store.manifest())["status"] == "ready"
        settings2 = settings.model_copy(update={"QDRANT_COLLECTION": "enterprise_support_dense_v2"})
        store2 = VectorStore(settings2, client)
        assert (await build_index(tmp_path, settings2, store2, fake))["status"] == "ready"
        updated = await Retriever(settings2, store2, fake).retrieve("VPN")
        assert "Replacement" in updated.evidence[0].text and "809" not in updated.evidence[0].text
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_partial_failure_not_ready_and_unknown_collection_safe(tmp_path):
    corpus(tmp_path)
    settings = RAGSettings(_env_file=None)
    client = AsyncQdrantClient(":memory:")
    store = VectorStore(settings, client)
    try:
        fake = FakeEmbedding()
        fake.embed = AsyncMock(side_effect=RAGError("embedding_timeout"))
        report = await build_index(tmp_path, settings, store, fake)
        assert report["status"] == "failed"
        assert (await store.manifest())["status"] == "failed"
        assert (
            await Retriever(settings, store, FakeEmbedding()).retrieve("VPN")
        ).error_code == "index_not_ready"
        assert (await build_index(tmp_path, settings, store, FakeEmbedding()))["status"] == "ready"
        incompatible = settings.model_copy(update={"EMBED_MODEL_NAME": "different-model"})
        with pytest.raises(RAGError, match="index_model_or_configuration_mismatch"):
            await VectorStore(incompatible, client).manifest()
        await client.create_collection(
            "enterprise_support_dense_unknown",
            vectors_config=models.VectorParams(size=1024, distance=models.Distance.COSINE),
        )
        settings.QDRANT_COLLECTION = "enterprise_support_dense_unknown"
        assert (
            await build_index(tmp_path, settings, VectorStore(settings, client), FakeEmbedding())
        )["error"] == "unknown_collection"
        assert (await client.get_collection(settings.QDRANT_COLLECTION)).points_count == 0
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_parse_failure_never_touches_store(tmp_path):
    corpus(tmp_path)
    (tmp_path / "guide.md").write_text("")
    store = AsyncMock()
    report = await build_index(tmp_path, RAGSettings(_env_file=None), store, FakeEmbedding())
    assert report["status"] == "failed" and report["failures"][0]["doc_id"] == "guide"
    store.begin.assert_not_awaited()
