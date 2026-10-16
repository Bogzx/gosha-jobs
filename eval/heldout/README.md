# Held-out ranking test set

An independent test set for the offline ranking eval (`docs/EVAL.md`,
`gosha/evaluation.py`). It shares no posting and no persona with
`eval/postings.jsonl` / `eval/personas.jsonl`, so a ranking change tuned on
the main set can be checked here for overfitting. It uses the same schema
and loads with the same functions:

```bash
.venv/bin/python -c "from gosha import evaluation as e; from pathlib import Path; \
p=e.load_postings(Path('eval/heldout/postings.jsonl')); \
u=e.load_personas(Path('eval/heldout/personas.jsonl')); print(len(p), len(u))"
# 253 10
```

Keep it held out: look at the aggregate numbers, not at which postings a
change moves up or down, or it stops being a test set.

## Postings (`postings.jsonl`, 253)

- **Collected 2026-10-16** with the production adapters (eJobs, BestJobs,
  Hipo, RemoteOK, Jobicy), through a copy of `scripts/eval_collect.py`
  that changes only the search list and the output path. The copy is
  [`collect.py`](collect.py); it writes the unlabelled candidate pool. Detail fetches are capped at 6 per search, as in the
  original script, and only postings with a description of at least 200
  characters are kept.
- **Disjoint from the main set:** anything whose URL or title + company
  is already in `eval/postings.jsonl` was skipped at collection time, and
  that was checked again on the final file (0 overlaps either way).
- **Scrubbed like the main set:** e-mail addresses, links and phone
  numbers are replaced with `[email]`, `[link]` and `[phone]`, and
  descriptions are capped at 2,000 characters. Otherwise the text is what
  the adapters return, including Hipo's HTML entities (`&nbsp;`, `&amp;`)
  and the zero-width characters in eJobs descriptions, because that is
  what production embeds.
- **IDs** are `heldout-<source>-NNN`, so they never collide with the main
  set's `<source>-NNN`.

### Searches (keyword, location): 56 in total

| Group | Searches |
|---|---|
| Role searches (33) | python developer (Cluj), java developer (Bucuresti), c# developer (Iasi), frontend developer (Timisoara), node.js, php developer, full stack (Cluj), mobile developer, kotlin, react native, data engineer (Bucuresti), business intelligence, power bi, data scientist, ai engineer, c++ (Iasi), firmware, automotive software (Timisoara), cloud engineer, site reliability, kubernetes, qa automation (Cluj), tester manual, cyber security, helpdesk, suport it, business analyst, project manager, contabil (Cluj), economist, agent vanzari, call center (Iasi), operator date |
| Entry level (16) | intern (Bucuresti), internship it (Cluj), practica, absolvent, entry level, junior software (Timisoara), junior .net, junior frontend (Iasi), junior data analyst, junior tester, junior android, trainee, graduate program, junior machine learning, junior c++, stagiu (Bucuresti) |
| Remote (7) | python, developer, data, devops, frontend, qa, machine learning, support, all with location `remote` (this is what turns on RemoteOK and Jobicy) |

A search with no city named used `romania`.

### Selection

The searches returned 675 new candidates. I sorted them by title, read
the full text of about 300, and selected 253 to get the following:

- every family in `evaluation.FAMILIES` represented;
- a realistic share of senior roles (70), which act as same-family
  distractors;
- at least 4 grade-2 postings for every held-out persona, and for every
  persona in the main set;
- about 30% Romanian (75 of 253).

Postings I read but left out:

- 3 written in French or Italian (the eval only splits `en` and `ro`);
- 1 internal sales memo (in Spanish) listed as a RemoteOK job;
- 1 MLM-style "independent contractor" ad;
- 2 graduate or internship programmes too generic to give a family;
- 6 Schaeffler postings whose first 2,000 characters are only company
  boilerplate;
- 2 Booking Holdings "Software Engineer I/II" postings that have only
  boilerplate and a title with no stack;
- a generic "software engineer, any of C/C#/Java/.NET" posting;
- near-duplicates (the same text under two titles or with two languages
  asked);
- 38 redundant ones, mostly English senior or remote roles plus some
  Romanian call-centre, sales and accounting ads, cut to keep the balance
  above.

### Labels

**I (Claude, an AI agent) assigned the labels on 2026-10-16 from each
posting's full text, not its title. No person has reviewed them.** The
rules are the ones in `gosha/evaluation.py` and `docs/EVAL.md`.

- **families**: one or more from `FAMILIES`. A posting gets a second
  family only when the text really asks for both, for example
  `.NET + React` gives `dotnet, fs`.
