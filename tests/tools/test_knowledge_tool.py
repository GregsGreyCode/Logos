"""Tests for tools/knowledge_store.py and tools/knowledge_tool.py."""

from __future__ import annotations

import json
import math
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools.knowledge_store import (
    KnowledgeStore,
    chunk_text,
    cosine_similarity_search,
    embed_texts,
    _cosine_similarity_pure,
)


# ===========================================================================
# Chunking
# ===========================================================================

class TestChunkText:
    def test_empty_string_returns_empty(self):
        assert chunk_text("") == []
        assert chunk_text("   ") == []

    def test_short_text_single_chunk(self):
        result = chunk_text("hello world", chunk_size=100)
        assert result == ["hello world"]

    def test_exact_chunk_size(self):
        text = "a" * 512
        result = chunk_text(text, chunk_size=512, chunk_overlap=0)
        assert len(result) == 1
        assert result[0] == text

    def test_overlap_produces_more_chunks(self):
        text = "a" * 1000
        no_overlap = chunk_text(text, chunk_size=500, chunk_overlap=0)
        with_overlap = chunk_text(text, chunk_size=500, chunk_overlap=100)
        assert len(with_overlap) > len(no_overlap)

    def test_chunks_cover_full_text(self):
        text = "The quick brown fox jumps over the lazy dog. " * 50
        chunks = chunk_text(text, chunk_size=100, chunk_overlap=20)
        # Verify first and last content is present
        assert chunks[0].startswith("The quick")
        assert chunks[-1].endswith("dog.")

    def test_overlap_content(self):
        text = "ABCDEFGHIJ" * 10  # 100 chars
        chunks = chunk_text(text, chunk_size=30, chunk_overlap=10)
        # Each chunk after the first should overlap with the previous
        for i in range(1, len(chunks)):
            # Last 10 chars of prev chunk should appear in current
            prev_end = chunks[i - 1][-10:]
            assert prev_end in chunks[i] or chunks[i].startswith(prev_end[:len(chunks[i])])


# ===========================================================================
# Cosine similarity
# ===========================================================================

class TestCosineSimilarity:
    def test_identical_vectors_score_one(self):
        v = [1.0, 0.0, 0.0]
        results = cosine_similarity_search(v, [v], top_k=1)
        assert len(results) == 1
        assert results[0][1] == pytest.approx(1.0, abs=0.01)

    def test_orthogonal_vectors_score_zero(self):
        q = [1.0, 0.0, 0.0]
        e = [0.0, 1.0, 0.0]
        results = cosine_similarity_search(q, [e], top_k=1, threshold=0.0)
        assert len(results) == 1
        assert results[0][1] == pytest.approx(0.0, abs=0.01)

    def test_top_k_ordering(self):
        q = [1.0, 0.0]
        embeddings = [
            [0.5, 0.5],   # score ~0.707
            [1.0, 0.0],   # score 1.0
            [0.0, 1.0],   # score 0.0
        ]
        results = cosine_similarity_search(q, embeddings, top_k=2)
        assert results[0][0] == 1  # best match
        assert results[1][0] == 0  # second best

    def test_threshold_filters(self):
        q = [1.0, 0.0]
        embeddings = [
            [1.0, 0.0],   # score 1.0
            [0.0, 1.0],   # score 0.0
        ]
        results = cosine_similarity_search(q, embeddings, top_k=5, threshold=0.5)
        assert len(results) == 1
        assert results[0][0] == 0

    def test_empty_embeddings(self):
        assert cosine_similarity_search([1.0], [], top_k=5) == []


class TestCosineSimilarityPure:
    def test_matches_numpy_version(self):
        q = [0.5, 0.3, 0.8]
        embeddings = [
            [0.1, 0.9, 0.2],
            [0.5, 0.3, 0.8],
            [0.7, 0.1, 0.4],
        ]
        pure = _cosine_similarity_pure(q, embeddings, top_k=3, threshold=0.0)
        numpy_results = cosine_similarity_search(q, embeddings, top_k=3, threshold=0.0)
        # Same ordering
        assert [r[0] for r in pure] == [r[0] for r in numpy_results]
        # Similar scores
        for p, n in zip(pure, numpy_results):
            assert p[1] == pytest.approx(n[1], abs=0.01)


