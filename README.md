# SCCC public data site

Static website + release pipeline for the Scaled and Classified Congressional Communications dataset.
No server: the site is plain HTML/JS served by GitHub Pages from this repo, and the bulk files are
attached to a GitHub Release of the same repo.

- Repo: https://github.com/mjheseltine/CongressMessageDB
- Site (once Pages is on): https://mjheseltine.github.io/CongressMessageDB/
- Bulk files: https://github.com/mjheseltine/CongressMessageDB/releases

```
prepare_data.py     builds everything below from the three internal CSVs (never reads text)
prepare_keywords.py builds the "distinctive words" data for the explorer (the only script that reads text)
build_demo.py       inlines the generated data into one HTML file (for previews/sharing drafts)
docs/               GitHub Pages root
  index.html        about, explorer, download table, citation (single page)
  summary.csv       member × Congress × platform aggregates (generated)
  members.csv       member-session attributes (generated)
  manifest.json     list of bulk files with sizes (generated)
  words_*.json      aggregate term counts for the explorer's word features (generated, optional)
  codebook.md       variable definitions
release/            NOT committed (.gitignore) — attached to the GitHub Release
  data/*.csv.gz, *.parquet
  members.csv
```

## 1. Build the release

```bash
pip install pandas pyarrow
python prepare_data.py \
  --tweets      /path/Tweets.csv \
  --facebook    /path/Facebook_Posts.csv \
  --newsletters /path/Newsletters.csv \
  --master      "/path/Member-Session Data (v5.4).csv" \
  --out release --site docs --version v1.1
```

Keep the master file with the other internal files, outside the repo.

The script loads each internal file fully into memory (pandas); the tweet file (~0.8 GB CSV)
needs roughly 4–6 GB of RAM. Message text is never read: the script only loads the columns
listed in `COLS_READ` (identifiers, dates, engagement, labels, scores, member attributes), so
the internal files with text can be used as-is.

Newsletters come out at two levels: `newsletters_<congress>` (one row per newsletter; a category
is 1 if any sentence carries it, scores are sentence means) and `newsletter_sentences_<congress>`
(one row per sentence bigram, as classified). The explorer, `summary.csv` and `members.csv` use the
sentence level for newsletters, as the article does, so category shares are comparable across platforms.

`members.csv` carries each member-session's message counts, category proportions and mean partisan
scores, pooled (no suffix) and per platform (`Twt`, `FB`, `NL` suffixes), so it works on its own as a
member-level dataset.

`--master` is the project's member-session file and is the ground truth for name, party, chamber,
state and district. The log reports member-sessions that have messages but are not in the master
(typically posts from before a member took office, or an unmapped ICPSR ID); rerun with
`--drop-unmatched` to exclude those messages from the release once you've reviewed the list.
Voteview's alternate ICPSR IDs in the message files (post-switch and returning-member IDs) are
folded into the master's IDs by the `ICPSR_ALIASES` table at the top of the script; extend it if
the log shows more. If the master has optional `TermStart` / `TermEnd` columns (YYYY-MM-DD, blank
where not needed), messages dated outside them are dropped, which lets you include members who
joined or left mid-session without sweeping in their pre- or post-Congress posts.

Party for the six mid-session switchers: `members.csv` carries the master's Party (longest
affiliation of the session) plus `PartySwitch` and `PartySwitchDate`; `summary.csv` and the explorer
assign party to each message by date, so a switcher has one row per party in that session.

Before publishing: fill in the two `[Authors: add definition]` placeholders in `docs/codebook.md`
(`CreditConstituent`, `CreditPolicy`) and confirm the licence line in the site footer.

## 1b. Build the distinctive-words data (optional)

The explorer shows the terms most distinctive of each category and of each band of the partisan
scale, updating with the platform / Congress / party / chamber filters. That needs aggregate term
counts, produced by a separate script that reads the message text:

```bash
pip install scikit-learn
python prepare_keywords.py \
  --tweets      /path/Tweets.csv \
  --facebook    /path/Facebook_Posts.csv \
  --newsletters /path/Newsletters.csv \
  --members docs/members.csv --site docs
```

Run it after `prepare_data.py` (it takes party and chamber from `docs/members.csv`). It reads each
file twice in chunks, takes 30-45 minutes in total, and writes `docs/words_twitter.json`,
`words_facebook.json` and `words_newsletters.json` (a few MB each; commit them). Its output is
counts of how many messages contain each common unigram or bigram, by Congress x party x chamber
cell, overall, per category and per score band; terms found in fewer than 25 messages (`--min-df`)
are dropped, so no output row can be traced to a message. No text is written. Members with party
"Other" are excluded from the counts.

The site works without these files: if they are absent the word features simply don't appear.
Knobs: `--vocab` (terms kept, default 4000), `--per-slice` (terms stored per cell and category,
default 500; lower it to shrink the files), `--min-df`.

## 2. Attach the bulk files to a GitHub Release

Each file must be under 2 GB (yours will be far smaller); there is no limit on total size or
downloads. With the GitHub CLI (`gh auth login` once):

```bash
gh release create v1.0 release/data/* release/members.csv \
   --repo mjheseltine/CongressMessageDB --title "SCCC v1.0" --notes "Public release, no message text."
```

Or in the browser: repo → Releases → *Draft a new release* → tag `v1.0` → drag the files in.

`DATA_BASE_URL` near the top of the `<script>` in `docs/index.html` is already set to
`https://github.com/mjheseltine/CongressMessageDB/releases/download/v1.0/`; change the tag
when you cut a new release.

## 3. Publish the site

1. Commit `docs/` (including the generated `summary.csv`, `members.csv`, `manifest.json`; a few MB).
2. Repo → Settings → Pages → Source: *Deploy from a branch* → `main` / `/docs` → Save.
3. The site appears at https://mjheseltine.github.io/CongressMessageDB/ within a couple of minutes.

A custom domain (Settings → Pages → Custom domain) is optional.

## 4. Updating later

Re-run `prepare_data.py` with a new `--version`, `gh release create v1.2 ...`, update
`DATA_BASE_URL` to the new tag, commit the regenerated `docs/*.csv` + `manifest.json`, and update the
Harvard Dataverse record so the DOI resolves to the current version. The version shows in the footer.

## 5. Phase 2 (optional): filtered message-level downloads in the browser

If people ask for "all of Rep. X's tweets" without downloading a whole Congress, DuckDB-WASM can
query the Parquet files directly from the browser via HTTP range requests, no server needed.
This needs CORS and range-request support on the file host; Cloudflare R2 (10 GB free, no egress
fees) is the usual choice for that, so phase 2 would mean mirroring the Parquet files there.
The `summary.csv` explorer stays as is.

## Local preview

`python -m http.server -d docs 8000` then open http://localhost:8000 — or
`python build_demo.py preview.html` for a single file you can open directly or send around.
