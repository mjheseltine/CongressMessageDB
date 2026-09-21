# SCCC public data site

Static website + release pipeline for the Scaled and Classified Congressional Communication dataset.
No server: the site is plain HTML/JS served by GitHub Pages from this repo, and the bulk files are
attached to a GitHub Release of the same repo.

- Repo: https://github.com/mjheseltine/CongressMessageDB
- Site (once Pages is on): https://mjheseltine.github.io/CongressMessageDB/
- Bulk files: https://github.com/mjheseltine/CongressMessageDB/releases

```
prepare_data.py     builds everything below from the three internal CSVs
build_demo.py       inlines summary.csv + manifest.json into one HTML file (for previews/sharing drafts)
docs/               GitHub Pages root
  index.html        about, explorer, download table, citation (single page)
  summary.csv       member × Congress × platform aggregates (generated)
  members.csv       member-session attributes (generated)
  manifest.json     list of bulk files with sizes (generated)
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
  --out release --site docs --version v1.0 \
  --voteview    /path/HSall_members.csv      # optional: session-specific party/chamber/state/district
```

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

`--voteview` replaces party / chamber / state / district with Voteview's values keyed on
(icpsr, congress) — https://voteview.com/data → "Member Ideology", all congresses, CSV — and
records which rows were corrected in `members.csv` → `Source`.

Before publishing: fill in the two `[Authors: add definition]` placeholders in `docs/codebook.md`
(`CreditConstituent`, `CreditPolicy`) and confirm the licence line in the site footer.

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

Re-run `prepare_data.py` with a new `--version`, `gh release create v1.1 ...`, update
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
