"""
BIFROST Knowledge Base — pipeline.py
=====================================
FastAPI service providing document ingestion, RAG retrieval, and project/session
namespace management backed by ChromaDB.

Project namespacing:
  - Persistent projects: collection name = "proj_{name}"
  - Ephemeral sessions: collection name = "sess_{uuid}", auto-expire after SESSION_TTL_HOURS
  - Default project: "proj_default" (always available)

Endpoints:
  GET  /health
  GET  /stats?project=name
  GET  /projects
  POST /projects         {"name": "..."}
  DELETE /projects/{name}
  GET  /sessions
  POST /sessions         {"label": "..."}
  DELETE /sessions/{id}  — manual clear
  POST /upload?project=name  — multipart file upload
  POST /query            {"question": "...", "project": "name", "top_k": 5}
  POST /retrieve         {"question": "...", "project": "name", "top_k": 5}
  DELETE /documents/{filename}?project=name
"""

import os
import io
import uuid
import time
import logging
import asyncio
import threading
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import chromadb
from chromadb.config import Settings
from fastapi import FastAPI, UploadFile, File, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from langchain_text_splitters import RecursiveCharacterTextSplitter

# ─── Config ────────────────────────────────────────────────────────────────────
EMBED_API_BASE   = os.getenv("EMBED_API_BASE",   "http://host.docker.internal:11436/v1")
EMBED_MODEL      = os.getenv("EMBED_MODEL",      "nomic-embed-text")
# C2 TWIN-DISARM (2026-06-20): default CHAT to the Router (:8089), NOT direct Ollama (:11434).
# The old :11434 default was only saved by the live container env override (CHAT_API_BASE=
# 127.0.0.1:8080) -- an env-less redeploy would silently misroute chat to direct Ollama.
# Model default also moved to a Router-valid alias so the env-less default is internally coherent.
CHAT_API_BASE    = os.getenv("CHAT_API_BASE",    "http://host.docker.internal:8089/v1")
CHAT_MODEL       = os.getenv("CHAT_MODEL",       "bifrost-t2-gemma4")
KB_CHUNK_SIZE    = int(os.getenv("KB_CHUNK_SIZE",    "1000"))
KB_CHUNK_OVERLAP = int(os.getenv("KB_CHUNK_OVERLAP", "200"))
KB_TOP_K         = int(os.getenv("KB_TOP_K",         "5"))
KB_SETTLE_TIME   = int(os.getenv("KB_SETTLE_TIME",   "5"))
SESSION_TTL_HOURS= float(os.getenv("SESSION_TTL_HOURS", "4"))
CHROMA_PATH      = os.getenv("CHROMA_PATH",      "/data/chroma")
INBOX_PATH       = Path(os.getenv("INBOX_PATH",  "/data/inbox"))
PROCESSED_PATH   = Path(os.getenv("PROCESSED_PATH", "/data/processed"))
REJECTED_PATH    = Path(os.getenv("REJECTED_PATH",  "/data/rejected"))

# ─── Multi-inbox config ───────────────────────────────────────────────────────
# INBOX_PATHS: comma-separated "path:project" pairs for additional watched inboxes
# e.g. "/data/inbox-cloud-ai:cloud-ai,/data/inbox-rfp:rfp"
# The default INBOX_PATH always maps to "default"
_extra_inboxes_raw = os.getenv("INBOX_PATHS", "")
EXTRA_INBOXES = {}
if _extra_inboxes_raw.strip():
    for entry in _extra_inboxes_raw.split(","):
        entry = entry.strip()
        if ":" in entry:
            path, project = entry.rsplit(":", 1)
            EXTRA_INBOXES[Path(path.strip())] = project.strip()

# Auto-detect mounted inbox directories matching /data/inbox-*
for p in Path("/data").glob("inbox-*"):
    if p.is_dir() and p not in EXTRA_INBOXES:
        project_name = p.name.replace("inbox-", "")
        EXTRA_INBOXES[p] = project_name

# ─── Reranker Config ───────────────────────────────────────────────────────────
RERANKER_ENABLED = os.getenv("RERANKER_ENABLED", "true").lower() == "true"
RERANKER_MODEL   = os.getenv("RERANKER_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2")
RERANKER_TOP_K   = int(os.getenv("RERANKER_TOP_K", "5"))
RERANKER_FETCH_K = int(os.getenv("RERANKER_FETCH_K", "50"))

