"""rag.py — RAG engine using LangChain + FAISS + HuggingFace embeddings

Pipeline
--------
  1. Clone repo (git clone --depth 1)
  2. Load relevant source files
  3. Chunk with language-aware splitter (splits on def/class/function boundaries)
  4. Embed with HuggingFaceEmbeddings all-MiniLM-L6-v2 (local, no API key)
  5. Store in FAISS index (local filesystem, persisted as .pkl + .faiss files)
  6. Retrieve: similarity_search top-k chunks for any query
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import AsyncGenerator

log = logging.getLogger("rag")

# ── Config ─────────────────────────────────────────────────────────────────────

RAG_ROOT = Path.home() / ".repomaster" / "rag"

CHUNK_SIZE    = 1500   # large enough to hold most function definitions
CHUNK_OVERLAP = 200
TOP_K         = 5

EMBED_MODEL = "all-MiniLM-L6-v2"

ALLOWED_EXTS = {
    ".py", ".md", ".txt", ".js", ".jsx", ".ts", ".tsx",
    ".java", ".cpp", ".c", ".h", ".go", ".rs", ".rb",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".sh",
    ".html", ".css", ".rst", ".vue", ".svelte",
}
SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules",
    "dist", "build", ".tox", ".eggs", "site-packages",
    ".mypy_cache", ".pytest_cache", "htmlcov", ".idea", ".vscode",
}
SKIP_FILES = {
    "yarn.lock", "package-lock.json", "poetry.lock",
    "uv.lock", "bun.lock", "pnpm-lock.yaml",
}
MAX_FILE_CHARS = 50_000
MAX_TOTAL_DOCS = 2000


# ── Slug & paths ───────────────────────────────────────────────────────────────

def _slug(repo: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", repo)

def _store_dir(repo: str) -> Path:
    return RAG_ROOT / _slug(repo)

def index_exists(repo: str) -> bool:
    d = _store_dir(repo)
    return (d / "index.faiss").exists() and (d / "index.pkl").exists()

def index_info(repo: str) -> dict:
    if not index_exists(repo):
        return {"exists": False}
    try:
        store = _load_store(repo)
        count = store.index.ntotal
        return {"exists": True, "chunks": count}
    except Exception:
        return {"exists": True, "chunks": "?"}


# ── Embeddings (lazy singleton) ────────────────────────────────────────────────

_embeddings = None

def _get_embeddings():
    global _embeddings
    if _embeddings is None:
        import warnings
        warnings.filterwarnings("ignore")
        from langchain_huggingface import HuggingFaceEmbeddings
        _embeddings = HuggingFaceEmbeddings(
            model_name=EMBED_MODEL,
            model_kwargs={"device": "cpu"},
            encode_kwargs={"normalize_embeddings": True},
        )
    return _embeddings


# ── FAISS store ────────────────────────────────────────────────────────────────

def _load_store(repo: str):
    import warnings
    warnings.filterwarnings("ignore")
    from langchain_community.vectorstores import FAISS
    return FAISS.load_local(
        str(_store_dir(repo)),
        _get_embeddings(),
        allow_dangerous_deserialization=True,
    )


# ── File loading ───────────────────────────────────────────────────────────────

def _should_include(path: Path) -> bool:
    for part in path.parts:
        if part in SKIP_DIRS:
            return False
    if path.name in SKIP_FILES:
        return False
    return path.suffix.lower() in ALLOWED_EXTS


def _load_documents(root: Path) -> list:
    from langchain_core.documents import Document
    docs = []
    for f in sorted(root.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(root)
        if not _should_include(rel):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        text = text.strip()
        if not text:
            continue
        if len(text) > MAX_FILE_CHARS:
            text = text[:MAX_FILE_CHARS]
        docs.append(Document(page_content=text, metadata={"source": str(rel)}))
        if len(docs) >= MAX_TOTAL_DOCS:
            break
    return docs


# ── Chunking ───────────────────────────────────────────────────────────────────

def _split_documents(docs: list) -> list:
    from langchain_text_splitters import RecursiveCharacterTextSplitter, Language

    lang_map = {
        "py": Language.PYTHON, "js": Language.JS, "jsx": Language.JS,
        "ts": Language.TS,     "tsx": Language.TS,
        "go": Language.GO,     "java": Language.JAVA,
        "cpp": Language.CPP,   "c": Language.CPP,   "h": Language.CPP,
        "rs": Language.RUST,   "rb": Language.RUBY,
    }
    _cache: dict[str, RecursiveCharacterTextSplitter] = {}

    def _get_splitter(ext: str) -> RecursiveCharacterTextSplitter:
        if ext not in _cache:
            if ext in lang_map:
                _cache[ext] = RecursiveCharacterTextSplitter.from_language(
                    language=lang_map[ext],
                    chunk_size=CHUNK_SIZE,
                    chunk_overlap=CHUNK_OVERLAP,
                )
            else:
                _cache[ext] = RecursiveCharacterTextSplitter(
                    chunk_size=CHUNK_SIZE,
                    chunk_overlap=CHUNK_OVERLAP,
                )
        return _cache[ext]

    chunks = []
    for doc in docs:
        ext = doc.metadata.get("source", "").rsplit(".", 1)[-1].lower()
        chunks.extend(_get_splitter(ext).split_documents([doc]))
    return chunks


# ── Retrieve ───────────────────────────────────────────────────────────────────

async def retrieve(repo: str, query: str, k: int = TOP_K) -> list[dict]:
    if not index_exists(repo):
        raise FileNotFoundError(f"No RAG index for {repo}. Build it first.")
    loop = asyncio.get_event_loop()
    store = await loop.run_in_executor(None, _load_store, repo)
    results = await loop.run_in_executor(
        None, lambda: store.similarity_search(query, k=k)
    )
    return [{"source": d.metadata.get("source", ""), "text": d.page_content} for d in results]


# ── Build index (SSE generator) ────────────────────────────────────────────────

async def build_index(repo_full_name: str) -> AsyncGenerator[dict, None]:
    tmp_dir = None
    try:
        # 1. Clone ─────────────────────────────────────────────────────────────
        yield {"type": "status", "stage": "cloning",
               "message": f"Cloning {repo_full_name}…"}
        await asyncio.sleep(0)

        tmp_dir = tempfile.mkdtemp(prefix="reposage_rag_")
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--depth", "1",
            f"https://github.com/{repo_full_name}.git", tmp_dir,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            yield {"type": "error",
                   "message": f"Clone failed: {stderr.decode(errors='replace')[:300]}"}
            return

        # 2. Load ──────────────────────────────────────────────────────────────
        yield {"type": "status", "stage": "loading", "message": "Loading source files…"}
        await asyncio.sleep(0)

        loop = asyncio.get_event_loop()
        raw_docs = await loop.run_in_executor(None, _load_documents, Path(tmp_dir))

        yield {"type": "status", "stage": "loading",
               "message": f"Loaded {len(raw_docs)} files."}
        await asyncio.sleep(0)

        if not raw_docs:
            yield {"type": "error", "message": "No source files found in repository."}
            return

        # 3. Chunk ─────────────────────────────────────────────────────────────
        yield {"type": "status", "stage": "chunking", "message": "Chunking with language-aware splitter…"}
        await asyncio.sleep(0)

        chunks = await loop.run_in_executor(None, _split_documents, raw_docs)
        chunks = chunks[:MAX_TOTAL_DOCS]

        yield {"type": "status", "stage": "chunking",
               "message": f"Created {len(chunks)} chunks from {len(raw_docs)} files."}
        await asyncio.sleep(0)

        # 4. Embed & build FAISS ───────────────────────────────────────────────
        total = len(chunks)
        yield {"type": "status", "stage": "embedding",
               "message": f"Embedding {total} chunks (loading model on first run)…"}
        await asyncio.sleep(0)

        # Wipe old index
        store_dir = _store_dir(repo_full_name)
        if store_dir.exists():
            shutil.rmtree(str(store_dir), ignore_errors=True)
        store_dir.mkdir(parents=True, exist_ok=True)

        def _build():
            import warnings
            warnings.filterwarnings("ignore")
            from langchain_community.vectorstores import FAISS
            store = FAISS.from_documents(chunks, _get_embeddings())
            store.save_local(str(store_dir))
            return store.index.ntotal

        # Stream fake progress while the thread runs
        task = loop.run_in_executor(None, _build)
        progress = 0
        while not task.done():
            await asyncio.sleep(2)
            progress = min(progress + int(total * 0.08), total - 1)
            yield {"type": "progress", "stage": "embedding",
                   "done": progress, "total": total,
                   "message": f"Embedding… ({progress}/{total} chunks)"}

        n_vectors = await task  # raises if _build() raised

        yield {"type": "done", "chunks": n_vectors,
               "message": f"Knowledge base ready — {n_vectors} chunks indexed."}

    except Exception as exc:
        log.exception("RAG build failed")
        yield {"type": "error", "message": f"Build failed: {exc}"}
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