# ===========================================================================
# KnowledgeStore
# ===========================================================================

@pytest.fixture
def store(tmp_path):
    """Create a KnowledgeStore with a mocked embedding function."""
    s = KnowledgeStore(
        knowledge_dir=tmp_path / "knowledge",
        embedding_model="test-model",
        chunk_size=50,
        chunk_overlap=10,
        max_chunks=100,
    )
    return s


def _fake_embed(texts, **kwargs):
    """Generate deterministic fake embeddings based on text content.

    Uses a simple bag-of-characters approach so texts with overlapping
    characters produce similar vectors (enabling cosine similarity tests).
    """
    embeddings = []
    for text in texts:
        # Bag-of-characters in 26 dimensions (a-z frequencies)
        text_lower = text.lower()
        emb = [text_lower.count(chr(ord('a') + i)) for i in range(26)]
        # Normalise
        norm = math.sqrt(sum(x * x for x in emb)) or 1
        embeddings.append([x / norm for x in emb])
    return embeddings


class TestKnowledgeStoreIngest:
    def test_ingest_creates_files(self, store, tmp_path):
        with patch("tools.knowledge_store.embed_texts", side_effect=_fake_embed):
            result = store.ingest("Hello world this is a test document", source_name="test-doc")

        assert result["success"] is True
        assert result["chunk_count"] > 0
        assert (tmp_path / "knowledge" / "index.json").exists()
        assert (tmp_path / "knowledge" / "chunks.jsonl").exists()

    def test_ingest_stores_source_metadata(self, store):
        with patch("tools.knowledge_store.embed_texts", side_effect=_fake_embed):
            store.ingest("Some text content here", source_name="my-doc", source_type="file")

        index = json.loads((store.knowledge_dir / "index.json").read_text())
        assert len(index) == 1
        assert index[0]["source_name"] == "my-doc"
        assert index[0]["source_type"] == "file"
        assert "ingested_at" in index[0]
        assert index[0]["chunk_count"] > 0

    def test_ingest_rejects_duplicate_source(self, store):
        with patch("tools.knowledge_store.embed_texts", side_effect=_fake_embed):
            store.ingest("Content one", source_name="dup")
            result = store.ingest("Content two", source_name="dup")

        assert result["success"] is False
        assert "already exists" in result["error"]

    def test_ingest_rejects_empty_text(self, store):
        result = store.ingest("", source_name="empty")
        assert result["success"] is False

    def test_ingest_respects_max_chunks(self, store):
        store.max_chunks = 5
        with patch("tools.knowledge_store.embed_texts", side_effect=_fake_embed):
            # This should produce more than 5 chunks
            result = store.ingest("x" * 500, source_name="big")

        assert result["success"] is False
        assert "exceed" in result["error"]

    def test_ingest_handles_embedding_failure(self, store):
        with patch("tools.knowledge_store.embed_texts", side_effect=RuntimeError("model not found")):
            result = store.ingest("Some text", source_name="fail")

        assert result["success"] is False
        assert "Embedding failed" in result["error"]

    def test_multiple_sources(self, store):
        with patch("tools.knowledge_store.embed_texts", side_effect=_fake_embed):
            r1 = store.ingest("Document one content", source_name="doc-1")
            r2 = store.ingest("Document two content", source_name="doc-2")

        assert r1["success"] is True
        assert r2["success"] is True
        assert r2["total_chunks"] > r1["total_chunks"]


