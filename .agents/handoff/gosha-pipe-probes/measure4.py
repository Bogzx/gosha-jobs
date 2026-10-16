import json, collections
from gosha.filters import matches_experience_level
P=[json.loads(l) for l in open("eval/postings.jsonl")]
for desc in (False, True):
  for levels in (["intern","junior"],["junior"],["intern"]):
    tot=collections.Counter(); got=collections.Counter()
    for p in P:
        tot[p["level"]]+=1
        if matches_experience_level(p["title"], levels, None, p["description"] if desc else None): got[p["level"]]+=1
    print(f"desc={desc!s:5} levels={levels}: " + "  ".join(f"{l}={got[l]}/{tot[l]}" for l in ("intern","junior","any","mid","senior")))