# Suppress ChromaDB telemetry capture() signature mismatch (0.5.x bug)
try:
    from chromadb.telemetry.product import ProductTelemetryClient
    ProductTelemetryClient.capture = lambda *args, **kwargs: None
except Exception:
    pass

SUPPORTED_EXTS = {".pdf", ".docx", ".md", ".txt"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bifrost-kb")

# ─── ChromaDB ──────────────────────────────────────────────────────────────────
chroma = chromadb.PersistentClient(
    path=CHROMA_PATH,
    settings=Settings(anonymized_telemetry=False),
)

# ─── Text splitter ─────────────────────────────────────────────────────────────
splitter = RecursiveCharacterTextSplitter(
    chunk_size=KB_CHUNK_SIZE,
    chunk_overlap=KB_CHUNK_OVERLAP,
    separators=["\n\n", "\n", ". ", " ", ""],
)

# ─── Reranker ──────────────────────────────────────────────────────────────────
_reranker = None

def get_reranker():
    global _reranker
    if _reranker is None and RERANKER_ENABLED:
        from sentence_transformers import CrossEncoder
        _reranker = CrossEncoder(RERANKER_MODEL)
        log.info(f"Reranker loaded: {RERANKER_MODEL}")
    return _reranker


def rerank_chunks(query: str, chunks: list[dict], top_k: int) -> list[dict]:
    """Rerank chunks using cross-encoder. Falls back to original order on error."""
    reranker = get_reranker()
    if not reranker or not chunks:
        return chunks[:top_k]
    try:
        pairs = [(query, c["text"]) for c in chunks]
        scores = reranker.predict(pairs)
        ranked = sorted(zip(scores, chunks), key=lambda x: x[0], reverse=True)
        result = [c for _, c in ranked[:top_k]]
        log.info(f"Reranked {len(chunks)} -> {len(result)} chunks")
        return result
    except Exception as e:
        log.warning(f"Reranker failed, using original order: {e}")
        return chunks[:top_k]

# ─── Session registry (in-memory, survives restarts via ChromaDB collection metadata) ──
# Sessions stored as collections with prefix "sess_"
# TTL enforced on startup + periodic cleanup

# ─── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI(title="BIFROST Knowledge Base", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# ─── Helpers ───────────────────────────────────────────────────────────────────

def collection_name(project: str, is_session: bool = False) -> str:
    prefix = "sess_" if is_session else "proj_"
    # ChromaDB collection names: alphanumeric + underscore + hyphen, 3-63 chars
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in project)
    return f"{prefix}{safe}"[:63]


def get_or_create_collection(name: str) -> chromadb.Collection:
    return chroma.get_or_create_collection(
        name=name,
        metadata={"hnsw:space": "cosine"},
    )


def list_projects() -> list[dict]:
    cols = chroma.list_collections()
    result = []
    for col in cols:
        if col.name.startswith("proj_"):
            name = col.name[5:]
            count = col.count()
            result.append({"name": name, "collection": col.name, "chunks": count, "type": "persistent"})
    return result


def list_sessions() -> list[dict]:
    cols = chroma.list_collections()
    result = []
    now = time.time()
    for col in cols:
        if col.name.startswith("sess_"):
            meta = col.metadata or {}
            created = meta.get("created_at", now)
            expires = created + SESSION_TTL_HOURS * 3600
            label = meta.get("label", col.name[5:])
            result.append({
                "id": col.name[5:],
                "label": label,
                "collection": col.name,
                "chunks": col.count(),
                "created_at": created,
                "expires_at": expires,
                "expires_in_minutes": max(0, int((expires - now) / 60)),
                "type": "session",
            })
    return result


def purge_expired_sessions():
    now = time.time()
    for col in chroma.list_collections():
        if col.name.startswith("sess_"):
            meta = col.metadata or {}
            created = meta.get("created_at", now)
            if now - created > SESSION_TTL_HOURS * 3600:
                log.info(f"Purging expired session: {col.name}")
                chroma.delete_collection(col.name)


