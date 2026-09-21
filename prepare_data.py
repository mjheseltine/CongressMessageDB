#!/usr/bin/env python3
"""
prepare_data.py -- build the public SCCC release from the internal message-level CSVs.

Reads the three internal files (tweets, Facebook posts, newsletter sentence-bigrams)
and writes everything the website and bulk downloads need. No message text is read
or written; only the columns listed in COLS_KEEP below are carried through.

Newsletters are released at two levels: one row per newsletter (a category is 1 if any
sentence in the newsletter was labelled 1; partisan scores are the mean across sentences)
and one row per sentence bigram. The website summary uses the newsletter level.

Outputs
  <out>/data/twitter_<congress>.csv.gz / .parquet               one row per tweet
  <out>/data/facebook_<congress>.csv.gz / .parquet              one row per post
  <out>/data/newsletters_<congress>.csv.gz / .parquet           one row per newsletter
  <out>/data/newsletter_sentences_<congress>.csv.gz / .parquet  one row per sentence bigram
  <out>/members.csv                         one row per (MemberICPSR, Congress)
  <site>/summary.csv                        one row per (Platform, Congress, MemberICPSR) with counts,
                                            category proportions and mean partisanship (drives the explorer)
  <site>/members.csv                        copy of members.csv for the site
  <site>/manifest.json                      list of bulk files with rows/bytes (drives the Download table)

Usage
  python prepare_data.py --tweets Tweets.csv --facebook Facebook_Posts.csv \
      --newsletters Newsletters.csv --out ./release --site ./docs \
      [--voteview HSall_members.csv]

The optional --voteview file (https://voteview.com/data, "Member Ideology" CSV) replaces
party / chamber / state / district with values keyed on (icpsr, congress), which fixes
members whose attributes changed across sessions (e.g. House -> Senate).
"""
import argparse
import datetime as dt
import json
import os
import sys

import numpy as np
import pandas as pd

CATEGORIES = [
    "Advertising", "PositionTaking", "CreditClaiming", "NegativePartisan",
    "Bipartisan", "ConstituentService", "CreditConstituent", "CreditPolicy",
]
SCORES = ["PartisanScore", "PartisanExtremity"]
MEMBER_COLS = ["MemberName", "MemberParty", "MemberChamber", "MemberState", "MemberDistrict"]

# Engagement columns per platform, mapped to a common name for the summary table.
ENGAGEMENT = {
    "twitter":     {"Likes": "likes", "Retweets": "shares", "Replies": "replies", "Quotes": "quotes"},
    "facebook":    {"Likes": "likes", "Shares": "shares", "Comments": "replies"},
    "newsletters": {"Sentences": "sentences"},
    "newsletter_sentences": {},
}
# Message-level columns kept in the public files (in this order, if present).
COLS_KEEP = {
    "twitter":     ["TweetID", "Handle", "Date", "Congress", "MemberICPSR",
                    "Likes", "Quotes", "Retweets", "Replies"] + CATEGORIES + SCORES,
    "facebook":    ["PostURL", "Date", "Congress", "MemberICPSR",
                    "Likes", "Comments", "Shares"] + CATEGORIES + SCORES,
    "newsletters": ["NewsletterID", "Date", "Congress", "MemberICPSR", "Sentences"] + CATEGORIES + SCORES,
    "newsletter_sentences": ["NewsletterID", "BigramNumber", "Date", "Congress", "MemberICPSR"] + CATEGORIES + SCORES,
}
PLATFORM_LABEL = {"twitter": "Twitter", "facebook": "Facebook", "newsletters": "Newsletters",
                  "newsletter_sentences": "Newsletter sentences"}
SUMMARY_PLATFORMS = ["twitter", "facebook", "newsletters"]   # units: tweet, post, newsletter
VOTEVIEW_PARTY = {100: "Democrat", 200: "Republican"}


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def read_platform(path, platform):
    log(f"Reading {platform}: {path}")
    dtypes = {c: "category" for c in MEMBER_COLS + ["Handle"]}
    df = pd.read_csv(path, dtype=dtypes, low_memory=False)
    missing = [c for c in ["Congress", "MemberICPSR", "Date"] + CATEGORIES + SCORES if c not in df.columns]
    if missing:
        raise SystemExit(f"{platform}: missing required columns {missing}")
    df["Congress"] = df["Congress"].astype("int16")
    df["MemberICPSR"] = df["MemberICPSR"].astype("int32")
    for c in CATEGORIES:
        df[c] = df[c].fillna(0).astype("int8")
    if "MemberChamber" not in df.columns and "MemberDistrict" in df.columns:
        # Senators carry district "S" in the internal files.
        df["MemberChamber"] = np.where(df["MemberDistrict"].astype(str).eq("S"), "Senate", "House")
    log(f"  {len(df):,} rows, Congresses {df.Congress.min()}-{df.Congress.max()}")
    return df


