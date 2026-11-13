"""Turn a user prompt into a search-engine query, and pick the SearXNG category for it.

Added 2026-09-29 after the Converse probe "For 9/29/2026, what are the major headlines for US news?
Use web search" produced one junk hit (https://en.wikipedia.org/wiki/For_9). Two separate faults, both
measured against the live SearXNG on :8888:

1. The prompt was sent to the engine verbatim, meta-instruction and all. A search engine has no use
   for "Use web search".
2. The category was hardcoded to "general", which on this instance returns ZERO results for
   everything -- its engines report `brave: Suspended: too many requests`, `duckduckgo: CAPTCHA`,
   `startpage: Suspended: CAPTCHA`. Only wikipedia still answers there, which is why a news question
   came back with a Wikipedia disambiguation page.

Measured, same instance, same moment:

    query                      general      web          news
    ROCm release notes         0 results    200 (docs)   13 (wikinews, off-topic)
    copper spot price today    0 results    214 (prices) 25 (bing news, reuters)
    US news headlines          0 results    --           80 (bing news, reuters)

2026-09-29, later the same day: `general` was repaired at the SearXNG end (bing and naver
enabled; its own engines -- brave, duckduckgo, startpage -- are still CAPTCHA'd), so it was
re-measured and is now used in BOTH paths. Only the top `search_max_results` (3) ever reaches
the model, so the TOP 3 was compared rather than the totals:

    ROCm release notes   web          3/3 on target (rocm.docs release-notes pages)
                         general      weaker -- index and portal pages, no release-notes page
                         general,web  2 release-notes pages + the portal          <-- best
    copper spot price    web          price page, then JUNK (publicappeal.org, via wiby)
                         general      encyclopaedia first, the price page third
                         general,web  Wikipedia, the price page, Britannica       <-- best
    median latency       web 1.18s    general 0.60s    general,web 1.30s
                         (+0.1s over web is nothing against a 20-40s completion)
    news -> news,general 36 unique -> 59, 0.93s -> 0.89s, adds reuters and msn

A single run suggested news,general had displaced the best dated hit; repeating 3x put that story
in the top 3 **3 of 3 times under both**, so that was ranking noise, not an effect. Repeat before
believing a ranking difference.

So: news-shaped questions go to `news` ALONE, everything else to `general,web`. Neither category
is used alone -- `general` misses pages `web` finds, and `web` alone admits junk from wiby that
`general` does not.

CORRECTION 2026-09-29, measured after deploying news,general to v462: adding `general` to the NEWS path
DILUTES it. Over 5 runs of the router's own query, counting how many of the top 3 are dated news:
    news          2.8 / 3   (3,2,3,3,3)
    news,general  2.0 / 3   (2,2,2,3,1)  -- one run led with "9 - Wikipedia"
general's encyclopaedic engines match the "9" in "9/29/2026" and outrank real stories. An earlier 3-run
check had asked only whether the best URL was PRESENT -- it stayed present, which is why the dilution of
the other two slots went unseen. Measure the whole top 3, since that is what reaches the model.

This module is deliberately free of router imports so it can be exercised standalone.
"""
import re

# Meta-instructions aimed at the assistant, useless to a search engine.
_META = re.compile(
    r"\b(?:please|kindly|could you|can you|would you|tell me|i want to know|i need to know)\b"
    r"|\b(?:use|using|via|with|do|perform|run)?\s*(?:a\s+)?web\s*search\b"
    r"|\bsearch\s+(?:for|the\s+web(?:\s+for)?|online|the\s+internet)\b"
    r"|\bgoogle\s+(?:it|for)\b"
    r"|\bbrowse\s+the\s+(?:web|internet)\b"
    r"|\blook\s+(?:it\s+)?up(?:\s+online)?\b"
    r"|\bcheck\s+(?:online|the\s+web)\b"
    r"|\bfind\s+out\b"
    r"|\bdo\s+not\s+guess\b",
    re.IGNORECASE,
)

# Interrogative scaffolding. A query keeps the nouns, not the question frame.
_LEAD = re.compile(
    r"^(?:for\s+)?(?:what\s+(?:are|is|was|were)(?:\s+the)?|who\s+(?:is|are|won)"
    r"|when\s+(?:is|was|did)|where\s+(?:is|are)|how\s+(?:is|are|does|do|much|many)"
    r"|tell\s+me\s+about|give\s+me)\s+",
    re.IGNORECASE,
)

