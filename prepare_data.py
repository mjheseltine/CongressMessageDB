#!/usr/bin/env python3
"""
prepare_data.py -- build the public SCCC release from the internal message-level CSVs.

Reads the three internal files (tweets, Facebook posts, newsletter sentence-bigrams)
and writes everything the website and bulk downloads need. No message text is read
or written; only the columns listed in COLS_KEEP below are carried through.

Newsletters are released at two levels: one row per newsletter (a category is 1 if any
sentence in the newsletter was labelled 1; partisan scores are the mean across sentences)
and one row per sentence bigram. The summary and members tables use the sentence level for
newsletters, as in the article, so category shares are comparable across platforms.

Outputs
  <out>/data/twitter_<congress>.csv.gz / .parquet               one row per tweet
  <out>/data/facebook_<congress>.csv.gz / .parquet              one row per post
  <out>/data/newsletters_<congress>.csv.gz / .parquet           one row per newsletter
  <out>/data/newsletter_sentences_<congress>.csv.gz / .parquet  one row per sentence bigram
  <out>/members.csv                         one row per (MemberICPSR, Congress): attributes from the master
                                            member-session file, party-switch fields, and message counts,
                                            category proportions and mean partisanship, pooled across
                                            platforms and with Twt / FB / NL suffixes per platform
  <site>/summary.csv                        one row per (Platform, Congress, MemberICPSR) with counts,
                                            category proportions and mean partisanship (drives the explorer)
  <site>/members.csv                        copy of members.csv for the site
  <site>/manifest.json                      list of bulk files with rows/bytes (drives the Download table)

Usage
  python prepare_data.py --tweets Tweets.csv --facebook Facebook_Posts.csv \
      --newsletters Newsletters.csv --master "Member-Session Data (v5.4).csv" \
      --out ./release --site ./docs

--master is the project's member-session file (one row per member and Congress, with Name,
MemberICPSR, Session, Party, PartySwitch, PartySwitchDate, Chamber, State, District). It is the
ground truth for member attributes: the values carried in the internal message files are used
only for member-sessions the master does not contain, and those are reported in the log.

Party for the six members who changed party mid-session is handled two ways. members.csv carries
the master's Party (the longest affiliation of the session) plus PartySwitch and PartySwitchDate.
summary.csv, which feeds the explorer, assigns party to each message by date, so a switcher has
two rows in that session (one per party).
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
    "newsletters": {},
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
# Every column the script is allowed to read. Anything else in the internal files (message text,
# URLs to media, etc.) is never loaded into memory.
COLS_READ = set(c for cols in COLS_KEEP.values() for c in cols) | set(MEMBER_COLS)
# (engagement key, frame key, label) for the summary table; newsletters use the sentence-level frame.
SUMMARY_SOURCES = [("twitter", "twitter", "Twitter"), ("facebook", "facebook", "Facebook"),
                   ("newsletters", "newsletter_sentences", "Newsletters")]
# members.csv aggregate blocks: column suffix -> frame key ("" = pooled across the three)
SUFFIX = {"Twt": "twitter", "FB": "facebook", "NL": "newsletter_sentences"}
CAT_SHORT = {"NegativePartisan": "NegPartisan"}
# Columns that identify a unique message; repeated values are collection duplicates and are dropped.
ID_COLS = {"twitter": ["TweetID"], "facebook": ["PostURL"], "newsletter_sentences": ["NewsletterID", "BigramNumber"]}
# Voteview issues a second ICPSR ID after a party switch (9 + the original) or when a member returns
# after a gap, and the internal message files carry a mix of both. Alternate IDs are folded into the
# ID the master file uses. A key can be an ICPSR (applies in every Congress) or (ICPSR, Congress).
ICPSR_ALIASES = {
    91980: 21980,          # Van Drew: post-switch ID -> master ID
    14910: 94910,          # Specter: Republican-era ID -> master's Democratic-era ID (111th)
    14828: 94828,          # Ralph Hall: original ID -> master's post-switch ID
    (21518, 112): 21127,   # Dold: master uses 21127 in the 112th ...
    (21127, 114): 21518,   #        ... and 21518 in the 114th
    (21152, 114): 21535,   # Guinta: master uses 21535 in the 114th
}

MASTER_COLS = {"Name": "MemberName", "MemberICPSR": "MemberICPSR", "Session": "Congress", "Party": "MemberParty",
               "PartySwitch": "PartySwitch", "PartySwitchDate": "PartySwitchDate", "Chamber": "MemberChamber",
               "State": "MemberState", "District": "MemberDistrict"}
MASTER_OPTIONAL = {"TermStart": "TermStart", "TermEnd": "TermEnd"}   # YYYY-MM-DD; messages outside are dropped


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def read_platform(path, platform):
    log(f"Reading {platform}: {path}")
    header = list(pd.read_csv(path, nrows=0).columns)
    wanted = [c for c in header if c in COLS_READ]
    ignored = [c for c in header if c not in COLS_READ]
    log(f"  reading {len(wanted)} of {len(header)} columns; ignoring: {ignored if ignored else 'none'}")
    dtypes = {c: "category" for c in MEMBER_COLS + ["Handle"] if c in wanted}
    df = pd.read_csv(path, usecols=wanted, dtype=dtypes, low_memory=False)
    missing = [c for c in ["Congress", "MemberICPSR", "Date"] + CATEGORIES + SCORES if c not in df.columns]
    if missing:
        raise SystemExit(f"{platform}: missing required columns {missing}")
    key_missing = df["Congress"].isna() | df["MemberICPSR"].isna()
    if key_missing.any():
        n = int(key_missing.sum())
        log(f"  dropping {n:,} rows ({n/len(df):.2%}) with no Congress or MemberICPSR "
            f"(no Congress: {int(df['Congress'].isna().sum()):,}; no ICPSR: {int(df['MemberICPSR'].isna().sum()):,})")
        if n / len(df) > 0.05:
            log("  WARNING: more than 5% of rows dropped -- check the member merge before releasing")
        df = df[~key_missing].copy()
    ids = ID_COLS.get(platform)
    if ids and all(c in df.columns for c in ids):
        dup = df.duplicated(subset=ids)
        if dup.any():
            log(f"  dropping {int(dup.sum()):,} duplicate rows ({dup.mean():.2%}) with a repeated {' + '.join(ids)}")
            df = df[~dup].copy()
    df["Congress"] = df["Congress"].astype("int16")
    df["MemberICPSR"] = df["MemberICPSR"].astype("int32")
    n_alias = 0
    for key, target in ICPSR_ALIASES.items():
        icp, cong = key if isinstance(key, tuple) else (key, None)
        m = df["MemberICPSR"] == icp
        if cong is not None:
            m &= df["Congress"] == cong
        if m.any():
            n_alias += int(m.sum())
            df.loc[m, "MemberICPSR"] = target
    if n_alias:
        log(f"  remapped {n_alias:,} rows from alternate ICPSR IDs")
    for c in CATEGORIES:
        df[c] = df[c].fillna(0).astype("int8")
    if "MemberChamber" not in df.columns and "MemberDistrict" in df.columns:
        # Senators carry district "S" in the internal files.
        df["MemberChamber"] = np.where(df["MemberDistrict"].astype(str).eq("S"), "Senate", "House")
    log(f"  {len(df):,} rows, Congresses {df.Congress.min()}-{df.Congress.max()}")
    return df


# Surnames with a capital letter mid-word that plain title-casing would lose (all-caps source names only).
MIDCAP_SURNAMES = {
    "defazio": "DeFazio", "degette": "DeGette", "delauro": "DeLauro", "delbene": "DelBene", "demint": "DeMint",
    "desantis": "DeSantis", "desaulnier": "DeSaulnier", "desjarlais": "DesJarlais", "lahood": "LaHood",
    "lamalfa": "LaMalfa", "latourette": "LaTourette", "laturner": "LaTurner", "lobiondo": "LoBiondo",
    "macarthur": "MacArthur", "lalota": "LaLota", "deremer": "DeRemer",
}


def normalize_name(name):
    """'CASSIDY, Bill' -> 'Cassidy, Bill'; 'MCCARTHY, Kevin' -> 'McCarthy, Kevin'; 'O'ROURKE, Beto' -> 'O'Rourke, Beto'.
    Names whose surname is not all upper-case are returned unchanged."""
    if not isinstance(name, str) or "," not in name:
        return name
    last, rest = name.split(",", 1)
    if not last.strip().isupper():
        return name
    words = []
    for w in last.strip().split():
        key = w.lower()
        if key in MIDCAP_SURNAMES:
            words.append(MIDCAP_SURNAMES[key])
            continue
        t = w.title()                                   # handles hyphens, apostrophes and accents
        if t.startswith("Mc") and len(t) > 2:
            t = "Mc" + t[2].upper() + t[3:]
        words.append(t)
    return " ".join(words) + "," + rest


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


def load_master(path):
    """The member-session master file -> one row per (MemberICPSR, Congress) with our column names."""
    header = list(pd.read_csv(path, nrows=0).columns)
    cols = {**MASTER_COLS, **{k: v for k, v in MASTER_OPTIONAL.items() if k in header}}
    m = pd.read_csv(path, usecols=list(cols), na_values=["NA"], low_memory=False).rename(columns=cols)
    for c in MASTER_OPTIONAL.values():
        if c not in m.columns:
            m[c] = np.nan
    if m["MemberICPSR"].isna().any():
        log(f"  WARNING: {int(m['MemberICPSR'].isna().sum())} master rows have no MemberICPSR and cannot be matched")
        m = m.dropna(subset=["MemberICPSR"])
    m["MemberICPSR"] = m["MemberICPSR"].astype("int32")
    m["Congress"] = m["Congress"].astype("int16")
    dup = m.duplicated(["MemberICPSR", "Congress"])
    if dup.any():
        raise SystemExit(f"master file has {int(dup.sum())} duplicate (MemberICPSR, Session) rows")
    m["MemberDistrict"] = m["MemberDistrict"].astype(str)
    log(f"  {len(m):,} member-sessions in the master file; {int(m['PartySwitch'].notna().sum())} with a mid-session party switch")
    return m


def build_members(frames, master_path=None):
    """One row per (MemberICPSR, Congress). Attributes come from the master file; for member-sessions
    it lacks, the modal value across that member's messages is used and flagged in Source."""
    parts = []
    for df in frames.values():
        cols = ["MemberICPSR", "Congress"] + [c for c in MEMBER_COLS if c in df.columns]
        parts.append(df[cols])
    allm = pd.concat(parts, ignore_index=True)
    members = (allm.groupby(["MemberICPSR", "Congress"], observed=True)
                   .agg({c: mode_or_first for c in MEMBER_COLS if c in allm.columns})
                   .reset_index())
    for c in MEMBER_COLS:
        members[c] = members[c].astype(object) if c in members.columns else np.nan
    members["PartySwitch"] = pd.Series([np.nan] * len(members), dtype=object)
    members["PartySwitchDate"] = pd.Series([np.nan] * len(members), dtype=object)
    members["Source"] = "messages"
    counts = {k: df.groupby(["MemberICPSR", "Congress"], observed=True).size() for k, df in frames.items()
              if k != "newsletter_sentences"}

    if master_path:
        log(f"Merging member attributes from {master_path}")
        m = load_master(master_path)
        members = members.merge(m, on=["MemberICPSR", "Congress"], how="left", suffixes=("", "_m"))
        has = members["MemberName_m"].notna() | members["MemberParty_m"].notna()
        for c in MEMBER_COLS + ["PartySwitch", "PartySwitchDate"]:
            members[c] = np.where(has, members[c + "_m"].astype(object), members[c].astype(object))
            members.drop(columns=[c + "_m"], inplace=True)
        for c in MASTER_OPTIONAL.values():
            members[c] = members[c].astype(object)
        members.loc[has, "Source"] = "master"
        unmatched = members[~has]
        log(f"  {int(has.sum()):,} of {len(members):,} member-sessions matched the master file")
        if len(unmatched):
            log(f"  {len(unmatched):,} member-sessions with messages are NOT in the master file (attributes taken from the messages):")
            for r in unmatched.sort_values(["Congress", "MemberICPSR"]).itertuples():
                n = ", ".join(f"{k} {int(v.get((r.MemberICPSR, r.Congress), 0)):,}" for k, v in counts.items())
                log(f"    ICPSR {r.MemberICPSR}  Congress {r.Congress}  {r.MemberName}  ({r.MemberParty}, {r.MemberState})  [{n}]")
        no_msgs = m.merge(members[["MemberICPSR", "Congress"]], on=["MemberICPSR", "Congress"], how="left", indicator=True)
        no_msgs = no_msgs[no_msgs["_merge"] == "left_only"]
        log(f"  {len(no_msgs):,} master member-sessions have no messages on any platform (not written to members.csv)")

    members = members[["MemberICPSR", "Congress"] + MEMBER_COLS + ["PartySwitch", "PartySwitchDate", "Source"] + list(MASTER_OPTIONAL.values())]
    before = members["MemberName"].astype(str)
    members["MemberName"] = before.map(normalize_name)
    log(f"  normalized capitalization of {(before != members['MemberName'].astype(str)).sum():,} member-session names")
    log("Computing member-session aggregates")
    members = members.merge(member_aggregates(frames), on=["MemberICPSR", "Congress"], how="left")
    return members.sort_values(["Congress", "MemberICPSR"]).reset_index(drop=True)


