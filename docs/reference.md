# Offline reference: Wikipedia and friends with no internet

Scout can keep reference works on the Mac (Wikipedia, WikiMed, iFixit, Wikivoyage) and
answer "look up X" from them when Starlink is down. They are Kiwix ZIM archives, the
same files the Kiwix apps and Project NOMAD use. The code is `src/scout/reference.py`.

## How it works

**Read in place.** Each archive carries its own full-text index (Xapian) and title
index. Scout searches those indexes and reads only the article sections a question
needs. Nothing is copied into Scout's store: Wikipedia alone is 49 GB without
pictures.

**Cut at headings.** An article is split at its h2 to h4 headings, as NOMAD's
`zim_extraction_service` does. Boilerplate sections (See also, References, External
links, Further reading, Notes, Bibliography) and their subsections are left out, and
so are reference marks, infoboxes, navigation boxes, figures and scripts. Table cells
are kept apart with `|`.

**Search.** Scout searches with the question's topic words ("how do I treat a bee
sting" is `treat bee sting`). The index needs every word in an article. When no
article has them all, Scout drops the words no article has, then one word at a time,
but keeps all of two words and at least half of more. Title matches come first when
the title is exactly the words asked. With several archives, results take turns.

**Tools.** The room, the phone and other Claude sessions (through the MCP server) get
two tools. Neither asks for a yes: they only read.

- `reference_search {query, book?, limit?}`: the best articles, each with its title,
  archive, first lines and ref.
- `reference_read {ref, section?, query?, max_chars?}`: an article's title, link,
  list of sections and introduction. `section` reads one section with its
  subsections; `query` picks the two sections that best match a few words.

Claude's prompt says to use them for "look up X" and general-knowledge questions when
the web is slow or down.

**Answers on this Mac.** With the internet down, Claude can't be reached. Then a
question ("what", "how", "why"...) or a "look up ..." that tiers 0 and 1 didn't
answer goes to the reference. The best passages (up to 600 characters each, from the
top three articles) go to the local model, which answers from one of them with an
exact quote, or says it isn't there. The answer is checked in plain code, as recall
answers are (`recall.py`): the quote must be in the passage, the passage must cover
the question's topic words, every number must be in the passage, and nearly all the
answer's words must be too. Then Scout says it: "From Wikipedia: Remove the stinger
by scraping it out sideways...". Anything that fails goes on to Claude, as before.
This needs the local model (`claude.local_model`), and runs in the room; the phone
needs the internet anyway.

**Online or not.** Scout opens a TCP connection to 1.1.1.1:443 and 8.8.8.8:53 (no
DNS) with a one-second timeout. Either answering means online. The result is kept
for 30 seconds. It is checked only for a question no local tier answered.

**Links.** An article's ref is `zim:<book>/<path>`, for example
`zim:wikipedia_en_all_nopic/Bee_sting`. Parentheses and other characters in the path
are quoted (`Mercury_%28planet%29`). Refs work like the store's (docs/links.md):
"give me a link to that" right after an answer from the reference puts the article's
link in your Scout reminders list, and `open_link` and `send_link` take the ref.

**The page.** The link is the article on Scout's web UI:
`<web base>/ref/<book>/<path>`, on the Tailscale name like the record pages. It
serves the archive's own HTML, images and styles, and needs the token like the rest
of the web UI. The archive's pages run on Scout's origin, so scripts are blocked
(`script-src 'none'`, no fetches, no forms): a page's script could otherwise call
Scout's API with the cookie. Wikipedia pages read fine without them. A redirect
(`Bee_stings`) goes to its article with a relative link, so the links inside work.

## Setting it up

1. Pick archives (below) and download them into
   `~/Library/Application Support/Scout/reference/`. `scripts/fetch-reference.sh`
   does it: it finds the newest copy at download.kiwix.org, resumes a stopped
   download, and checks Kiwix's sha256.

   ```sh
   scripts/fetch-reference.sh wikipedia_en_all_nopic wikipedia_en_medicine_maxi ifixit_en_all
   ```

2. Optional settings, in `config.toml`:

   ```toml
   [reference]
   dir = "reference"           # in Scout's data folder
   archives = []               # names (newest copy) or file names; empty: every .zim in dir
   local_answers = "offline"   # answer from them on this Mac: offline, always, never
   ```

3. Restart Scout. `scoutctl.sh status` shows a line like
   `reference: 2 archives (wikipedia_en_all_nopic 2026-06-01, 52.7 GB; ...)`.

