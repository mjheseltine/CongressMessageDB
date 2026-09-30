#!/usr/bin/env python3
"""
prepare_keywords.py -- aggregate term statistics for the explorer's "distinctive words" features.

This is the ONLY script in the pipeline that reads message text, and it is kept separate from
prepare_data.py for that reason. It writes no text. Its output is document-frequency counts of
common terms (unigrams and bigrams): for each platform x Congress x party x chamber cell, how many
messages contain each term, overall, within each of the six main categories, and within each of
five partisan-score bands. Terms seen in fewer than --min-df messages are discarded, so nothing
in the output can be traced to an individual message.

The website sums these cells for whatever platform / Congress range / party / chamber a visitor
selects and computes distinctiveness (Monroe, Colaresi & Quinn 2008 "Fightin' Words" log-odds)
in the browser, so the words update live with the filters.

It also produces the monthly term-frequency data behind the "By word or phrase" chart: for a
larger vocabulary (unigrams to trigrams found in at least --search-min-df messages), the number of
messages containing each term per month, by party x chamber, written as small compressed shards
the page fetches on demand, plus bulk CSVs for download.

Outputs
  <site>/words_twitter.json, words_facebook.json, words_newsletters.json   (distinctive words)
  <site>/terms/index.json                monthly message totals per platform x party x chamber
  <site>/terms/vocab.json.gz             searchable vocabulary + normalization map (autocomplete)
  <site>/terms/<platform>/<shard>.json.gz  monthly counts per term (256 shards per platform)
  <release>/termfreq_<platform>.csv.gz   long-format bulk file: term, month, party, chamber, n_messages
  <release>/termfreq_totals.csv          monthly message totals

Usage
  python prepare_keywords.py --tweets Tweets.csv --facebook Facebook_Posts.csv \
      --newsletters Newsletters.csv --members docs/members.csv --site docs

Requires: pandas, numpy, scipy, scikit-learn  (pip install scikit-learn)
Runtime: roughly 30-45 minutes for the full dataset; each file is read twice in chunks so memory
stays modest (a few GB).
"""
import argparse
import datetime as dt
import gzip
import json
import os
import re
import sys

import numpy as np
import pandas as pd
import scipy.sparse as sp
from sklearn.feature_extraction.text import CountVectorizer, ENGLISH_STOP_WORDS

try:                                     # keep member IDs consistent with prepare_data.py
    from prepare_data import ICPSR_ALIASES
except ImportError:
    ICPSR_ALIASES = {}

CATEGORIES = ["Advertising", "CreditClaiming", "PositionTaking", "ConstituentService", "NegativePartisan", "Bipartisan"]
ALL_CATEGORIES = CATEGORIES + ["CreditConstituent", "CreditPolicy"]   # read but only the six above get slices
BANDS = [(-1.0, -0.6), (-0.6, -0.2), (-0.2, 0.2), (0.2, 0.6), (0.6, 1.0)]   # five equal bands of the partisan score
HIST_EDGES = np.linspace(-1, 1, 21)          # 20 bins of 0.1 for the histogram behind the spectrum
PARTIES = ["Democrat", "Republican"]         # "Other" is excluded from word statistics
CHAMBERS = ["House", "Senate"]
FIRST_YEAR, LAST_YEAR = 2009, 2022
MONTHS = [f"{y}-{m:02d}" for y in range(FIRST_YEAR, LAST_YEAR + 1) for m in range(1, 13)]
SEARCH_CELLS = [f"{p}|{c}" for p in PARTIES for c in CHAMBERS]        # 4 per platform
SHARDS = 256
SLICES = ["all"] + CATEGORIES + [f"band{i}" for i in range(len(BANDS))]
S = len(SLICES)

PLATFORMS = {
    "twitter":     ("Twitter", "tweets"),
    "facebook":    ("Facebook", "Facebook posts"),
    "newsletters": ("Newsletters", "newsletter sentences"),
}

EXTRA_STOP = {"rt", "amp", "http", "https", "co", "www", "com", "html", "don", "doesn", "didn", "isn", "aren",
              "wasn", "weren", "ll", "ve", "re", "just", "im", "th", "st", "nd", "rd", "us",
              "don't", "doesn't", "didn't", "isn't", "aren't", "wasn't", "weren't", "can't", "won't", "i'm", "i've",
              "i'll", "i'd", "we're", "we've", "we'll", "you're", "you've", "it's", "that's", "there's", "let's",
              "what's", "here's", "they're", "who's"}