async def embed_texts(texts: list[str]) -> list[list[float]]:
    batch_size = 32
    all_embeddings = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(
                f"{EMBED_API_BASE}/embeddings",
                json={"model": EMBED_MODEL, "input": batch},
            )
            resp.raise_for_status()
            data = resp.json()
            all_embeddings.extend([item["embedding"] for item in data["data"]])
    return all_embeddings


def extract_text(filename: str, content: bytes) -> str:
    ext = Path(filename).suffix.lower()
    if ext == ".txt" or ext == ".md":
        return content.decode("utf-8", errors="replace")
    elif ext == ".pdf":
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(content))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    elif ext == ".docx":
        import docx
        doc = docx.Document(io.BytesIO(content))
        return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
    else:
        raise ValueError(f"Unsupported file type: {ext}")


async def ingest_document(
    filename: str,
    content: bytes,
    project: str,
    is_session: bool = False,
) -> dict:
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTS:
        raise ValueError(f"Unsupported file type: {ext}. Supported: {', '.join(SUPPORTED_EXTS)}")

    log.info(f"Ingesting {filename} into project={project}")

    # Extract text
    text = extract_text(filename, content)
    if not text.strip():
        raise ValueError(f"No text extracted from {filename}")

    # Split into chunks
    chunks = splitter.split_text(text)
    log.info(f"{filename}: {len(chunks)} chunks")

    # Embed
    embeddings = await embed_texts(chunks)

    # Store in ChromaDB
    col_name = collection_name(project, is_session)
    col = get_or_create_collection(col_name)

    # Remove existing chunks for this file (re-index)
    try:
        existing = col.get(where={"source": filename})
        if existing["ids"]:
            col.delete(ids=existing["ids"])
            log.info(f"Removed {len(existing['ids'])} existing chunks for {filename}")
    except Exception:
        pass

    ids = [f"{filename}::{i}" for i in range(len(chunks))]
    col.add(
        ids=ids,
        embeddings=embeddings,
        documents=chunks,
        metadatas=[{"source": filename, "chunk": i, "ingested_at": time.time()} for i in range(len(chunks))],
    )

    return {"filename": filename, "chunks": len(chunks), "project": project}


async def retrieve_context(
    question: str,
    project: str,
    is_session: bool = False,
    top_k: int = KB_TOP_K,
) -> list[dict]:
    col_name = collection_name(project, is_session)
    try:
        col = chroma.get_collection(col_name)
    except Exception:
        # C2 LOUD-SIGNAL (2026-06-20): missing collection -> VISIBLE, never silent, never
        # cross-project (isolation is a hard rule). Surfaces wrong-project misconfig.
        log.warning(
            "retrieve_context: collection NOT FOUND col=%s project=%s -> 0 chunks "
            "(wrong-project misconfig? no cross-project fallback by design)",
            col_name, project,
        )
        return []

    if col.count() == 0:
        # C2 LOUD-SIGNAL: queried an EMPTY collection -> surface likely wrong-project misconfig.
        log.warning(
            "retrieve_context: queried EMPTY collection col=%s project=%s -> 0 chunks "
            "(wrong-project misconfig? no cross-project fallback by design)",
            col_name, project,
        )
        return []

    # Fetch more candidates for reranking when enabled
    fetch_k = RERANKER_FETCH_K if RERANKER_ENABLED else top_k
    q_embed = await embed_texts([question])
    results = col.query(
        query_embeddings=q_embed,
        n_results=min(fetch_k, col.count()),
        include=["documents", "metadatas", "distances"],
    )

    chunks = []
    for doc, meta, dist in zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    ):
        chunks.append({
            "text": doc,
            "source": meta.get("source", "unknown"),
            "chunk": meta.get("chunk", 0),
            "similarity": round(1 - dist, 3),
        })
    return chunks