def mode_or_first(s):
    s = s.dropna()
    if s.empty:
        return np.nan
    m = s.mode()
    return m.iloc[0] if len(m) else s.iloc[0]


def collapse_newsletters(bigrams):
    """One row per newsletter from the sentence-bigram rows."""
    log("Collapsing newsletter sentences to newsletters")
    g = bigrams.groupby("NewsletterID", observed=True)
    spec = {"Date": ("Date", "min"), "Congress": ("Congress", "first"), "MemberICPSR": ("MemberICPSR", "first"),
            "Sentences": ("BigramNumber", "size"),
            "PartisanScore": ("PartisanScore", "mean"), "PartisanExtremity": ("PartisanExtremity", "mean")}
    spec.update({c: (c, "max") for c in CATEGORIES})
    for c in MEMBER_COLS:
        if c in bigrams.columns:
            spec[c] = (c, "first")
    nl = g.agg(**spec).reset_index()
    for c in CATEGORIES:
        nl[c] = nl[c].astype("int8")
    log(f"  {len(nl):,} newsletters, {nl.Sentences.mean():.1f} sentences each on average")
    return nl


def build_members(frames, voteview_path=None):
    """One row per (MemberICPSR, Congress). Attributes = modal value across all messages,
    overridden by Voteview when supplied."""
    parts = []
    for df in frames.values():
        cols = ["MemberICPSR", "Congress"] + [c for c in MEMBER_COLS if c in df.columns]
        parts.append(df[cols])
    allm = pd.concat(parts, ignore_index=True)
    members = (allm.groupby(["MemberICPSR", "Congress"], observed=True)
                   .agg({c: mode_or_first for c in MEMBER_COLS if c in allm.columns})
                   .reset_index())
    members["Source"] = "messages"

    if voteview_path:
        log(f"Merging Voteview attributes from {voteview_path}")
        vv = pd.read_csv(voteview_path, low_memory=False)
        vv = vv[vv["chamber"].isin(["House", "Senate"])]
        vv = vv.rename(columns={"icpsr": "MemberICPSR", "congress": "Congress"})
        vv["MemberParty"] = vv["party_code"].map(VOTEVIEW_PARTY).fillna("Other")
        vv["MemberChamber"] = vv["chamber"]
        vv["MemberState"] = vv["state_abbrev"]
        vv["MemberDistrict"] = np.where(vv["chamber"].eq("Senate"), "S",
                                        vv["district_code"].fillna(0).astype(int).astype(str))
        vv["MemberName"] = vv["bioname"]
        vv = vv[["MemberICPSR", "Congress", "MemberName", "MemberParty", "MemberChamber",
                 "MemberState", "MemberDistrict"]].drop_duplicates(["MemberICPSR", "Congress"])
        members = members.merge(vv, on=["MemberICPSR", "Congress"], how="left", suffixes=("", "_vv"))
        for c in MEMBER_COLS:
            has = members[c + "_vv"].notna()
            members.loc[has, c] = members.loc[has, c + "_vv"]
            members.loc[has, "Source"] = "voteview"
            members.drop(columns=[c + "_vv"], inplace=True)
        n_vv = (members["Source"] == "voteview").sum()
        log(f"  {n_vv:,} of {len(members):,} member-sessions matched Voteview")

    members = members.sort_values(["Congress", "MemberICPSR"]).reset_index(drop=True)
    return members[["MemberICPSR", "Congress"] + MEMBER_COLS + ["Source"]]