STOP = set(ENGLISH_STOP_WORDS) | EXTRA_STOP      # single words; extended by --stopwords at run time
TERM_STOP = set()                                # multi-word entries from --stopwords, removed as terms
# Plurals that must not be folded into their singular because the meaning differs.
NO_MERGE = {"rights", "states", "news", "arms", "times", "goods", "customs", "means", "grounds", "ties",
            "series", "species", "affairs", "savings", "glasses", "spirits", "minutes", "seconds", "sanctions"}
TOKEN = r"\b[a-z][a-z']*[a-z]\b"
TOKEN_RE = re.compile(TOKEN)
URL_RE = re.compile(r"https?://\S+|www\.\S+")
MENTION_RE = re.compile(r"@\w+")


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def fnv1a(s):
    """32-bit FNV-1a; the page uses the same function to find a term's shard."""
    h = 0x811C9DC5
    for b in s.encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def clean(texts):
    out = texts.fillna("").astype(str).str.replace("&amp;", "&", regex=False)
    out = out.str.replace(URL_RE, " ", regex=True).str.replace(MENTION_RE, " ", regex=True)
    return out.str.replace("#", "", regex=False).str.lower()


def base_form(tok, known):
    """Fold a possessive into its base, and a plural into its singular when the singular is a known word."""
    t = tok
    if t.endswith("'s"):
        t = t[:-2]
    elif t.endswith("s'"):
        t = t[:-1]
    if t in NO_MERGE or t in STOP:
        return t
    for suffix, repl in (("s", ""), ("ies", "y"), ("ches", "ch"), ("shes", "sh"), ("sses", "ss"),
                         ("xes", "x"), ("zes", "z"), ("ses", "s")):
        if t.endswith(suffix):
            cand = t[:-len(suffix)] + repl
            if len(cand) >= 3 and cand in known and cand not in STOP:
                return cand
    return t


class Normalizer:
    """Maps raw tokens to canonical forms, and canonical terms to a display form: the most common raw
    term (unigram or bigram) that folds into it, preferring forms without a possessive."""
    def __init__(self, unigram_counts):
        known = set(unigram_counts)
        self.canon = {tok: base_form(tok, known) for tok in unigram_counts}
        self.merged = sum(1 for t, b in self.canon.items() if t != b)
        self.display = {}

    def learn_display(self, raw_term_counts):
        best = {}
        for raw, n in raw_term_counts.items():
            c = self.canonical_term(raw)
            score = (0 if "'" in raw else 1, n)          # non-possessive forms win, then frequency
            if c not in best or score > best[c][0]:
                best[c] = (score, raw)
        # if the only raw forms are possessives, display the canonical (possessive-free) form itself
        self.display = {c: (c if ("'" in v[1] and v[1] != c) else v[1]) for c, v in best.items()}

    def tokenize(self, text):
        out = []
        for w in TOKEN_RE.findall(text):
            if w in STOP:
                continue
            c = self.canon.get(w, w)
            if c not in STOP:
                out.append(c)
        return out

    def canonical_term(self, term):
        return " ".join(self.canon.get(w, w) for w in term.split())

    def display_term(self, term):
        return self.display.get(term, term)


def load_members(path):
    """(MemberICPSR, Congress) -> (party, chamber, switch_from, switch_to, switch_date); the last three are
    None except for members who changed party mid-session, whose messages are assigned by date."""
    m = pd.read_csv(path, usecols=["MemberICPSR", "Congress", "MemberParty", "MemberChamber", "PartySwitch", "PartySwitchDate"])
    m = m[m["MemberChamber"].isin(CHAMBERS)]
    out = {}
    for r in m.itertuples():
        frm = to = date = None
        if isinstance(r.PartySwitch, str) and " to " in r.PartySwitch:
            frm, to = r.PartySwitch.split(" to ", 1)
            date = str(r.PartySwitchDate)
        out[(int(r.MemberICPSR), int(r.Congress))] = (r.MemberParty, r.MemberChamber, frm, to, date)
    return out


def apply_aliases(ch):
    for key, target in ICPSR_ALIASES.items():
        icp, cong = key if isinstance(key, tuple) else (key, None)
        m = ch["MemberICPSR"] == icp
        if cong is not None:
            m &= ch["Congress"] == cong
        ch.loc[m, "MemberICPSR"] = target
    return ch


