# SCCC codebook

Scaled and Classified Congressional Communication (SCCC), public release.
Source article: Kistner, Heseltine, Alvarez, Fitch, Lothamer and Simas (2026), *American Political Science Review*, doi:10.1017/S0003055426101841.

The public files contain **no message text**. Each row carries a platform identifier so that researchers with their own API access can retrieve the underlying message.

## Files

| File | Unit of observation | Notes |
|---|---|---|
| `twitter_<congress>.csv.gz` / `.parquet` | one tweet | 111th–117th Congress |
| `facebook_<congress>.csv.gz` / `.parquet` | one Facebook post | 111th–117th Congress |
| `newsletters_<congress>.csv.gz` / `.parquet` | one email newsletter | 111th–117th Congress; categories and scores rolled up from the sentence level (see below) |
| `newsletter_sentences_<congress>.csv.gz` / `.parquet` | one sentence bigram from an email newsletter | the unit the classifiers and scaling model operated on; several rows per newsletter |
| `members.csv` | one member × Congress | join key for the message files |
| `summary.csv` | one member × Congress × platform | aggregates behind the website explorer |

Congress numbers map to years as follows: 111 = 2009–10, 112 = 2011–12, 113 = 2013–14, 114 = 2015–16, 115 = 2017–18, 116 = 2019–20, 117 = 2021–22. Data for the 111th Congress do not cover the full session.

## Message-level variables

| Variable | Type | Platforms | Description |
|---|---|---|---|
| `TweetID` | integer | Twitter | Tweet status ID. |
| `Handle` | string | Twitter | Account handle at time of collection (official, campaign or personal account of the member). |
| `PostURL` | string | Facebook | URL of the post on facebook.com. |
| `NewsletterID` | integer | Newsletters | Newsletter identifier (DCinbox). |
| `Sentences` | integer | Newsletters (newsletter level) | Number of sentence-bigram units in the newsletter. |
| `BigramNumber` | integer | Newsletters (sentence level) | Position of the sentence bigram within the newsletter, starting at 1. |
| `Date` | YYYY-MM-DD | all | Date the message was posted/sent. |
| `Congress` | integer | all | Congressional session (111–117). |
| `MemberICPSR` | integer | all | ICPSR legislator ID (as used by Voteview). Join to `members.csv`. |
| `Likes`, `Retweets`, `Replies`, `Quotes` | integer | Twitter | Engagement counts at time of collection. |
| `Likes`, `Shares`, `Comments` | integer | Facebook | Engagement counts at time of collection (CrowdTangle). Early-period posts may show zeros where counts were unavailable. |
| `Advertising` | 0/1 | all | Effort to disseminate the member's name in a favourable way (Mayhew 1974). |
| `CreditClaiming` | 0/1 | all | Claims personal responsibility for a government action or outcome (Mayhew 1974). |
| `PositionTaking` | 0/1 | all | Judgmental statement on a matter of interest to political actors (Mayhew 1974). |
| `ConstituentService` | 0/1 | all | Presentation of self in the district or allocation of resources to constituents (Fenno 1978). |
| `NegativePartisan` | 0/1 | all | Attack on the policies or politicians of the opposing party (Russell 2018). |
| `Bipartisan` | 0/1 | all | Advocates the value of bipartisan collaboration (Russell 2018). |
| `CreditConstituent` | 0/1 | all | Subtype of credit claiming. **[Authors: add definition.]** |
| `CreditPolicy` | 0/1 | all | Subtype of credit claiming. **[Authors: add definition.]** |
| `PartisanScore` | float, −1 to 1 | all | Text Partisanship Score: 2πᵣ − 1 from a class affinity model (Perry and Benoit 2017) fit separately by Congress. −1 = most Democratic language, 0 = nonpartisan, +1 = most Republican. Missing where the model was not fit (e.g. messages dated outside the session's scaling window). |
| `PartisanExtremity` | float, 0 to 1 | all | Absolute value of `PartisanScore`. |

Categories are neither mutually exclusive nor exhaustive; roughly 28% of tweets, posts and newsletter sentences fall in none of the six main categories.

**Newsletter roll-up.** Classification and scaling were performed on sentence bigrams. In the newsletter-level files, a category is 1 if *any* sentence in the newsletter was labelled 1, and `PartisanScore` / `PartisanExtremity` are the means across the newsletter's sentences. Because newsletters are long, newsletter-level category shares are much higher than tweet or post shares and are not directly comparable to them; use the sentence-level file for share-of-sentences measures as reported in the article.

Classifier labels come from BERTweet models trained on approximately 43,000 hand-coded messages. Out-of-sample macro F1 by category: 0.83–0.95 (Twitter), 0.81–0.92 (Facebook), 0.77–0.93 (newsletters). See Section G–H of the article's supplementary material.

## `members.csv`

| Variable | Description |
|---|---|
| `MemberICPSR` | ICPSR legislator ID. |
| `Congress` | Congressional session. |
| `MemberName` | Name as recorded in the source data. |
| `MemberParty` | Democrat, Republican or Other. |
| `MemberChamber` | House or Senate. |
| `MemberState` | Two-letter state abbreviation. |
| `MemberDistrict` | House district number, or `S` for senators. |
| `Source` | `voteview` if attributes were taken from Voteview for that member-session; `messages` if inferred from the internal files. |

## `summary.csv`

One row per `Platform` × `Congress` × `MemberICPSR`, with the member attributes above plus:

| Variable | Description |
|---|---|
| `n_messages` | Number of tweets, posts or newsletters. |
| `n_scored` | Number of rows with a non-missing `PartisanScore`. |
| `mean_partisan_score`, `mean_partisan_extremity` | Means over scored rows. |
| `p_<Category>` | Share of rows labelled 1 for that category. |
| `mean_likes`, `mean_shares`, `mean_replies`, `mean_quotes` | Mean engagement per message (social media only; `Retweets`→`shares`, `Comments`→`replies`). |
| `mean_sentences` | Mean number of sentence-bigram units per newsletter (newsletters only). |

To aggregate across rows, weight proportions by `n_messages` and scores by `n_scored`.

## Known limitations

- Twitter and Facebook content from accounts deleted before collection is absent; the 2009–2017 counts are lower bounds.
- Facebook engagement counts for the earliest years are frequently zero because CrowdTangle did not return them.
- Some member-sessions lack a name or party in the source data; these show as blank or `Other`. Where Voteview attributes are available (`Source = voteview`) they should be preferred.
- Newsletter-level category flags use an any-sentence rule, so they rise with newsletter length; sentence-level shares are in `newsletter_sentences_*`.

## Licence

Data: CC BY 4.0. Please cite both the article and the Dataverse record (doi:10.7910/DVN/19MIBB).
