import asyncio, json, collections, re, httpx, sys
from gosha.scrapers.base import DEFAULT_HEADERS
P=[json.loads(l) for l in open("eval/postings.jsonl")]
by=collections.defaultdict(list)
for p in P: by[p["source"]].append(p["url"])
MARK=re.compile(r"(?i)(expirat|nu mai este (?:activ|disponibil)|no longer available|has expired|job expired|anun[tț]ul a expirat|oferta a expirat|position (?:has been )?filled|nu mai e valabil)")
async def main():
    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=15) as c:
        for src, urls in by.items():
            for u in urls[:12]:
                try:
                    h=await c.head(u, follow_redirects=True)
                    g=await c.get(u, follow_redirects=True)
                    m=MARK.search(g.text)
                    print(src, h.status_code, g.status_code, "redir" if str(g.url)!=u else "-", (m.group(0) if m else ""), u[:90], "->", str(g.url)[:70] if str(g.url)!=u else "")
                except Exception as e:
                    print(src, "ERR", type(e).__name__, u[:80])
asyncio.run(main())