- **level**, judged from the text only. The board's own level
  (eJobs/BestJobs/Jobicy metadata) is ignored, because it was often wrong:
  "Senior Platform Engineer" filed as entry level, "Technical Analyst I"
  filed as senior.
  - `intern`: internship, working student, stagiu or practică.
  - `junior`: junior, entry level, absolvent, graduate programme or
    "early-career" in the title or text, or 0–2 years asked
    (1–2 or 0–3 also count).
  - `mid`: 2–4 years asked (3–5 and 4+ included), a "Medior", "Middle" or
    "Mid" title, or experience demanded without a number ("experienced",
    "solid experience", "cu experiență").
  - `senior`: 5+ years asked, or a senior, lead, staff, principal,
    architect, head, team-lead or manager-of-a-team title or text. A
    "Senior" title wins over a lower number of years.
  - `any`: no level signal at all, or experience is "a plus" only, or the
    posting says any seniority.
- **lang**: the language of the role text itself (duties and
  requirements). Several Hipo, Vodafone and PPC postings open with a
  Romanian company paragraph and then describe the role in English; those
  are `en`.
- **note**: present on 195 postings. It records the evidence behind the
  level (for example "3-5 years") and any ambiguity. Three kinds of note
  matter most for a human review:
  - **"description is company boilerplate"** (5 postings: Booking
    Holdings, ING Hubs, Stripe). These are the only postings labelled
    from the title. They are kept on purpose, because
    boilerplate is a failure mode the ranker has to handle.
  - "years truncated" or "years not in text": the requirements fall after
    the 2,000-character cap.
  - Edge cases of family, for example a PLC/SCADA role labelled `emb`, a
    data-entry operator labelled `other`, or a low-code AI role labelled
    `ml`.

### Counts

| family | postings | | level | postings | | lang / source | postings |
|---|---|---|---|---|---|---|---|
| `py` | 12 | | `intern` | 17 | | `en` | 178 |
| `java` | 13 | | `junior` | 36 | | `ro` | 75 (29.6%) |
| `dotnet` | 9 | | `any` | 61 | | bestjobs | 102 |
| `be` | 20 | | `mid` | 69 | | ejobs | 81 |
| `fe` | 14 | | `senior` | 70 | | hipo | 49 |
| `fs` | 28 | | | | | jobicy | 18 |
| `mob` | 8 | | | | | remoteok | 3 |
| `ml` | 22 | | | | | | |
| `data` | 36 | | | | | | |
| `emb` | 9 | | | | | | |
| `ops` | 31 | | | | | | |
| `qa` | 10 | | | | | | |
| `sec` | 11 | | | | | | |
| `support` | 21 | | | | | | |
| `ba` | 10 | | | | | | |
| `acc` | 19 | | | | | | |
| `sales` | 14 | | | | | | |
| `cs` | 21 | | | | | | |
| `other` | 12 | | | | | | |

Family counts are multi-label: 59 postings carry more than one family.
Language by level:

| | intern | junior | any | mid | senior |
|---|---|---|---|---|---|
| en | 15 | 21 | 43 | 41 | 58 |
| ro | 2 | 15 | 18 | 28 | 12 |

## Personas (`personas.jsonl`, 10)

These are new invented people with different wording from the main set's
14. The CVs are 1,600–2,000 characters each, with sections (profile,
education, experience, projects, skills, languages), like real student
CVs. Three are in Romanian. Every family has an `ADJACENT` rule in
`evaluation.py`.

| id | lang | family | saved search (`keyword`) | grade-2 postings | grade-1 postings |
|---|---|---|---|---|---|
| `h-ml` | en | ml | ai engineer | 15 | 32 |
| `h-data-ro` | ro | data | analist date | 23 | 34 |
| `h-ops` | en | ops | cloud engineer | 17 | 54 |
| `h-qa-ro` | ro | qa | tester software | 6 | 4 |
| `h-fe` | en | fe | react developer | 9 | 22 |
| `h-mob` | en | mob | android developer | 5 | 24 |
| `h-emb` | en | emb | embedded c developer | 6 | 3 |
| `h-dotnet` | en | dotnet | c# developer | 4 | 36 |
| `h-java` | en | java | junior java developer | 7 | 34 |
| `h-cs-ro` | ro | cs | relatii clienti | 20 | 27 |

`h-cs-ro` is the non-IT control. On this posting set, the main set's 14
personas also have at least 4 grade-2 postings each (py 5, java 7,
dotnet 4, fe 9, mob 5, ml 15, data 23, emb 6, ops 17, qa 6, acc-ro 16,
cs 20), so they can be run here too.

## Limitations

- **Small.** `dotnet` (4), `mob` (5), `qa` and `emb` (6 each) have few
  relevant postings, so their per-persona numbers are noisy. Read
  differences under about 0.1 nDCG@10 as noise, as with the main set.
- **One annotator, an AI agent, on one day of the boards.** The labels
  need a human pass, just like the main set's.
- **The same boards as the main set**, two weeks later. Some employers'
  boilerplate (SCOR, Levi9, Siemens Energy, PPC) recurs across both sets.
  This tests generalisation to new postings, not to new boards.
- **Sanity check:** the TF-IDF baseline gives feed nDCG@10 0.513
  (95% CI 0.384–0.628) on this set with these personas.