def build_summary(frames, members):
    """One row per (Platform, Congress, MemberICPSR)."""
    out = []
    for platform in SUMMARY_PLATFORMS:
        df = frames[platform]
        g = df.groupby(["Congress", "MemberICPSR"], observed=True)
        s = pd.DataFrame({
            "n_messages": g.size(),
            "n_scored": g["PartisanScore"].count(),
            "mean_partisan_score": g["PartisanScore"].mean(),
            "mean_partisan_extremity": g["PartisanExtremity"].mean(),
        })
        for c in CATEGORIES:
            s["p_" + c] = g[c].mean()
        for src, dst in ENGAGEMENT[platform].items():
            if src in df.columns:
                s["mean_" + dst] = g[src].mean()
        s = s.reset_index()
        s.insert(0, "Platform", PLATFORM_LABEL[platform])
        out.append(s)
    summary = pd.concat(out, ignore_index=True)
    summary = summary.merge(members.drop(columns=["Source"]), on=["MemberICPSR", "Congress"], how="left")
    front = ["Platform", "Congress", "MemberICPSR"] + MEMBER_COLS
    summary = summary[front + [c for c in summary.columns if c not in front]]
    # Round to keep the file small; the explorer re-weights by n_messages / n_scored.
    for c in summary.columns:
        if summary[c].dtype.kind == "f":
            summary[c] = summary[c].round(4)
    return summary.sort_values(["Platform", "Congress", "MemberICPSR"]).reset_index(drop=True)


def write_bulk(frames, out_dir):
    data_dir = os.path.join(out_dir, "data")
    os.makedirs(data_dir, exist_ok=True)
    manifest = []
    for platform, df in frames.items():
        keep = [c for c in COLS_KEEP[platform] if c in df.columns]
        for congress, sub in df.groupby("Congress", observed=True):
            sub = sub[keep].sort_values("Date")
            stem = f"{platform}_{congress}"
            csv_path = os.path.join(data_dir, stem + ".csv.gz")
            pq_path = os.path.join(data_dir, stem + ".parquet")
            sub.to_csv(csv_path, index=False, compression={"method": "gzip", "compresslevel": 6})
            sub.to_parquet(pq_path, index=False, compression="zstd")
            manifest.append({
                "platform": PLATFORM_LABEL[platform],
                "congress": int(congress),
                "rows": int(len(sub)),
                "csv_gz": {"file": os.path.basename(csv_path), "bytes": os.path.getsize(csv_path)},
                "parquet": {"file": os.path.basename(pq_path), "bytes": os.path.getsize(pq_path)},
            })
            log(f"  wrote {stem}: {len(sub):,} rows, "
                f"{os.path.getsize(csv_path)/1e6:.1f} MB csv.gz, {os.path.getsize(pq_path)/1e6:.1f} MB parquet")
    return manifest


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tweets", required=True)
    ap.add_argument("--facebook", required=True)
    ap.add_argument("--newsletters", required=True)
    ap.add_argument("--out", default="release", help="folder for bulk files (upload to R2 / Releases / Dataverse)")
    ap.add_argument("--site", default="docs", help="GitHub Pages folder (receives summary.csv, members.csv, manifest.json)")
    ap.add_argument("--voteview", default=None, help="Voteview HSall_members.csv for session-specific member attributes")
    ap.add_argument("--version", default=dt.date.today().isoformat(), help="release label written into manifest.json")
    args = ap.parse_args()

    bigrams = read_platform(args.newsletters, "newsletter_sentences")
    frames = {
        "twitter": read_platform(args.tweets, "twitter"),
        "facebook": read_platform(args.facebook, "facebook"),
        "newsletters": collapse_newsletters(bigrams),
        "newsletter_sentences": bigrams,
    }

    log("Building members table")
    members = build_members(frames, args.voteview)
    log("Building summary table")
    summary = build_summary(frames, members)
    log("Writing bulk files")
    manifest = write_bulk(frames, args.out)

    os.makedirs(args.site, exist_ok=True)
    members.to_csv(os.path.join(args.out, "members.csv"), index=False)
    members.to_csv(os.path.join(args.site, "members.csv"), index=False)
    summary.to_csv(os.path.join(args.site, "summary.csv"), index=False)
    with open(os.path.join(args.site, "manifest.json"), "w") as f:
        json.dump({
            "version": args.version,
            "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"),
            "totals": {PLATFORM_LABEL[p]: int(len(frames[p])) for p in frames},
            "members": int(members["MemberICPSR"].nunique()),
            "files": manifest,
        }, f, indent=1)

    log(f"\nDone. summary.csv: {len(summary):,} rows; members.csv: {len(members):,} rows; "
        f"{len(manifest)} bulk files in {args.out}/data")


if __name__ == "__main__":
    main()
