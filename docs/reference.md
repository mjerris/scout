# Offline reference: Wikipedia and friends with no internet

Scout can keep reference works on the Mac (Wikipedia, WikiMed, iFixit, Wikivoyage) and
answer "look up X" from them when Starlink is down. They are Kiwix ZIM archives, the
same files the Kiwix apps and Project NOMAD use. Scout reads them through kiwix-serve,
Kiwix's own server, which it runs on 127.0.0.1. The code is `src/scout/reference.py`
(search, sections, answers) and `src/scout/kiwix.py` (kiwix-serve).

## How it works

**Read in place.** Each archive carries its own full-text index (Xapian) and title
index. Scout searches those indexes through kiwix-serve (`/search` and `/suggest`) and
reads only the articles a question needs (`/raw/<book>/content/<path>`). Nothing is
copied into Scout's store: Wikipedia alone is 49 GB without pictures.

**kiwix-serve.** The app runs it as a child process: `kiwix-serve --address 127.0.0.1
--port 8769 --attachToProcess <Scout's pid> --skipInvalid <archives>`, with the newest
copy of each archive Scout uses. It looks at the folder every minute and restarts
kiwix-serve when the archives change, starts it again if it exits (waiting 10 s, then
30 s, 1 and 5 minutes while it keeps failing), and stops it when Scout stops;
`--attachToProcess` makes it exit by itself if Scout is killed. Its output goes to
`logs/kiwix-serve.log`. The phone service and the MCP server read through the same
port. `voice_status` has a `reference` entry (running, not installed, no archives,
failed; its version, address, pid and how many archives it serves).

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
the web is slow or down. Without kiwix-serve they say so: "The offline reference isn't
installed: kiwix-serve, the program that reads the archives, is missing", or that it
isn't running right now.

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
serves the archive's own HTML, images and styles (from kiwix-serve's `/raw`, so no
Kiwix toolbar), and needs the token like the rest of the web UI. kiwix-serve itself
listens only on 127.0.0.1. The archive's pages run on Scout's origin, so scripts are blocked
(`script-src 'none'`, no fetches, no forms): a page's script could otherwise call
Scout's API with the cookie. Wikipedia pages read fine without them. A redirect
(`Bee_stings`) goes to its article with a relative link, so the links inside work.

## Setting it up

1. Pick archives (below) and download them into
   `~/Library/Application Support/Scout/reference/`. `scripts/fetch-reference.sh`
   does it: it finds the newest copy at download.kiwix.org, resumes a stopped
   download, and checks Kiwix's sha256. Then it installs kiwix-serve
   (`scripts/install-kiwix.sh`).

   ```sh
   scripts/fetch-reference.sh wikipedia_en_all_nopic wikipedia_en_medicine_maxi ifixit_en_all
   ```

2. Optional settings, in `config.toml`:

   ```toml
   [reference]
   dir = "reference"           # in Scout's data folder
   archives = []               # names (newest copy) or file names; empty: every .zim in dir
   local_answers = "offline"   # answer from them on this Mac: offline, always, never
   port = 8769                 # kiwix-serve listens on 127.0.0.1 here
   ```

3. Within a minute Scout starts kiwix-serve on them (no restart needed).
   `scoutctl.sh status` shows a line like `reference: 2 archives
   (wikipedia_en_all_nopic 2026-06-01, 52.7 GB; ...) in ..., through kiwix-serve on
   127.0.0.1:8769`.

**kiwix-serve** comes from Kiwix's official kiwix-tools release for macOS on Apple
silicon (`kiwix-tools_macos-arm64-3.8.2.tar.gz` at download.kiwix.org/release/kiwix-tools/;
Homebrew has no kiwix-tools formula). `scripts/install-kiwix.sh` puts only `kiwix-serve`
in Scout's `bin/` folder, never system-wide. It is pinned to 3.8.2 and checked against
the tarball's sha256 in the script and the md5 Kiwix publishes beside it (Kiwix
publishes no sha256 for kiwix-tools), then the binary's own sha256; an installed copy
that matches is left alone. Scout's installer runs it once there are archives.

Scout uses the newest copy of each archive; delete old ones by hand. A file that isn't
a ZIM, or a half-downloaded `.part`, is skipped, and kiwix-serve skips one it can't
read.

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

## Why kiwix-serve, not libzim in Scout's process

Scout used to read the archives with libzim, the library kiwix-serve is built on, as a
Python package in its own process. That saved running a server, but libzim is
GPL-3.0-or-later, and a program that loads a GPL library into its own process is
generally treated as one combined work under the GPL's terms. Scout's code is public
and carries no license of its own, so it keeps GPL code out of its process: kiwix-serve
is a separate program, run as it is and spoken to over HTTP, and libzim is gone from
Scout's dependencies (only the test-archive and benchmark scripts use it, through
`uv run --with`). What changed:

- **The same search.** kiwix-serve searches the same Xapian index inside the archive
  and the same title index, so results are the same. Scout still cuts articles at
  their headings itself.
- **A process to run.** The app starts, restarts and stops it (above), on a port only
  this Mac can reach. kiwix-serve has no sign-in, so it never listens on the network:
  pages reach other devices only through Scout's web UI, behind its token.
- **What it costs.** Measured with `scripts/bench_reference.py` on synthetic archives
  (Apple M6, 24 GB): kiwix-serve answers 27 to 40 ms after it starts; a search plus
  reading the top five articles takes 16 to 17 ms (28 ms at most), against 8 to 10.5 ms
  with libzim in Scout's process; kiwix-serve uses 13 to 17 MB after starting and about
  70 MB after 30 searches, mostly pages mapped from the archive, which macOS drops
  first. On the real WikiMed and Wikipedia top archives (3.8 GB together) a search of
  both, with the articles' first lines, takes 70 to 90 ms.
- **A damaged archive** can't take Scout down any more: kiwix-serve skips one it can't
  read, and if it crashes, Scout starts it again.

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