def party_chamber(row_key, date, members):
    rec = members.get(row_key)
    if rec is None:
        return None
    party, chamber, frm, to, sw = rec
    if frm is not None:
        party = frm if str(date) < sw else to
    return (party, chamber) if party in PARTIES else None


def chunks(path, text_col, extra_cols, chunksize):
    cols = [text_col] + extra_cols
    header = list(pd.read_csv(path, nrows=0).columns)
    missing = [c for c in cols if c not in header]
    if missing:
        raise SystemExit(f"{path}: missing columns {missing}")
    return pd.read_csv(path, usecols=cols, dtype={text_col: str}, chunksize=chunksize, low_memory=False)


def fit_vocabulary(path, text_col, args):
    """Pass 1: sample messages and fit the candidate vocabulary."""
    rng = np.random.default_rng(0)
    sample = []
    n_seen = 0
    for ch in chunks(path, text_col, [], args.chunk):
        n_seen += len(ch)
        take = ch[text_col].sample(frac=args.sample_rate, random_state=int(rng.integers(1 << 30)))
        sample.append(take)
    sample = pd.concat(sample)
    if len(sample) > args.sample:
        sample = sample.sample(n=args.sample, random_state=0)
    log(f"  vocabulary from {len(sample):,} of {n_seen:,} messages")
    texts = clean(sample)
    uni = CountVectorizer(binary=True, ngram_range=(1, 1), stop_words=sorted(STOP), token_pattern=TOKEN, min_df=2, dtype=np.int32)
    Xu = uni.fit_transform(texts)
    counts = dict(zip(uni.get_feature_names_out().tolist(), np.asarray(Xu.sum(axis=0)).ravel().tolist()))
    norm = Normalizer(counts)
    raw = CountVectorizer(binary=True, ngram_range=(1, 3), stop_words=sorted(STOP), token_pattern=TOKEN, min_df=2, dtype=np.int32)
    Xr = raw.fit_transform(texts)
    norm.learn_display(dict(zip(raw.get_feature_names_out().tolist(), np.asarray(Xr.sum(axis=0)).ravel().tolist())))
    log(f"  {norm.merged:,} plural/possessive variants folded into base forms")
    vec = CountVectorizer(binary=True, ngram_range=(1, 3), tokenizer=norm.tokenize, token_pattern=None, lowercase=False,
                          min_df=max(2, int(args.min_df * len(sample) / max(n_seen, 1))),
                          max_features=max(args.vocab * 3, args.search_vocab), dtype=np.int32)
    vec.fit(texts)
    vocab = [t for t in vec.get_feature_names_out().tolist() if t not in TERM_STOP]
    log(f"  candidate vocabulary: {len(vocab):,} terms")
    return vocab, norm


