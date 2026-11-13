"""
RAG injection for bifrost-igor (v97 Path 2).
Opens inhouse chromadb persist dir directly (read-only), embeds query via
Forge nomic-embed-text, retrieves top-k from configured collections.

kb_runner remains canonical writer. Router is reader-only.
Chroma is hostPath-mounted at /data/inhouse-chroma (read-only).
chromadb client opens lazily on first call.
sqlite is in WAL mode (flipped 2026-05-01) for safe concurrent reader/writer.
"""
import os
import asyncio
import logging
from typing import Optional
import httpx

logger = logging.getLogger("bifrost.rag_inject_chromadb")

CHROMA_PATH = os.getenv("CHROMA_PATH", "/data/inhouse-chroma")
EMBED_URL = os.getenv("EMBED_URL", "http://192.168.x.4:11436/api/embeddings")  # Hearth Vega8 :11436 canonical embed (was Forge :11434 drift; embed belongs on Hearth, Forge is NPU/compute)
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")
EMBED_TIMEOUT = float(os.getenv("EMBED_TIMEOUT", "10.0"))
RAG_MAX_TOKENS = int(os.getenv("RAG_MAX_TOKENS", "2000"))

# Ceremony 9 (2026-09-22). Injection had NO off switch of any kind, and no relevance
# floor: the top_k nearest passages were injected however far away they were, then
# the model was told to cite them. Measured -- Igor cited "Passage 1" for a sha256
# present nowhere in the corpus.
#
# Default ON, so shipped behavior is unchanged. Once deployed, disengaging is an
# env edit plus a pod restart rather than another rebuild.
#
# An EMPTY value counts as ENABLED on purpose: an accidentally-blank env var must
# not silently switch off grounding fleet-wide. Only an explicit 0/false/off/no
# disables it.
RAG_INJECT_ENABLE = (os.getenv("RAG_INJECT_ENABLE", "1").strip().lower()
                     not in ("0", "false", "off", "no"))

# Ceremony 10 (2026-09-22). The relevance floor Ceremony 9 declined to add.
#
# Ceremony 9 rejected a threshold because the distributions overlap. That was the
# wrong test -- it demanded PERFECT separation, from 8 per-query minimums. Measured
# again per PASSAGE over 24 queries / 120 passages:
#     on-topic  n=60  min 0.239  median 0.284  max 0.300
#     nonsense  n=60  min 0.277  median 0.304  max 0.329
#     T=0.300 -> 100% of on-topic passages kept, 76.7% of nonsense rejected,
#     and ZERO on-topic queries left without grounding.
#
# 0.300 is the on-topic MAXIMUM in that sample, so it is fitted to the edge and an
# unseen query could fall outside it. RAG_MIN_PASSAGES makes a blackout impossible
# by construction: the nearest hit is always kept whatever its distance. One weak
# passage is survivable now that the framing below says a passage may be
# irrelevant; returning nothing at all would not be.
RAG_MAX_DISTANCE = float(os.getenv("RAG_MAX_DISTANCE", "0.30"))
RAG_MIN_PASSAGES = int(os.getenv("RAG_MIN_PASSAGES", "1"))

DEFAULT_COLLECTIONS = os.getenv(
    "RAG_COLLECTIONS",
    "inhouse_decisions,inhouse_pk,inhouse_architect_notes,inhouse_briefs",
).split(",")

_chroma_client = None
_collections_cache: dict = {}


def _get_chroma_client():
    global _chroma_client
    if _chroma_client is None:
        import chromadb
        from chromadb.config import Settings
        _chroma_client = chromadb.PersistentClient(
            path=CHROMA_PATH,
            settings=Settings(anonymized_telemetry=False, allow_reset=False),
        )
        logger.info(f"chromadb client opened: {CHROMA_PATH}")
    return _chroma_client


def _get_collection(name: str):
    if name not in _collections_cache:
        try:
            _collections_cache[name] = _get_chroma_client().get_collection(name)
        except Exception as e:
            logger.warning(f"collection {name} not available: {e}")
            _collections_cache[name] = None
    return _collections_cache[name]


async def _embed_query(query: str) -> Optional[list[float]]:
    try:
        async with httpx.AsyncClient(timeout=EMBED_TIMEOUT) as client:
            r = await client.post(
                EMBED_URL,
                json={"model": EMBED_MODEL, "prompt": query},
            )
            r.raise_for_status()
            data = r.json()
        return data.get("embedding")
    except Exception as e:
        logger.warning(f"embed failed: {e}")
        return None