# ─── Inbox watcher (multi-inbox support) ───────────────────────────────────────
async def _watch_single_inbox(inbox_path: Path, project: str):
    """Watch a single inbox directory and ingest files into the given project."""
    inbox_path.mkdir(parents=True, exist_ok=True)
    PROCESSED_PATH.mkdir(parents=True, exist_ok=True)
    REJECTED_PATH.mkdir(parents=True, exist_ok=True)
    log.info(f"Watching inbox: {inbox_path} -> project={project}")
    while True:
        try:
            for f in inbox_path.iterdir():
                if f.suffix.lower() in SUPPORTED_EXTS:
                    await asyncio.sleep(KB_SETTLE_TIME)
                    # Skip if already processed on a previous run
                    if (PROCESSED_PATH / f.name).exists():
                        log.info(f"Skipping already-processed: {f.name}")
                        try:
                            f.unlink()
                        except Exception:
                            pass
                        continue
                    try:
                        content = f.read_bytes()
                        await ingest_document(f.name, content, project)
                        # copy2_with_fallback: avoids cross-device rename on
                        # Windows Docker Desktop volume mounts (L:\ -> /data)
                        try:
                            (PROCESSED_PATH / f.name).write_bytes(f.read_bytes())
                            f.unlink()
                            log.info(f"Processed: {f.name} -> project={project}")
                        except Exception as move_err:
                            log.warning(f"Ingested {f.name} but move failed: {move_err} -- file left in inbox")
                    except Exception as e:
                        log.error(f"Failed {f.name}: {e}")
                        try:
                            (REJECTED_PATH / f.name).write_bytes(f.read_bytes())
                            f.unlink()
                            (REJECTED_PATH / f"{f.name}.error.txt").write_text(str(e))
                        except Exception as rej_err:
                            log.warning(f"Could not move {f.name} to rejected: {rej_err}")
        except Exception as _watch_err:
            log.error(f"watch_inbox({inbox_path}) error (retrying in 30s): {_watch_err}")
            await asyncio.sleep(30)
            continue
        await asyncio.sleep(10)


async def watch_inbox():
    """Start watchers for all configured inbox directories."""
    # Default inbox
    tasks = [asyncio.create_task(_watch_single_inbox(INBOX_PATH, "default"))]
    # Extra inboxes (from INBOX_PATHS env or auto-detected /data/inbox-* mounts)
    for path, project in EXTRA_INBOXES.items():
        tasks.append(asyncio.create_task(_watch_single_inbox(path, project)))
    if EXTRA_INBOXES:
        log.info(f"Multi-inbox active: {len(EXTRA_INBOXES)} extra inbox(es)")
    await asyncio.gather(*tasks)


@app.on_event("startup")
async def startup():
    # Ensure default project collection exists
    get_or_create_collection("proj_default")
    # Purge expired sessions
    purge_expired_sessions()
    # Start inbox watcher (covers default + extra inboxes)
    asyncio.create_task(watch_inbox())
    log.info("BIFROST Knowledge Base ready")


# ─── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    cols = chroma.list_collections()
    return {
        "status": "ok",
        "collections": len(cols),
        "embed_model": EMBED_MODEL,
        "chat_model": CHAT_MODEL,
    }


@app.get("/stats")
async def stats(project: str = "default"):
    cols = chroma.list_collections()
    result = {"projects": list_projects(), "sessions": list_sessions()}
    # Add specific project stats if requested
    col_name = collection_name(project)
    try:
        col = chroma.get_collection(col_name)
        sources = set()
        data = col.get(include=["metadatas"])
        for m in data["metadatas"]:
            if m:
                sources.add(m.get("source", "unknown"))
        result["active_project"] = {
            "name": project,
            "chunks": col.count(),
            "documents": list(sources),
        }
    except Exception:
        result["active_project"] = {"name": project, "chunks": 0, "documents": []}
    return result


class ProjectCreate(BaseModel):
    name: str


@app.get("/projects")
async def get_projects():
    return list_projects()


@app.post("/projects")
async def create_project(body: ProjectCreate):
    col_name = collection_name(body.name)
    col = get_or_create_collection(col_name)
    return {"name": body.name, "collection": col_name, "chunks": col.count()}


@app.delete("/projects/{name}")
async def delete_project(name: str):
    if name == "default":
        raise HTTPException(status_code=400, detail="Cannot delete the default project")
    col_name = collection_name(name)
    try:
        chroma.delete_collection(col_name)
        return {"deleted": name}
    except Exception:
        raise HTTPException(status_code=404, detail=f"Project not found: {name}")