def member_aggregates(frames):
    """Per member-session: message counts, category counts/proportions and mean partisanship,
    pooled across platforms (no suffix) and per platform (Twt / FB / NL). Newsletters count sentences."""
    def block(df, suffix):
        g = df.groupby(["MemberICPSR", "Congress"], observed=True)
        n = g.size()
        out = pd.DataFrame({"NumMessages": n,
                            "PartisanScore": g["PartisanScore"].mean(),
                            "PartisanExtremity": g["PartisanExtremity"].mean()})
        if "NewsletterID" in df.columns:
            out["NumNewsletters"] = g["NewsletterID"].nunique()
        for c in CATEGORIES:
            short = CAT_SHORT.get(c, c)
            out["Num" + short] = g[c].sum().astype("int64")
            out["Prop" + short] = out["Num" + short] / n
        out.columns = [c + suffix for c in out.columns]
        return out
    base = ["MemberICPSR", "Congress", "PartisanScore", "PartisanExtremity"] + CATEGORIES
    pooled = pd.concat([frames[k][base] for k in SUFFIX.values()], ignore_index=True)
    blocks = [block(pooled, "")] + [block(frames[k], suf) for suf, k in SUFFIX.items()]
    agg = pd.concat(blocks, axis=1).reset_index()
    for c in agg.columns:
        if c.startswith("Num"):
            agg[c] = agg[c].astype("Int64")        # whole numbers, blank where a member has no messages on a platform
        elif agg[c].dtype.kind == "f":
            agg[c] = agg[c].round(4)
    return agg