async def inject_kb_context_async(
    messages: list[dict],
    user_prompt: str,
    top_k: int = 5,
    collections: Optional[list[str]] = None,
) -> list[dict]:
    """
    Retrieve top-k from configured collections, prepend as system message
    after baseline. On any failure, returns messages unchanged. Never raises.
    """
    if not RAG_INJECT_ENABLE:
        logger.info("RAG_INJECT: disabled by RAG_INJECT_ENABLE, skipping")
        return messages

    coll_names = collections or DEFAULT_COLLECTIONS

    query_embedding = await _embed_query(user_prompt)
    if query_embedding is None:
        logger.info("RAG_INJECT: embed failed, skipping")
        return messages

    all_hits = []
    loop = asyncio.get_event_loop()
    for coll_name in coll_names:
        coll = _get_collection(coll_name.strip())
        if coll is None:
            continue
        try:
            result = await loop.run_in_executor(
                None,
                lambda c=coll: c.query(query_embeddings=[query_embedding], n_results=top_k),
            )
            docs = result.get("documents", [[]])[0]
            metadatas = result.get("metadatas", [[]])[0]
            distances = result.get("distances", [[]])[0]
            for doc, meta, dist in zip(docs, metadatas, distances):
                all_hits.append({
                    "text": doc,
                    "source": (meta or {}).get("source", "(unknown)"),
                    "collection": coll_name.strip(),
                    "distance": dist,
                })
        except Exception as e:
            logger.warning(f"query {coll_name} failed: {e}")
            continue

    if not all_hits:
        logger.info(f"RAG_INJECT: zero hits across {len(coll_names)} collections, skipping")
        return messages

    all_hits.sort(key=lambda h: h["distance"])

    # Ceremony 10: drop passages beyond RAG_MAX_DISTANCE, but never return nothing.
    near = [h for h in all_hits if h["distance"] <= RAG_MAX_DISTANCE]
    if len(near) < RAG_MIN_PASSAGES:
        # Everything was far away. Keep the nearest few anyway and let the framing
        # below do the rest -- a silent zero-passage result is indistinguishable
        # from "the KB has nothing", which is the failure this whole line of work
        # has been correcting.
        near = all_hits[:RAG_MIN_PASSAGES]
    top_hits = near[:top_k]

    # The old title, "RETRIEVED KB CONTEXT", asserted a relevance this code has
    # never established -- nothing is filtered and nothing is checked.
    ctx_lines = [f"## POSSIBLY-RELEVANT KB PASSAGES (unfiltered similarity search over {len(coll_names)} collections; MAY NOT answer the question)"]
    budget = RAG_MAX_TOKENS * 4
    for i, h in enumerate(top_hits, start=1):
        line = (f"\n[Passage {i}, source: {h['source']} "
                f"collection={h['collection']} dist={h['distance']:.3f}]\n"
                f"{h['text']}\n")
        if len(line) > budget:
            break
        ctx_lines.append(line)
        budget -= len(line)
    # This previously read: "Use this context to ground your response. Cite sources
    # where claims rely on retrieved passages." That instruction is what manufactured
    # the false citations -- passages are selected by nearest-neighbor only, so a
    # model told to cite them will cite one whether or not it contains the answer.
    #
    # SUPERSEDED by Ceremony 10. That rejection was measured wrongly -- perfect
    # separation from 8 per-query minimums. Per PASSAGE over 120 passages, a cut at
    # 0.300 keeps 100% of on-topic passages and rejects 76.7% of nonsense with no
    # blackouts, so RAG_MAX_DISTANCE now exists. The framing below still matters:
    # the filter reduces noise, it does not certify relevance.
    ctx_lines.append(
        "\nThese passages were retrieved by similarity alone and MAY BE IRRELEVANT "
        "to the question -- no relevance check was performed and none of them may apply. "
        "Use a passage only if it LITERALLY CONTAINS what you need. If none does, say "
        "you do not have the information. Never cite a passage for a value it does not contain."
    )
    ctx_block = "\n".join(ctx_lines)

    if messages and messages[0].get("role") == "system":
        new_msgs = [
            messages[0],
            {"role": "system", "content": ctx_block},
        ] + messages[1:]
    else:
        new_msgs = [{"role": "system", "content": ctx_block}] + messages

    logger.info(
        f"RAG_INJECT: {len(top_hits)} passages from "
        f"{len(set(h['collection'] for h in top_hits))} collections "
        f"(top dist={top_hits[0]['distance']:.3f})"
    )
    return new_msgs
