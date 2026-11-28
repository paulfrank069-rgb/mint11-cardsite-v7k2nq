# mint11-cardsite-v7k2nq
Static site

## Money page

`/money/` is a password-protected dashboard (cash in/out, realized and unrealized P/L, held inventory, sold cards, counted transactions).

- Data lives only in `data/money.enc.json`, encrypted with AES-256-GCM (key from PBKDF2-SHA256, 600000 iterations). The page fetches it with `cache: "no-store"` and decrypts it in the browser with `window.crypto.subtle`. No plaintext data and no password are in the repo.
- **To refresh the data, replace `data/money.enc.json`** with a newly encrypted file and publish. Nothing else needs rebuilding. A new file has a new salt, so open tabs ask for the password again.
- Unlocking keeps a session key in `sessionStorage` for that tab only (never the password or the decrypted data). The **Lock** button or closing the tab clears it.
- The page hides cert numbers: it drops any `cert*`/`slab*`/`label` fields and masks cert-like numbers in notes.
- The publish workflow copies `money/` and `data/` into the site. `build.py` copies them into `pages/` too and adds the Cards / Money nav.
