# Daily publish

The live site is published **only from `era-filter-1127`**. Each push to that branch runs the
**Publish site** workflow (`.github/workflows/pages.yml`), which unpacks `chunks/`, adds the Money page
and force-pushes the result to the `published` branch.

**Never push packs to `main`.** On `main` the workflow is switched off, so a pack pushed there never reaches the site.

## Each morning

1. **Build the pack** as usual: a base64 tarball split into `chunks/chunk-NN.txt`, plus `chunks/MANIFEST.txt`
   (`chunks=N`, `sha256=…`, `bytes=…`).
2. **Update only `chunks/` on `era-filter-1127`.**
   - Overwrite `chunk-00 … chunk-(N-1)` and `MANIFEST.txt`.
   - **If today's pack has fewer chunks than are in the folder**, empty the extra files to 0 bytes. Don't delete them.
     The workflow joins *every* `chunk-*.txt` in version order (`ls | sort -V`). Leftover data would corrupt the tarball, but empty files add nothing.
     ```bash
     N=$(sed -n 's/^chunks=//p' chunks/MANIFEST.txt)
     for f in chunks/chunk-*.txt; do n=${f##*chunk-}; n=$((10#${n%.txt})); [ "$n" -ge "$N" ] && : > "$f"; done
     ```
3. **Check before pushing**: the joined chunks must match the MANIFEST.
   ```bash
   cat $(ls chunks/chunk-*.txt | sort -V) | base64 -d | sha256sum   # must equal sha256= in MANIFEST
   ```
4. **Commit and push.** Each push to `era-filter-1127` starts a publish run, and a newer run cancels an older one that is still going
   (`cancel-in-progress`). Push the pack as **one commit**, or treat only the run for the last commit as the one that matters.
   If you must split a large pack over several pushes, the runs for half-pushed states can fail or publish a broken site.
   Only the final run counts.
5. **Confirm**:
   - In Actions, the *Publish site* run for your last commit is **success**.
   - On the `published` branch, `index.html` shows today's "Last updated" date and has the Money link.
   - `money/index.html` and `data/money.enc.json` are present.

## Leave these alone in a daily pack

- `money/index.html` (the Money page), `data/money.enc.json` (encrypted money data), `build.py`, `README.md`,
  and `.github/workflows/pages.yml`.
- The Money page is added to the site by the workflow's **Add Money page** step, so packs don't need to include it.
- To refresh the money data, replace only `data/money.enc.json` with a newly encrypted file. Commit it on its own or with the pack.

## Never commit

- Cert numbers, slab-label photos or barcodes: card photos must be the label-cropped ones.
- Plaintext money data (CSV/JSON) or any password. Only the encrypted `data/money.enc.json` goes in the repo.