No program needs installing: libzim comes as a Python package. Scout uses the newest
copy of each archive; delete old ones by hand. A file that isn't a ZIM, or a
half-downloaded `.part`, is skipped.

## Which archives

Sizes from download.kiwix.org on 2026-10-15. "maxi" has pictures, "nopic" has none,
"mini" has only each article's introduction.

| archive | what | size |
|---|---|---|
| `wikipedia_en_all_maxi` (2026-08) | all of English Wikipedia, with pictures | 119 GB |
| `wikipedia_en_all_nopic` (2026-06) | all of it, no pictures | 49 GB |
| `wikipedia_en_all_mini` (2026-09) | every article's introduction | 13 GB |
| `wikipedia_en_top_maxi` (2026-09) | the most-read articles, with pictures | 6.5 GB |
| `wikipedia_en_top_nopic` (2026-09) | the most-read articles, no pictures | 2.6 GB |
| `wikipedia_en_top_mini` (2026-09) | their introductions | 283 MB |
| `wikipedia_en_medicine_maxi` (2026-10) | WikiMed: Wikipedia's medical articles, with pictures | 1.7 GB |
| `wikipedia_en_medicine_nopic` (2026-10) | WikiMed, no pictures | 1.0 GB |
| `ifixit_en_all` (2025-12) | iFixit repair guides | 3.3 GB |
| `wikivoyage_en_all_nopic` (2026-09) | Wikivoyage travel guides | 260 MB |

A good set for the RV: `wikipedia_en_all_nopic`, `wikipedia_en_medicine_maxi`,
`ifixit_en_all` and `wikivoyage_en_all_nopic`, about 54 GB. A small one:
`wikipedia_en_top_nopic` and `wikipedia_en_medicine_nopic`, about 3.6 GB.

## Why in Scout's process, not kiwix-serve

kiwix-serve (Homebrew `kiwix-tools`) is a web server for ZIM files. Scout reads them
with libzim, the library kiwix-serve is built on, so the search is the same: the same
Xapian index inside the archive, the same title suggestions. What differs:

- **Nothing to run.** No service to start, keep alive, or give a port, and no
  Homebrew formula. libzim is a 10 MB Python wheel (`libzim` 3.13.1).
- **Cheap.** Measured with `scripts/bench_reference.py` on synthetic archives
  (Apple M6, 24 GB): a 200,000-article archive (789 MB) opens in 2.7 ms; a search
  plus reading the top five articles takes 10.5 ms (16 ms at most); memory goes
  from 24 MB after opening to 88 MB after 30 searches, and those are pages mapped
  from the file, which macOS drops first. Ten times more articles changed the
  search time from 8 to 10.5 ms.
- **One web UI.** The pages come from Scout's web UI, behind its token and on its
  Tailscale name. kiwix-serve has no sign-in, so it would need its own port kept off
  the network.

What it costs: libzim is native code in Scout's process. A damaged archive can abort
libzim, so Scout checks the ZIM signature before opening a file and skips any it
can't read. And libzim is GPL-3.0-or-later.

## Maps (not built)

Offline maps would take three things. None is small, so none is built yet.

1. **Tiles.** Protomaps' basemap is one `.pmtiles` file for the planet. The
   `pmtiles` command (Homebrew `pmtiles`) cuts out a region over HTTP without
   downloading the planet: `pmtiles extract <planet url> region.pmtiles --region=<state
   geojson> --maxzoom=15`. NOMAD ships states this way: Oregon is 379 MB, California
   1.1 GB, all 50 states about 20 GB. A world map to zoom 5 is about 15 MB, for
   underneath.
2. **A viewer.** A page on the web UI with MapLibre GL and the pmtiles protocol,
   served from Scout's own files (no CDN when offline), reading the file with range
   requests. It needs a style and fonts (Protomaps' basemaps assets) stored locally.
   That is about 1 MB of scripts, a web route that serves byte ranges, and the page.
3. **Where the RV is.** Scout has no reliable location yet. The Starlink dish's
   location and IP or Wi-Fi geolocation have been wrong here; a spare iPhone running
   GPS2IP was the plan, put off for now. A USB GPS (NMEA over serial) would also do.
   Once there is a position, the map centers on it, `places_near` and the weather
   can use "here", and the extract can follow the trip (the next state before you
   get there).

Offline search for places (an "OpenStreetMap near me" with no internet) is a fourth,
larger piece: the tiles hold names but not a search index.