class TestKnowledgeStoreSearch:
    def test_search_empty_store(self, store):
        with patch("tools.knowledge_store.embed_texts", side_effect=_fake_embed):
            result = store.search("test query")

        assert result["success"] is True
        assert result["results"] == []
        assert "empty" in result.get("message", "").lower()

    def test_search_returns_results(self, tmp_path):
        store = KnowledgeStore(
            knowledge_dir=tmp_path / "search_test",
            embedding_model="test-model",
            chunk_size=50,
            chunk_overlap=10,
            max_chunks=100,
        )
        with patch("tools.knowledge_store.embed_texts", side_effect=_fake_embed):
            store.ingest("Python is a programming language", source_name="python-doc")
            result = store.search("programming", top_k=3)

        assert result["success"] is True
        assert len(result["results"]) > 0
        assert "text" in result["results"][0]
        assert "source" in result["results"][0]
        assert "score" in result["results"][0]

    def test_search_includes_source_attribution(self, store):
        with patch("tools.knowledge_store.embed_texts", side_effect=_fake_embed):
            store.ingest("Content from first source", source_name="source-a")
            result = store.search("content")

        for r in result["results"]:
            assert r["source"] == "source-a"

    def test_search_clamps_top_k(self, store):
        with patch("tools.knowledge_store.embed_texts", side_effect=_fake_embed):
            store.ingest("x " * 200, source_name="big")
            result = store.search("x", top_k=3)

        assert len(result["results"]) <= 3


class TestKnowledgeStoreManage:
    def test_list_sources_empty(self, store):
        result = store.list_sources()
        assert result["success"] is True
        assert result["sources"] == []
        assert result["total_sources"] == 0

    def test_list_sources_populated(self, store):
        with patch("tools.knowledge_store.embed_texts", side_effect=_fake_embed):
            store.ingest("Content A", source_name="a")
            store.ingest("Content B", source_name="b")

        result = store.list_sources()
        assert result["total_sources"] == 2
        names = {s["source_name"] for s in result["sources"]}
        assert names == {"a", "b"}

    def test_remove_source(self, store):
        with patch("tools.knowledge_store.embed_texts", side_effect=_fake_embed):
            store.ingest("Content to remove", source_name="doomed")
            store.ingest("Content to keep", source_name="keeper")

        result = store.remove_source("doomed")
        assert result["success"] is True
        assert result["remaining_sources"] == 1

        # Verify removed from disk
        index = json.loads((store.knowledge_dir / "index.json").read_text())
        assert len(index) == 1
        assert index[0]["source_name"] == "keeper"

    def test_remove_nonexistent_source(self, store):
        result = store.remove_source("ghost")
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_stats(self, store):
        with patch("tools.knowledge_store.embed_texts", side_effect=_fake_embed):
            store.ingest("Statistics test content", source_name="stats-doc")

        result = store.stats()
        assert result["success"] is True
        assert result["total_sources"] == 1
        assert result["total_chunks"] > 0
        assert result["max_chunks"] == 100
        assert result["embedding_model"] == "test-model"


class TestKnowledgeStorePersistence:
    def test_data_survives_reload(self, tmp_path):
        """Ingested data persists across store instances."""
        dir_ = tmp_path / "knowledge"

        with patch("tools.knowledge_store.embed_texts", side_effect=_fake_embed):
            store1 = KnowledgeStore(knowledge_dir=dir_, chunk_size=50, chunk_overlap=10)
            store1.ingest("Persistent data here", source_name="persist")

        # New store instance pointing at same directory
        store2 = KnowledgeStore(knowledge_dir=dir_, chunk_size=50, chunk_overlap=10)
        sources = store2.list_sources()
        assert sources["total_sources"] == 1
        assert sources["sources"][0]["source_name"] == "persist"

    def test_remove_persists(self, tmp_path):
        dir_ = tmp_path / "knowledge"

        with patch("tools.knowledge_store.embed_texts", side_effect=_fake_embed):
            store1 = KnowledgeStore(knowledge_dir=dir_, chunk_size=50, chunk_overlap=10)
            store1.ingest("Will be removed", source_name="temp")
            store1.remove_source("temp")

        store2 = KnowledgeStore(knowledge_dir=dir_, chunk_size=50, chunk_overlap=10)
        assert store2.list_sources()["total_sources"] == 0


# ===========================================================================
# Knowledge tool handlers
# ===========================================================================