def count_platform(path, text_col, vocab, norm, members, args):
    """Pass 2: accumulate per-cell, per-slice document frequencies."""
    vec = CountVectorizer(vocabulary=vocab, binary=True, ngram_range=(1, 3), tokenizer=norm.tokenize, token_pattern=None,
                          lowercase=False, dtype=np.int32)
    V = len(vocab)
    NM = len(MONTHS)
    M = np.zeros((len(SEARCH_CELLS) * NM, V), dtype=np.int32)      # (party-chamber x month) x term
    Mdocs = np.zeros(len(SEARCH_CELLS) * NM, dtype=np.int64)
    cells = {}          # (congress, party, chamber) -> cell index
    acc = []            # list of dense (S x V) blocks, one per cell, allocated on first sight
    ndocs = []          # per cell: S counts
    hist = []           # per cell: 20 bins
    skipped_other = 0
    n_rows = 0
    extra = ["Congress", "MemberICPSR", "Date", "PartisanScore"] + CATEGORIES
    for ch in chunks(path, text_col, extra, args.chunk):
        ch = ch.dropna(subset=["Congress", "MemberICPSR"])
        ch["Congress"] = ch["Congress"].astype(int); ch["MemberICPSR"] = ch["MemberICPSR"].astype(int)
        ch = apply_aliases(ch)
        pc = [party_chamber(k, d, members) for k, d in zip(zip(ch["MemberICPSR"], ch["Congress"]), ch["Date"])]
        keep = np.array([p is not None for p in pc])
        skipped_other += int((~keep).sum())
        ch = ch[keep]
        pc = [p for p in pc if p is not None]
        if ch.empty:
            continue
        n_rows += len(ch)
        X = vec.transform(clean(ch[text_col])).tocsr()
        congress = ch["Congress"].astype(int).to_numpy()
        score = ch["PartisanScore"].to_numpy(dtype=float)
        cat = ch[CATEGORIES].fillna(0).to_numpy(dtype=np.int8)
        band = np.full(len(ch), -1)
        scored = ~np.isnan(score)
        for b, (lo, hi) in enumerate(BANDS):
            inb = scored & (score >= lo) & ((score < hi) if b < len(BANDS) - 1 else (score <= hi))
            band[inb] = b
        # cell index per row
        cell_idx = np.empty(len(ch), dtype=np.int64)
        for i, (c, (party, chamber)) in enumerate(zip(congress, pc)):
            key = (int(c), party, chamber)
            if key not in cells:
                cells[key] = len(cells)
                acc.append(np.zeros((S, V), dtype=np.int64))
                ndocs.append(np.zeros(S, dtype=np.int64))
                hist.append(np.zeros(len(HIST_EDGES) - 1, dtype=np.int64))
            cell_idx[i] = cells[key]
        # slice membership: rows -> (cell, slice) pairs
        rows, cols = [], []
        r = np.arange(len(ch))
        rows.append(r); cols.append(cell_idx * S + 0)                                  # all
        for j, c in enumerate(CATEGORIES):
            m = cat[:, j] == 1
            rows.append(r[m]); cols.append(cell_idx[m] * S + 1 + j)
        for b in range(len(BANDS)):
            m = band == b
            rows.append(r[m]); cols.append(cell_idx[m] * S + 1 + len(CATEGORIES) + b)
        rows = np.concatenate(rows); cols = np.concatenate(cols)
        G = sp.csr_matrix((np.ones(len(rows), dtype=np.int32), (rows, cols)), shape=(len(ch), len(cells) * S))
        block = (G.T @ X).toarray()                                                     # (cells*S) x V
        gsum = np.asarray(G.sum(axis=0)).ravel()
        # monthly counts by party x chamber
        dates = ch["Date"].astype(str)
        yr = pd.to_numeric(dates.str[:4], errors="coerce"); mo = pd.to_numeric(dates.str[5:7], errors="coerce")
        midx = ((yr - FIRST_YEAR) * 12 + (mo - 1)).to_numpy()
        ok = ~np.isnan(midx) & (midx >= 0) & (midx < NM)
        sc = np.array([SEARCH_CELLS.index(f"{p}|{c}") for p, c in pc])
        col = (sc[ok] * NM + midx[ok].astype(int))
        G2 = sp.csr_matrix((np.ones(int(ok.sum()), dtype=np.int32), (r[ok], col)), shape=(len(ch), len(SEARCH_CELLS) * NM))
        M += (G2.T @ X).toarray().astype(np.int32)
        Mdocs += np.asarray(G2.sum(axis=0)).ravel().astype(np.int64)
        for key, ci in cells.items():
            acc[ci] += block[ci * S:(ci + 1) * S]
            ndocs[ci] += gsum[ci * S:(ci + 1) * S]
            m = (cell_idx == ci) & scored
            if m.any():
                hist[ci] += np.histogram(score[m], bins=HIST_EDGES)[0]
        log(f"    {n_rows:,} messages counted")
    log(f"  {n_rows:,} messages in {len(cells)} cells; {skipped_other:,} skipped (party Other or no member record)")
    return cells, acc, ndocs, hist, M, Mdocs