class SessionCreate(BaseModel):
    label: Optional[str] = None


@app.get("/sessions")
async def get_sessions():
    purge_expired_sessions()
    return list_sessions()


@app.post("/sessions")
async def create_session(body: SessionCreate):
    sess_id = str(uuid.uuid4())[:8]
    col_name = f"sess_{sess_id}"
    label = body.label or f"Session {sess_id}"
    now = time.time()
    col = chroma.get_or_create_collection(
        name=col_name,
        metadata={
            "hnsw:space": "cosine",
            "created_at": now,
            "label": label,
        },
    )
    return {
        "id": sess_id,
        "label": label,
        "collection": col_name,
        "expires_at": now + SESSION_TTL_HOURS * 3600,
        "expires_in_minutes": int(SESSION_TTL_HOURS * 60),
    }


@app.delete("/sessions/{sess_id}")
async def delete_session(sess_id: str):
    col_name = f"sess_{sess_id}"
    try:
        chroma.delete_collection(col_name)
        return {"deleted": sess_id}
    except Exception:
        raise HTTPException(status_code=404, detail=f"Session not found: {sess_id}")


@app.post("/upload")
async def upload_file(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    project: str = Query("default"),
    session: Optional[str] = Query(None),
):
    ext = Path(file.filename).suffix.lower()
    if ext not in SUPPORTED_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {ext}. Supported: {', '.join(SUPPORTED_EXTS)}"
        )

    content = await file.read()
    is_session = session is not None
    proj = session if is_session else project

    try:
        result = await ingest_document(file.filename, content, proj, is_session)
        return {"status": "indexed", **result}
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))


class QueryRequest(BaseModel):
    question: str
    project: str = "default"
    session: Optional[str] = None
    top_k: int = KB_TOP_K


@app.post("/retrieve")
async def retrieve(req: QueryRequest):
    """Retrieval only — no generation. For Router integration."""
    is_session = req.session is not None
    proj = req.session if is_session else req.project
    chunks = await retrieve_context(req.question, proj, is_session, req.top_k)
    reranked = False
    if RERANKER_ENABLED and chunks:
        chunks = rerank_chunks(req.question, chunks, req.top_k)
        reranked = True
    return {"question": req.question, "project": proj, "chunks": chunks, "reranked": reranked}


@app.get("/reranker/status")
async def reranker_status():
    return {
        "enabled": RERANKER_ENABLED,
        "model": RERANKER_MODEL if RERANKER_ENABLED else None,
        "fetch_k": RERANKER_FETCH_K,
        "top_k": RERANKER_TOP_K,
    }


@app.post("/query")
async def query(req: QueryRequest):
    """Full RAG — retrieve + generate answer."""
    is_session = req.session is not None
    proj = req.session if is_session else req.project
    chunks = await retrieve_context(req.question, proj, is_session, req.top_k)

    if not chunks:
        context = "No relevant documents found in the knowledge base."
    else:
        context = "\n\n---\n\n".join(
            f"[{c['source']}]\n{c['text']}" for c in chunks
        )

    prompt = f"""You are a helpful assistant with access to a knowledge base.
Use the following context to answer the question. If the context doesn't contain
the answer, say so clearly.

CONTEXT:
{context}

QUESTION: {req.question}

ANSWER:"""

    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(
            f"{CHAT_API_BASE}/chat/completions",
            json={
                "model": CHAT_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
            },
        )
        resp.raise_for_status()
        answer = resp.json()["choices"][0]["message"]["content"]

    return {
        "question": req.question,
        "answer": answer,
        "project": proj,
        "sources": list({c["source"] for c in chunks}),
        "chunks_used": len(chunks),
    }


@app.delete("/documents/{filename}")
async def delete_document(filename: str, project: str = Query("default"), session: Optional[str] = Query(None)):
    is_session = session is not None
    proj = session if is_session else project
    col_name = collection_name(proj, is_session)
    try:
        col = chroma.get_collection(col_name)
        existing = col.get(where={"source": filename})
        if not existing["ids"]:
            raise HTTPException(status_code=404, detail=f"Document not found: {filename}")
        col.delete(ids=existing["ids"])
        return {"deleted": filename, "chunks_removed": len(existing["ids"])}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