# Left-over function words after the strips above, e.g. "Use web search to find X" -> "to find X".
_LEAD_STOP = re.compile(r"^(?:to\s+find|to\s+get|to\s+see|about|on|for|the|a|an|to)\s+", re.IGNORECASE)

_NEWSY = re.compile(
    r"\b(?:news|headline|headlines|breaking|top\s+stor(?:y|ies)|current\s+events"
    r"|standings|scores?|election|who\s+won)\b",
    re.IGNORECASE,
)

# Bracketed tags: probe ids, run/batch counters, nonces. 2026-10-07: a cache-defeating
# nonce I appended myself, "[probe n1791420593]", went straight to the engine and the top 3
# came back CNN, AP News, Fox News -- 0 of 3 on target, against 2 of 3 without it. Square and
# curly brackets are stripped unconditionally (a user question does not put search terms in
# them); PARENTHESES only when the content looks like a tag, because "(USD)" or "(2nd ed)"
# can carry real terms.
_TAG_SQUARE_CURLY = re.compile(r"\s*[\[\{][^\]\}]{0,80}[\]\}]\s*")
_TAG_PAREN = re.compile(
    r"\s*\((?=[^)]{0,40}\))"
    r"(?=[^)]*(?:\b(?:probe|run|batch|trial|iter|iteration|seed|nonce|attempt|take|rev)\b"
    r"|=|#))"
    r"[^)]*\)\s*",
    re.IGNORECASE,
)

# Clauses aimed at the ASSISTANT that survive _META because they are not about searching.
# Each must be a trailing clause AND a directive verb phrase -- "how do I cite a dataset"
# and "the sources of nitrogen runoff" are content and must survive, which cases s11-s13
# of the gate pin.
_TRAILING_DIRECTIVE = re.compile(
    r"(?:[.?!;,]|\s)\s*(?:"
    r"cite\s+(?:the\s+|your\s+)?sources?(?:\s+you\s+(?:were\s+given|used))?"
    r"|show\s+your\s+(?:work|reasoning|sources?)"
    r"|explain\s+your\s+(?:answer|reasoning)"
    r"|(?:be|keep\s+it)\s+(?:concise|brief|specific|short)"
    r"|answer\s+(?:concisely|briefly)"
    r"|do\s+not\s+(?:guess|speculate)"
    r")\s*[.?!]*\s*$",
    re.IGNORECASE,
)

MAX_QUERY_CHARS = 200


def build_search_query(prompt: str) -> str:
    """Strip what a search engine cannot use, keep what it can.

    Falls back to the original prompt if stripping leaves too little to search for -- an empty or
    two-word query is worse than a verbose one.
    """
    raw = (prompt or "").strip()
    # Tags FIRST: a bracketed tag can sit AFTER a trailing directive
    # ("... Cite the sources you were given. [probe n17...]"), and the directive
    # pattern is end-anchored, so it cannot see past the bracket. Gate case s03.
    q = _TAG_SQUARE_CURLY.sub(" ", raw)
    q = _TAG_PAREN.sub(" ", q)
    q = _META.sub(" ", q)
    q = re.sub(r"\s{2,}", " ", q).strip(" \t\r\n?.!,;:-")
    q = _LEAD.sub("", q).strip()
    for _ in range(2):                       # e.g. "to find the current price" -> "current price"
        new = _LEAD_STOP.sub("", q).strip()
        if new == q:
            break
        q = new
    for _ in range(3):                       # "... Be concise. Cite your sources." -> both go
        new = _TRAILING_DIRECTIVE.sub("", q).strip()
        if new == q:
            break
        q = new
    q = re.sub(r"\s{2,}", " ", q).strip(" \t\r\n?.!,;:-")
    if len(q) < 8 or len(q.split()) < 2:
        return raw[:MAX_QUERY_CHARS]
    return q[:MAX_QUERY_CHARS]


def pick_search_category(prompt: str) -> str:
    """"news,general" for news-shaped questions, else "general,web".

    Both pairs beat either category alone on the top 3, which is all the router injects -- see
    the module docstring for the comparison.
    """
    return "news" if _NEWSY.search(prompt or "") else "general,web"
