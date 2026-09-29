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

Outputs
  <site>/words_twitter.json, <site>/words_facebook.json, <site>/words_newsletters.json

Usage
  python prepare_keywords.py --tweets Tweets.csv --facebook Facebook_Posts.csv \
      --newsletters Newsletters.csv --members docs/members.csv --site docs

Requires: pandas, numpy, scipy, scikit-learn  (pip install scikit-learn)
Runtime: roughly 30-45 minutes for the full dataset; each file is read twice in chunks so memory
stays modest (a few GB).
"""
import argparse
import datetime as dt
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
SLICES = ["all"] + CATEGORIES + [f"band{i}" for i in range(len(BANDS))]
S = len(SLICES)

PLATFORMS = {
    "twitter":     ("Twitter", "tweets"),
    "facebook":    ("Facebook", "Facebook posts"),
    "newsletters": ("Newsletters", "newsletter sentences"),
}

EXTRA_STOP = {"rt", "amp", "http", "https", "co", "www", "com", "html", "don", "doesn", "didn", "isn", "aren",
              "wasn", "weren", "ll", "ve", "re", "just", "im", "th", "st", "nd", "rd", "us"}
STOP = set(ENGLISH_STOP_WORDS) | EXTRA_STOP      # extended by --stopwords at run time
TOKEN = r"\b[a-z][a-z']*[a-z]\b"
URL_RE = re.compile(r"https?://\S+|www\.\S+")
MENTION_RE = re.compile(r"@\w+")


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def clean(texts):
    out = texts.fillna("").astype(str).str.replace("&amp;", "&", regex=False)
    out = out.str.replace(URL_RE, " ", regex=True).str.replace(MENTION_RE, " ", regex=True)
    return out.str.replace("#", "", regex=False).str.lower()


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
    vec = CountVectorizer(binary=True, ngram_range=(1, 2), stop_words=sorted(STOP), token_pattern=TOKEN,
                          min_df=max(2, int(args.min_df * len(sample) / max(n_seen, 1))),
                          max_features=args.vocab * 3, dtype=np.int32)
    vec.fit(clean(sample))
    vocab = vec.get_feature_names_out().tolist()
    log(f"  candidate vocabulary: {len(vocab):,} terms")
    return vocab


def count_platform(path, text_col, vocab, members, args):
    """Pass 2: accumulate per-cell, per-slice document frequencies."""
    vec = CountVectorizer(vocabulary=vocab, binary=True, ngram_range=(1, 2), stop_words=sorted(STOP), token_pattern=TOKEN,
                          dtype=np.int32)
    V = len(vocab)
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
        for key, ci in cells.items():
            acc[ci] += block[ci * S:(ci + 1) * S]
            ndocs[ci] += gsum[ci * S:(ci + 1) * S]
            m = (cell_idx == ci) & scored
            if m.any():
                hist[ci] += np.histogram(score[m], bins=HIST_EDGES)[0]
        log(f"    {n_rows:,} messages counted")
    log(f"  {n_rows:,} messages in {len(cells)} cells; {skipped_other:,} skipped (party Other or no member record)")
    return cells, acc, ndocs, hist


def prune_and_write(label, unit, vocab, cells, acc, ndocs, hist, out_path, args):
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
    terms = [vocab[i] for i in idx]
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tweets"); ap.add_argument("--facebook"); ap.add_argument("--newsletters")
    ap.add_argument("--members", default="docs/members.csv", help="members.csv from prepare_data.py (party and chamber)")
    ap.add_argument("--site", default="docs")
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
        STOP.update(extra)
        log(f"{len(extra)} extra stop terms from {args.stopwords}")
    members = load_members(args.members)
    log(f"{len(members):,} member-sessions with party and chamber from {args.members}")
    os.makedirs(args.site, exist_ok=True)

    for key, path in paths.items():
        if not path:
            continue
        label, unit = PLATFORMS[key]
        log(f"\n{label}: {path}")
        vocab = fit_vocabulary(path, args.text_col, args)
        cells, acc, ndocs, hist = count_platform(path, args.text_col, vocab, members, args)
        prune_and_write(label, unit, vocab, cells, acc, ndocs, hist, os.path.join(args.site, f"words_{key}.json"), args)
    log("\nDone.")


if __name__ == "__main__":
    main()