def message_party(df, members):
    """Party of each message: the member-session's party, except for mid-session switchers, whose
    messages before PartySwitchDate get the party they switched from and later ones the party they
    switched to. PartySwitch is written as '<from> to <to>'."""
    m = members[["MemberICPSR", "Congress", "MemberParty", "PartySwitch", "PartySwitchDate"]]
    x = df[["MemberICPSR", "Congress", "Date"]].merge(m, on=["MemberICPSR", "Congress"], how="left")
    party = x["MemberParty"].astype("object")
    sw = x["PartySwitch"].notna()
    if sw.any():
        frm = x.loc[sw, "PartySwitch"].str.split(" to ").str[0]
        to = x.loc[sw, "PartySwitch"].str.split(" to ").str[1]
        before = x.loc[sw, "Date"].astype(str) < x.loc[sw, "PartySwitchDate"].astype(str)
        party.loc[sw] = np.where(before, frm, to)
    return party.to_numpy()


def build_summary(frames, members):
    """One row per (Platform, Congress, MemberICPSR)."""
    out = []
    for eng_key, frame_key, label in SUMMARY_SOURCES:
        df = frames[frame_key]
        df = df.assign(MemberParty=message_party(df, members))
        g = df.groupby(["Congress", "MemberICPSR", "MemberParty"], observed=True, dropna=False)
        s = pd.DataFrame({
            "n_messages": g.size(),
            "n_scored": g["PartisanScore"].count(),
            "mean_partisan_score": g["PartisanScore"].mean(),
            "mean_partisan_extremity": g["PartisanExtremity"].mean(),
        })
        if "NewsletterID" in df.columns:
            s["n_newsletters"] = g["NewsletterID"].nunique()
        for c in CATEGORIES:
            s["p_" + c] = g[c].mean()
        for src, dst in ENGAGEMENT[eng_key].items():
            if src in df.columns:
                s["mean_" + dst] = g[src].mean()
        s = s.reset_index()
        s.insert(0, "Platform", label)
        out.append(s)
    summary = pd.concat(out, ignore_index=True)
    attrs = [c for c in MEMBER_COLS if c != "MemberParty"]
    summary = summary.merge(members[["MemberICPSR", "Congress"] + attrs], on=["MemberICPSR", "Congress"], how="left")
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
    ap.add_argument("--master", default=None, help='member-session master file, e.g. "Member-Session Data (v5.4).csv"')
    ap.add_argument("--drop-unmatched", action="store_true",
                    help="with --master: drop messages from member-sessions the master file does not contain")
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
    members = build_members(frames, args.master)
    if args.master and args.drop_unmatched:
        keep = members.loc[members["Source"] == "master", ["MemberICPSR", "Congress"]]
        for key, df in frames.items():
            n0 = len(df)
            frames[key] = df.merge(keep, on=["MemberICPSR", "Congress"], how="inner")
            log(f"  {key}: dropped {n0 - len(frames[key]):,} messages from member-sessions not in the master file")
        members = members[members["Source"] == "master"].reset_index(drop=True)
    if args.master and (members["TermStart"].notna().any() or members["TermEnd"].notna().any()):
        span = members.loc[members["TermStart"].notna() | members["TermEnd"].notna(),
                           ["MemberICPSR", "Congress", "TermStart", "TermEnd"]]
        for key, df in frames.items():
            x = df[["MemberICPSR", "Congress", "Date"]].merge(span, on=["MemberICPSR", "Congress"], how="left")
            d = x["Date"].astype(str)
            out = (x["TermStart"].notna() & (d < x["TermStart"].astype(str))) | (x["TermEnd"].notna() & (d > x["TermEnd"].astype(str)))
            if out.any():
                log(f"  {key}: dropped {int(out.sum()):,} messages dated outside a member's TermStart/TermEnd")
                frames[key] = df[~out.to_numpy()].copy()
        # member aggregates were computed before the date filter; recompute
        members = members.drop(columns=[c for c in members.columns if c.startswith(("Num", "Prop", "PartisanScore", "PartisanExtremity"))])
        members = members.merge(member_aggregates(frames), on=["MemberICPSR", "Congress"], how="left")
        gone = members["NumMessages"].isna()
        if gone.any():
            log(f"  {int(gone.sum())} member-sessions have no messages left after the term-date filter and are not written")
            members = members[~gone].reset_index(drop=True)
    members = members.drop(columns=list(MASTER_OPTIONAL.values()))
    log("Building summary table")
    summary = build_summary(frames, members)
    log("Writing bulk files")
    manifest = write_bulk(frames, args.out)

    os.makedirs(args.site, exist_ok=True)
    members.to_csv(os.path.join(args.out, "members.csv"), index=False)
    members.to_csv(os.path.join(args.site, "members.csv"), index=False)
    summary.to_csv(os.path.join(args.site, "summary.csv"), index=False)
    with open(os.path.join(args.site, "manifest.json"), "w", encoding="utf-8") as f:
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
