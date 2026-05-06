# B-Roll-Finder review — top 5 fixes

Patch in `top-5-fixes.patch` against `erfsalehi/B-Roll-Finder@main`
(parent commit `8aad71a`).

## How to apply

```sh
git clone https://github.com/erfsalehi/B-Roll-Finder
cd B-Roll-Finder
git checkout -b fixes/top-5-review
git apply --index ../path/to/top-5-fixes.patch
git commit -m "Fix top 5 review issues"
```

## What it fixes

1. **`app.py`** — read uploaded files via `getvalue()` instead of `read()`,
   so Streamlit reruns don't drain the buffer and silently produce an empty
   script / 0-byte audio.
2. **`core/youtube.py:search_youtube_single`** — drop
   `force_generic_extractor: True` and add `default_search='ytsearch'` so
   YouTube search works reliably across yt-dlp versions.
3. **`core/youtube.py:download_video`** — change `outtmpl` to use
   `'%(ext)s'` so yt-dlp doesn't leave fragment files (`.f137.mp4`, etc.)
   or pick a surprise extension. `merge_output_format='mp4'` still produces
   `.mp4` at the expected path.
4. **`core/output.py:generate_fcpxml`** — declare `<resources><format
   id="r1">` so Final Cut Pro will actually import the file. Size the
   sequence/gap to the real shot range, escape attribute quotes, and use
   real per-shot durations for markers.
5. **`core/keywords.py:parse_groq_response`** — assign keyword blocks to
   slots by **order of appearance**, not by `[HH:MM:SS]` string match, so
   two slots within the same second don't collide and lose keywords.

   Bonus included in the same patch: narrow the tenacity retry to network /
   transient errors only (`APIConnectionError`, `RateLimitError`,
   `InternalServerError`, `requests.RequestException`, `TimeoutError`) so
   bad API keys fail fast instead of looping ~30s.

## Verification

Local sanity ran in `/tmp/broll`:

- `python -m py_compile app.py core/youtube.py core/keywords.py core/output.py` → OK
- FCPXML output round-trips through `xml.etree.ElementTree.fromstring`,
  declares `format id="r1"`, and contains the expected marker count.
- `parse_groq_response` correctly disambiguates two slots that share the
  same `[00:00:00]` marker (regression test for fix #5).

The yt-dlp / Streamlit fixes (#1, #2, #3) are surface-level swaps and
weren't end-to-end tested in this environment because they need a live
Streamlit server, real video URLs, and real API keys.