def prune_and_write(label, unit, vocab, norm, cells, acc, ndocs, hist, out_path, args):
    """Keep the useful part of the vocabulary and write the JSON the site reads."""
    V = len(vocab)
    total_all = sum(a[0] for a in acc)                       # overall document frequency per term
    keep = total_all >= args.min_df
    order = np.argsort(-total_all)
    top = np.zeros(V, dtype=bool)
    top[order[:args.vocab]] = True
    for s in range(1, S):                                     # plus the most frequent terms of every slice
        tot = sum(a[s] for a in acc)
        top[np.argsort(-tot)[:args.per_slice_vocab]] = True
    keep &= top
    idx = np.where(keep)[0]
    remap = {old: new for new, old in enumerate(idx)}
    terms = [norm.display_term(vocab[i]) for i in idx]
    log(f"  final vocabulary: {len(terms):,} terms (min df {args.min_df})")

    out_cells = []
    for (congress, party, chamber), ci in sorted(cells.items(), key=lambda kv: kv[1]):
        a = acc[ci][:, idx]
        ntok = acc[ci].sum(axis=1)                            # term occurrences over the full candidate vocabulary
        cell = {"congress": congress, "party": party, "chamber": chamber,
                "ndocs": ndocs[ci].tolist(), "ntok": ntok.tolist(), "hist": hist[ci].tolist(),
                "all": a[0].tolist(), "s": {}}
        for s in range(1, S):
            row = a[s]
            nz = np.where(row > 0)[0]
            if len(nz) == 0:
                continue
            take = nz[np.argsort(-row[nz])[:args.per_slice]]
            take.sort()
            cell["s"][str(s)] = [take.tolist(), row[take].tolist()]
        out_cells.append(cell)

    payload = {"platform": label, "unit": unit, "generated": dt.date.today().isoformat(),
               "min_df": args.min_df, "terms": terms, "slices": SLICES, "bands": BANDS,
               "hist_edges": [round(float(e), 2) for e in HIST_EDGES], "cells": out_cells}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"), ensure_ascii=False)
    log(f"  wrote {out_path} ({os.path.getsize(out_path) / 1e6:.1f} MB)")