class TestKnowledgeToolHandlers:
    def test_ingest_handler(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        # Reset singleton
        import tools.knowledge_tool as kt
        kt._store = None

        with patch("tools.knowledge_store.embed_texts", side_effect=_fake_embed):
            result = json.loads(kt.knowledge_ingest(
                source_name="test",
                content="Hello world test content for ingestion",
            ))

        assert result["success"] is True

    def test_search_handler(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        import tools.knowledge_tool as kt
        kt._store = None

        with patch("tools.knowledge_store.embed_texts", side_effect=_fake_embed):
            kt.knowledge_ingest(source_name="doc", content="Test content for searching")
            result = json.loads(kt.knowledge_search(query="test"))

        assert result["success"] is True

    def test_manage_list_handler(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        import tools.knowledge_tool as kt
        kt._store = None

        result = json.loads(kt.knowledge_manage(action="list"))
        assert result["success"] is True
        assert result["sources"] == []

    def test_manage_stats_handler(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        import tools.knowledge_tool as kt
        kt._store = None

        result = json.loads(kt.knowledge_manage(action="stats"))
        assert result["success"] is True
        assert "total_chunks" in result

    def test_manage_invalid_action(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        import tools.knowledge_tool as kt
        kt._store = None

        result = json.loads(kt.knowledge_manage(action="invalid"))
        assert result["success"] is False

    def test_ingest_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        import tools.knowledge_tool as kt
        kt._store = None

        # Create a test file
        test_file = tmp_path / "test_doc.md"
        test_file.write_text("# Title\n\nThis is a test markdown document with content.")

        with patch("tools.knowledge_store.embed_texts", side_effect=_fake_embed):
            result = json.loads(kt.knowledge_ingest(
                source_name="test-file",
                file_path=str(test_file),
            ))

        assert result["success"] is True

    def test_ingest_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        import tools.knowledge_tool as kt
        kt._store = None

        result = json.loads(kt.knowledge_ingest(
            source_name="ghost",
            file_path="/nonexistent/path.txt",
        ))
        assert result["success"] is False
        assert "not found" in result["error"].lower()


# ===========================================================================
# Config defaults
# ===========================================================================

class TestKnowledgeConfig:
    def test_config_section_exists(self):
        from logos_cli.config import DEFAULT_CONFIG
        assert "knowledge" in DEFAULT_CONFIG

    def test_config_defaults(self):
        from logos_cli.config import DEFAULT_CONFIG
        k = DEFAULT_CONFIG["knowledge"]
        assert k["embedding_model"] == "nomic-embed-text"
        assert k["chunk_size"] == 512
        assert k["chunk_overlap"] == 64
        assert k["max_chunks"] == 10_000

    def test_auto_ingest_defaults(self):
        from logos_cli.config import DEFAULT_CONFIG
        k = DEFAULT_CONFIG["knowledge"]
        assert k["auto_ingest_sessions"] is False
        assert k["auto_ingest_min_messages"] == 8


# ===========================================================================
# Auto-ingest from sessions
# ===========================================================================

class TestAutoIngestSession:
    def test_auto_ingest_disabled_by_default(self, tmp_path, monkeypatch):
        """When auto_ingest_sessions is False, no ingestion happens."""
        from gateway.run import GatewayRunner

        app = GatewayRunner.__new__(GatewayRunner)
        msgs = [{"role": "assistant", "content": "A" * 100}] * 10

        with patch("logos_cli.config.load_config", return_value={"knowledge": {"auto_ingest_sessions": False}}), \
             patch("tools.knowledge_store.embed_texts", side_effect=_fake_embed) as mock_embed:
            app._auto_ingest_session("test-session-123", msgs)

        # embed_texts should NOT have been called
        mock_embed.assert_not_called()

    def test_auto_ingest_when_enabled(self, tmp_path, monkeypatch):
        """When enabled, assistant messages are ingested into knowledge."""
        monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
        from gateway.run import GatewayRunner

        app = GatewayRunner.__new__(GatewayRunner)
        msgs = [
            {"role": "user", "content": "Tell me about Python"},
            {"role": "assistant", "content": "Python is a high-level programming language designed for readability."},
        ] * 5  # 10 messages total

        cfg = {
            "knowledge": {
                "auto_ingest_sessions": True,
                "auto_ingest_min_messages": 4,
                "embedding_model": "test-model",
                "chunk_size": 100,
                "chunk_overlap": 10,
                "max_chunks": 1000,
            }
        }

        with patch("logos_cli.config.load_config", return_value=cfg), \
             patch("tools.knowledge_store.embed_texts", side_effect=_fake_embed):
            app._auto_ingest_session("abc123def456", msgs)

        # Check knowledge was stored
        store = KnowledgeStore(knowledge_dir=tmp_path / "knowledge")
        sources = store.list_sources()
        assert sources["total_sources"] == 1
        assert sources["sources"][0]["source_name"].startswith("session-")
        assert sources["sources"][0]["source_type"] == "session"

    def test_auto_ingest_skips_short_sessions(self, tmp_path, monkeypatch):
        """Sessions shorter than min_messages are skipped."""
        monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
        from gateway.run import GatewayRunner

        app = GatewayRunner.__new__(GatewayRunner)
        msgs = [{"role": "assistant", "content": "Short response."}] * 3

        cfg = {"knowledge": {"auto_ingest_sessions": True, "auto_ingest_min_messages": 8}}

        with patch("logos_cli.config.load_config", return_value=cfg), \
             patch("tools.knowledge_store.embed_texts", side_effect=_fake_embed) as mock_embed:
            app._auto_ingest_session("short-session", msgs)

        mock_embed.assert_not_called()


# ===========================================================================
# Memory transfer / fork
# ===========================================================================

class TestMemoryFork:
    def test_fork_copies_memory(self, tmp_path):
        """Forking copies MEMORY.md to the target instance."""
        source = tmp_path / "instances" / "hermes-alice-researcher"
        target = tmp_path / "instances" / "hermes-alice-coder"

        # Create source with memory
        (source / "memories").mkdir(parents=True)
        (source / "memories" / "MEMORY.md").write_text("Alice likes Python.\n§\nPrefers pytest over unittest.")

        # Target exists but empty
        target.mkdir(parents=True)

        import shutil
        src_mem = source / "memories" / "MEMORY.md"
        tgt_mem_dir = target / "memories"
        tgt_mem_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src_mem), str(tgt_mem_dir / "MEMORY.md"))

        # Verify
        assert (target / "memories" / "MEMORY.md").exists()
        content = (target / "memories" / "MEMORY.md").read_text()
        assert "Alice likes Python" in content

    def test_fork_copies_knowledge(self, tmp_path):
        """Forking copies the entire knowledge directory."""
        source = tmp_path / "instances" / "hermes-bob-analyst"
        target = tmp_path / "instances" / "hermes-bob-writer"

        # Create source with knowledge
        with patch("tools.knowledge_store.embed_texts", side_effect=_fake_embed):
            store = KnowledgeStore(
                knowledge_dir=source / "knowledge",
                chunk_size=50, chunk_overlap=10,
            )
            store.ingest("Important analysis data here for testing", source_name="analysis")

        # Fork knowledge
        import shutil
        src_knowledge = source / "knowledge"
        tgt_knowledge = target / "knowledge"
        target.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(src_knowledge), str(tgt_knowledge))

        # Verify target has the knowledge
        target_store = KnowledgeStore(knowledge_dir=tgt_knowledge)
        sources = target_store.list_sources()
        assert sources["total_sources"] == 1
        assert sources["sources"][0]["source_name"] == "analysis"

    def test_fork_does_not_affect_source(self, tmp_path):
        """Forking should not modify the source instance."""
        source = tmp_path / "instances" / "source-agent"
        target = tmp_path / "instances" / "target-agent"

        (source / "memories").mkdir(parents=True)
        (source / "memories" / "MEMORY.md").write_text("Original content")

        import shutil
        tgt_mem_dir = target / "memories"
        tgt_mem_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(source / "memories" / "MEMORY.md"), str(tgt_mem_dir / "MEMORY.md"))

        # Modify target
        (target / "memories" / "MEMORY.md").write_text("Modified in target")

        # Source should be unchanged
        assert (source / "memories" / "MEMORY.md").read_text() == "Original content"
