# Scan2Text Web

*Developed by Ms Sushumna Rao for BRAOU Aidea Lab, Dr. B.R. Ambedkar Open University.*

A local web app that turns scanned books (PDFs or images) into **accessible
EPUB and Word (.docx)** files, using free vision models through Ollama.
Everything runs on your computer. Nothing is uploaded to the internet.

## One-time setup
1. Install **Python 3.10+** from python.org (Windows: tick "Add Python to PATH").
2. Install **Ollama** from https://ollama.com/download and start it.
3. Download a model (in a terminal / Command Prompt):
   ```
   ollama pull qwen3-vl:4b
   ```
   Optional: `qwen3-vl:8b` (best quality, slow), `qwen3-vl:2b` (fast),
   `deepseek-ocr` (OCR specialist) or `glm-ocr` (fastest, less formatting).
   Ollama 0.13 or newer is needed for Qwen3-VL and DeepSeek-OCR.

## Start
- **Windows:** double-click `run_windows.bat`
- **Mac/Linux:** `./run_mac_linux.sh`

Your browser opens at **http://127.0.0.1:8000**. Keep the black window open
while you use the app; close it to stop.

## Accounts and sign-in
- Only **@braou.ac.in** e-mail addresses can have accounts. There is no self sign-up.
- **First start:** a setup page asks for the first administrator (name, @braou.ac.in
  e-mail, password). It only works on the computer running Scan2Text.
- **Admins** open **Admin** (top right) to add users. Each new user gets a one-time
  temporary password, which they must change at first sign-in. Admins can also
  make someone an admin, disable an account, or reset a password.
- Each user sees only their own books; admins see everyone's.
- **Activity log** (Admin → Activity log): sign-ins, failed sign-ins, uploads,
  finished/failed conversions, downloads, proofreading, deletions and admin
  changes, with time, user and IP address. Filter it, or download it as CSV.
- 5 wrong passwords lock that account for 15 minutes.
- To use a different e-mail domain, set the environment variable `S2T_DOMAIN`
  (for example `S2T_DOMAIN=example.edu`) before starting.

**Use from other computers on the university network:** start with
`run_windows.bat --lan` (or `./run_mac_linux.sh --lan`), then open
`http://<this-computer's-IP>:8000` on the other device. Everyone must sign in.
Traffic is plain HTTP, so use this only on a trusted internal network; for
wider access, put it behind HTTPS (for example via the university web server).

## How to use
1. Choose one or more scanned PDFs or images.
2. Enter the title and author (optional), pick the model and book language.
3. Click **Convert**. Books are processed one at a time with live progress.
4. Download **EPUB** or **Word**.
5. Click **Proofread** to fix OCR mistakes. **Save & rebuild** updates both files.

## Good to know
- **Speed (16 GB, no GPU):** Qwen3-VL 4B takes roughly 1–2 minutes per page.
  Try a few pages first. An NVIDIA GPU is used automatically and is much faster.
- **Resume:** if you stop a book or close the app, click **Resume**. Pages
  already read are remembered and skipped.
- **Accessibility:** real headings, table of contents, document language,
  marked table headers, list styles, and EPUB accessibility metadata. EPUBs
  pass EPUBCheck. The app itself works with a keyboard and screen readers.
- **Not included:** pictures and figures from the scan.
- **Storage:** books, cache and the user/log database (`scan2text.db`) live in
  `~/.scan2text` (your user folder). Back up `scan2text.db` to keep accounts and logs.
  Delete books in the app, or delete that folder to clear everything.