def write_search(key, label, vocab, norm, M, Mdocs, site, release, args):
    """Shards for the page, monthly totals, and the long-format bulk file."""
    NM = len(MONTHS)
    total = M.reshape(len(SEARCH_CELLS), NM, -1).sum(axis=(0, 1))             # messages containing each term
    order = np.argsort(-total)
    keep = [i for i in order[:args.search_vocab] if total[i] >= args.search_min_df]
    log(f"  search vocabulary: {len(keep):,} terms in at least {args.search_min_df} messages")
    shards = {}
    rows = []
    for i in keep:
        key_term = vocab[i]                                   # canonical (folded) form: the lookup key
        term = norm.display_term(vocab[i])                     # what the page shows
        entry = {"d": term}
        for c, cell in enumerate(SEARCH_CELLS):
            series = M[c * NM:(c + 1) * NM, i]
            nz = np.nonzero(series)[0]
            if len(nz):
                entry[cell] = [nz.tolist(), series[nz].tolist()]
                party, chamber = cell.split("|")
                rows.append((term, party, chamber, nz, series[nz]))
        shards.setdefault(fnv1a(key_term) % SHARDS, {})[key_term] = entry
    out_dir = os.path.join(site, "terms", key)
    os.makedirs(out_dir, exist_ok=True)
    for old in os.listdir(out_dir):                                             # stale shards from a previous run
        os.remove(os.path.join(out_dir, old))
    nbytes = 0
    for sid, data in shards.items():
        path = os.path.join(out_dir, f"{sid:03d}.json.gz")
        with gzip.open(path, "wt", encoding="utf-8", compresslevel=6) as f:
            json.dump(data, f, separators=(",", ":"), ensure_ascii=False)
        nbytes += os.path.getsize(path)
    log(f"  wrote {len(shards)} shards to {out_dir} ({nbytes / 1e6:.1f} MB)")
    # bulk file
    os.makedirs(release, exist_ok=True)
    bulk = pd.DataFrame({
        "term": np.repeat([r[0] for r in rows], [len(r[3]) for r in rows]),
        "month": [MONTHS[m] for r in rows for m in r[3]],
        "party": np.repeat([r[1] for r in rows], [len(r[3]) for r in rows]),
        "chamber": np.repeat([r[2] for r in rows], [len(r[3]) for r in rows]),
        "n_messages": np.concatenate([r[4] for r in rows]) if rows else [],
    })
    bpath = os.path.join(release, f"termfreq_{key}.csv.gz")
    bulk.to_csv(bpath, index=False, compression={"method": "gzip", "compresslevel": 6})
    log(f"  wrote {bpath}: {len(bulk):,} rows, {os.path.getsize(bpath) / 1e6:.1f} MB")
    totals = {cell: Mdocs[c * NM:(c + 1) * NM].tolist() for c, cell in enumerate(SEARCH_CELLS)}
    return [(vocab[i], norm.display_term(vocab[i])) for i in keep], totals


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tweets"); ap.add_argument("--facebook"); ap.add_argument("--newsletters")
    ap.add_argument("--members", default="docs/members.csv", help="members.csv from prepare_data.py (party and chamber)")
    ap.add_argument("--site", default="docs")
    ap.add_argument("--release", default="release/data", help="folder for the bulk termfreq_*.csv.gz files")
    ap.add_argument("--search-vocab", type=int, default=30000, help="max searchable terms per platform")
    ap.add_argument("--search-min-df", type=int, default=50, help="a term must appear in this many messages to be searchable")
    ap.add_argument("--text-col", default="TextOriginal")
    ap.add_argument("--stopwords", default="stopwords.txt",
                    help="optional text file, one term per line, of extra terms to ignore (default: stopwords.txt if present)")
    ap.add_argument("--vocab", type=int, default=4000, help="terms kept by overall frequency")
    ap.add_argument("--per-slice-vocab", type=int, default=300, help="extra terms kept per category/band by frequency")
    ap.add_argument("--per-slice", type=int, default=500, help="terms stored per cell x category/band slice")
    ap.add_argument("--min-df", type=int, default=25, help="drop terms found in fewer messages than this")
    ap.add_argument("--sample", type=int, default=300000, help="max messages used to fit the vocabulary")
    ap.add_argument("--sample-rate", type=float, default=0.1)
    ap.add_argument("--chunk", type=int, default=200000)
    args = ap.parse_args()

    paths = {"twitter": args.tweets, "facebook": args.facebook, "newsletters": args.newsletters}
    if not any(paths.values()):
        raise SystemExit("give at least one of --tweets / --facebook / --newsletters")
    if os.path.exists(args.stopwords):
        extra = {w.strip().lower() for w in open(args.stopwords, encoding="utf-8") if w.strip() and not w.startswith("#")}
        STOP.update(w for w in extra if " " not in w)
        TERM_STOP.update(w for w in extra if " " in w)
        log(f"{len(extra)} extra stop terms from {args.stopwords} ({len(TERM_STOP)} multi-word)")
    members = load_members(args.members)
    log(f"{len(members):,} member-sessions with party and chamber from {args.members}")
    os.makedirs(os.path.join(args.site, "terms"), exist_ok=True)
    index_path = os.path.join(args.site, "terms", "index.json")
    index = json.load(open(index_path, encoding="utf-8")) if os.path.exists(index_path) else {}
    index.update({"months": MONTHS, "cells": SEARCH_CELLS, "shards": SHARDS})
    index.setdefault("totals", {})
    vocab_union = {}
    canon = {}

    for key, path in paths.items():
        if not path:
            continue
        label, unit = PLATFORMS[key]
        log(f"\n{label}: {path}")
        vocab, norm = fit_vocabulary(path, args.text_col, args)
        cells, acc, ndocs, hist, M, Mdocs = count_platform(path, args.text_col, vocab, norm, members, args)
        prune_and_write(label, unit, vocab, norm, cells, acc, ndocs, hist, os.path.join(args.site, f"words_{key}.json"), args)
        terms, totals = write_search(key, label, vocab, norm, M, Mdocs, args.site, args.release, args)
        index["totals"][label] = totals
        for k, d in terms:
            vocab_union.setdefault(k, d)
        canon.update({t: c for t, c in norm.canon.items() if t != c})

    # vocabulary file for autocomplete + query normalization (union across platforms)
    allterms = sorted(vocab_union.items())                   # [canonical, display] pairs
    with gzip.open(os.path.join(args.site, "terms", "vocab.json.gz"), "wt", encoding="utf-8") as f:
        json.dump({"terms": allterms, "canon": canon, "stop": sorted(STOP), "min_df": args.search_min_df},
                  f, separators=(",", ":"), ensure_ascii=False)
    index.pop("vocab", None)
    index["generated"] = dt.date.today().isoformat()
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, separators=(",", ":"))
    tot = pd.DataFrame([(plat, MONTHS[m], *cell.split("|"), n) for plat, cells_ in index["totals"].items()
                        for cell, series in cells_.items() for m, n in enumerate(series)],
                       columns=["platform", "month", "party", "chamber", "n_messages"])
    tot.to_csv(os.path.join(args.release, "termfreq_totals.csv"), index=False)
    log(f"\n{len(allterms):,} searchable terms across platforms; index and vocabulary written to {args.site}/terms")
    log("Done.")


if __name__ == "__main__":
    main()
