"""
Scan2Text Web — turns scanned books into accessible EPUB and Word files using
free vision models via Ollama. Sign-in is limited to admin-added accounts on
the university domain (default braou.ac.in), and every action is logged.

Run:   python server.py          (opens http://127.0.0.1:8000)
LAN:   python server.py --lan    (lets other devices on your network use it)
"""
import hashlib
import json
import os
import pathlib
import queue
import re
import shutil
import sys
import threading
import time
import uuid

import ollama
import pymupdf
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel

import auth
import docx_builder
import epub_builder

BASE = pathlib.Path(__file__).parent
STATIC = BASE / "static"
DATA = pathlib.Path(os.environ.get("S2T_DATA", pathlib.Path.home() / ".scan2text"))
CACHE = DATA / "cache"
JOBS_DIR = DATA / "jobs"
CACHE.mkdir(parents=True, exist_ok=True)
JOBS_DIR.mkdir(parents=True, exist_ok=True)
auth.init(DATA / "scan2text.db")

COOKIE = "s2t_session"
MODELS = {
    "qwen3-vl:4b": "Qwen3-VL 4B · recommended",
    "qwen3-vl:8b": "Qwen3-VL 8B · best quality, slow",
    "qwen3-vl:2b": "Qwen3-VL 2B · fast",
    "deepseek-ocr": "DeepSeek-OCR 3B · OCR specialist",
    "qwen2.5vl:3b": "Qwen2.5-VL 3B",
    "qwen2.5vl:7b": "Qwen2.5-VL 7B · slow",
    "glm-ocr": "GLM-OCR · fastest, less formatting",
}
# models trained on one fixed prompt; everything else gets MD_PROMPT
FIXED_PROMPTS = {
    "glm-ocr": "Text Recognition:",
    "deepseek-ocr": "<|grounding|>Convert the document to markdown.",
}
LANGS = {"te": "Telugu", "hi": "Hindi", "en": "English"}
LANG_HINT = {
    "te": "The page is written in Telugu. Copy the text exactly in Telugu script "
          "(any English words stay in English). Do not translate or transliterate.\n",
    "hi": "The page is written in Hindi. Copy the text exactly in Devanagari script, "
          "keeping every matra, nukta and conjunct, and the danda (।) punctuation "
          "(any English words stay in English). Do not translate or transliterate.\n",
    "en": "The page is written in English.\n",
}
ALLOWED = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}
MD_PROMPT = (
    "{lang_hint}"
    "Convert this scanned book page to Markdown. Rules:\n"
    "- Use # for chapter titles, ## for section headings, ### for sub-headings.\n"
    "- Keep **bold**, *italic*, lists, block quotes and tables.\n"
    "- One blank line between paragraphs; do not break lines inside a paragraph.\n"
    "- Leave out page numbers and running headers/footers.\n"
    "- Output only the Markdown, no comments."
)

app = FastAPI(title="Scan2Text", docs_url=None, redoc_url=None, openapi_url=None)
jobs: dict[str, dict] = {}
work_q: "queue.Queue[str]" = queue.Queue()


# ================= auth helpers =================
def client_ip(request: Request):
    return request.client.host if request.client else None


@app.middleware("http")
async def same_origin_writes(request: Request, call_next):
    """Block cross-site form posts: state-changing requests must come from this app's own pages."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        host = request.headers.get("host")
        if origin and host and origin.split("://", 1)[-1] != host:
            return PlainTextResponse("Cross-site request blocked", status_code=403)
    resp = await call_next(request)
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    return resp


def current_user(request: Request):
    u = auth.session_user(request.cookies.get(COOKIE))
    if not u:
        raise HTTPException(401, "Please sign in.")
    return u


def require_admin(u=Depends(current_user)):
    if u["role"] != "admin":
        raise HTTPException(403, "Administrators only.")
    return u


def set_session_cookie(response: Response, request: Request, token: str):
    response.set_cookie(COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True,
                        samesite="lax", secure=request.url.scheme == "https", path="/")


def is_local(request: Request):
    return client_ip(request) in ("127.0.0.1", "::1", "localhost")


# ================= pages =================
def page(name):
    return FileResponse(STATIC / name, headers={"Cache-Control": "no-store"})


@app.get("/", include_in_schema=False)
def home(request: Request):
    if auth.user_count() == 0:
        return RedirectResponse("/setup")
    if not auth.session_user(request.cookies.get(COOKIE)):
        return RedirectResponse("/login")
    return page("index.html")


@app.get("/login", include_in_schema=False)
def login_page():
    if auth.user_count() == 0:
        return RedirectResponse("/setup")
    return page("login.html")


@app.get("/setup", include_in_schema=False)
def setup_page():
    if auth.user_count() > 0:
        return RedirectResponse("/login")
    return page("setup.html")


@app.get("/admin", include_in_schema=False)
def admin_page(request: Request):
    u = auth.session_user(request.cookies.get(COOKIE))
    if not u:
        return RedirectResponse("/login")
    if u["role"] != "admin":
        return RedirectResponse("/")
    return page("admin.html")


@app.get("/style.css", include_in_schema=False)
def css():
    return FileResponse(STATIC / "style.css", media_type="text/css")


# ================= auth API =================
class LoginIn(BaseModel):
    email: str
    password: str


class SetupIn(BaseModel):
    email: str
    name: str
    password: str


class PasswordIn(BaseModel):
    current: str
    new: str


@app.get("/api/auth/state")
def auth_state(request: Request):
    u = auth.session_user(request.cookies.get(COOKIE))
    return {"setup_needed": auth.user_count() == 0, "domain": auth.ALLOWED_DOMAIN,
            "user": auth.public_user(u)}


@app.post("/api/auth/setup")
def setup(body: SetupIn, request: Request, response: Response):
    if auth.user_count() > 0:
        raise HTTPException(409, "Setup is already done.")
    if not is_local(request):
        raise HTTPException(403, "The first administrator must be created on the computer running Scan2Text.")
    if problem := auth.password_problem(body.password):
        raise HTTPException(400, problem)
    try:
        u = auth.create_user(body.email, body.name, body.password, role="admin",
                             created_by="setup", must_change=False)
    except ValueError as e:
        raise HTTPException(400, str(e))
    auth.log("setup_admin_created", u["email"], "", client_ip(request))
    set_session_cookie(response, request, auth.new_session(u["id"]))
    return {"user": auth.public_user(u)}


@app.post("/api/auth/login")
def login(body: LoginIn, request: Request, response: Response):
    email = auth.normalize_email(body.email)
    if not auth.email_allowed(email):
        auth.log("login_failed", email, "not a university address", client_ip(request))
        raise HTTPException(400, f"Use your @{auth.ALLOWED_DOMAIN} e-mail address.")
    u, err = auth.authenticate(email, body.password)
    if not u:
        auth.log("login_failed", email, err, client_ip(request))
        raise HTTPException(401, err)
    set_session_cookie(response, request, auth.new_session(u["id"]))
    auth.log("login", u["email"], "", client_ip(request))
    return {"user": auth.public_user(u)}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE)
    u = auth.session_user(token)
    auth.end_session(token)
    response.delete_cookie(COOKIE, path="/")
    if u:
        auth.log("logout", u["email"], "", client_ip(request))
    return {"ok": True}


@app.post("/api/auth/password")
def change_password(body: PasswordIn, request: Request, u=Depends(current_user)):
    if not auth.check_pw(body.current, u["pw_hash"]):
        raise HTTPException(400, "Current password is wrong.")
    if problem := auth.password_problem(body.new):
        raise HTTPException(400, problem)
    if body.new == body.current:
        raise HTTPException(400, "Choose a new password that differs from the current one.")
    auth.update_user(u["id"], pw_hash=auth.hash_pw(body.new), must_change=0)
    auth.end_all_sessions(u["id"], keep=request.cookies.get(COOKIE))
    auth.log("password_changed", u["email"], "", client_ip(request))
    return {"ok": True}


# ================= admin API =================
class NewUserIn(BaseModel):
    email: str
    name: str = ""
    role: str = "user"


class UserPatch(BaseModel):
    role: str | None = None
    active: bool | None = None
    name: str | None = None


@app.get("/api/admin/users")
def admin_users(admin=Depends(require_admin)):
    return auth.list_users()


@app.post("/api/admin/users")
def admin_add_user(body: NewUserIn, request: Request, admin=Depends(require_admin)):
    pw = auth.temp_password()
    try:
        u = auth.create_user(body.email, body.name, pw, role=body.role, created_by=admin["email"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    auth.log("user_added", admin["email"], f"{u['email']} as {u['role']}", client_ip(request))
    return {"user": auth.public_user(u), "temp_password": pw}


@app.patch("/api/admin/users/{uid}")
def admin_update_user(uid: int, body: UserPatch, request: Request, admin=Depends(require_admin)):
    u = auth.get_user(uid)
    if not u:
        raise HTTPException(404, "User not found")
    removing_admin = (body.role == "user" and u["role"] == "admin") or \
                     (body.active is False and u["role"] == "admin" and u["active"])
    if removing_admin and auth.active_admins() <= 1:
        raise HTTPException(400, "There must always be at least one active administrator.")
    changes = []
    if body.role in ("user", "admin") and body.role != u["role"]:
        auth.update_user(uid, role=body.role)
        changes.append(f"role → {body.role}")
    if body.active is not None and body.active != bool(u["active"]):
        auth.update_user(uid, active=int(body.active))
        changes.append("enabled" if body.active else "disabled")
    if body.name and body.name.strip() != u["name"]:
        auth.update_user(uid, name=body.name.strip())
        changes.append(f"name → {body.name.strip()}")
    if changes:
        auth.log("user_updated", admin["email"], f"{u['email']}: {', '.join(changes)}", client_ip(request))
    return auth.public_user(auth.get_user(uid))


@app.post("/api/admin/users/{uid}/reset")
def admin_reset_password(uid: int, request: Request, admin=Depends(require_admin)):
    u = auth.get_user(uid)
    if not u:
        raise HTTPException(404, "User not found")
    pw = auth.temp_password()
    auth.update_user(uid, pw_hash=auth.hash_pw(pw), must_change=1)
    auth.end_all_sessions(uid)
    auth.log("password_reset", admin["email"], u["email"], client_ip(request))
    return {"temp_password": pw}


@app.get("/api/admin/logs")
def admin_logs(email: str = "", action: str = "", limit: int = 300, before: int = 0,
               admin=Depends(require_admin)):
    return {"logs": auth.get_logs(email or None, action or None, limit, before or None),
            "actions": auth.log_actions()}


@app.get("/api/admin/logs.csv")
def admin_logs_csv(request: Request, email: str = "", action: str = "", admin=Depends(require_admin)):
    import csv
    import io
    rows = auth.get_logs(email or None, action or None, 5000)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time", "user", "action", "detail", "ip"])
    for r in rows:
        w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])),
                    r["email"] or "", r["action"], r["detail"] or "", r["ip"] or ""])
    auth.log("logs_exported", admin["email"], f"{len(rows)} rows", client_ip(request))
    return Response(buf.getvalue().encode("utf-8-sig"), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="scan2text-activity-log.csv"'})


# ================= job storage =================
def job_dir(jid):
    return JOBS_DIR / jid


def save_job(job):
    (job_dir(job["id"]) / "job.json").write_text(json.dumps(job), encoding="utf-8")


def public(job):
    keys = ("id", "filename", "title", "author", "model", "lang", "state", "page", "owner",
            "total", "error", "warnings", "created", "started", "finished", "eta")
    return {k: job.get(k) for k in keys}


def load_jobs():
    for f in JOBS_DIR.glob("*/job.json"):
        try:
            job = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if job["state"] in ("queued", "running"):
            job["state"] = "interrupted"  # server was closed mid-way; can resume
        job["cancel"] = False
        job.setdefault("owner", None)
        jobs[job["id"]] = job


# ================= OCR =================
_THINK = re.compile(r"<think>.*?</think>", re.S)
_GROUND = re.compile(r"<\|det\|>.*?<\|/det\|>|<\|/?ref\|>|<\|[a-z_/]+\|>", re.S)


def tidy_output(text):
    """Remove model 'thinking' blocks and DeepSeek-OCR layout tags."""
    text = _THINK.sub("", text)
    text = _GROUND.sub("", text)
    text = re.sub(r"^(image|text|title|sub_title|table|figure)\n", "", text, flags=re.M)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def ocr_page(png, model, lang="en"):
    prompt = FIXED_PROMPTS.get(model) or MD_PROMPT.format(lang_hint=LANG_HINT.get(lang, ""))
    key = hashlib.sha256(png + model.encode() + prompt.encode()).hexdigest()
    cached = CACHE / f"{key}.txt"
    if cached.exists():
        return cached.read_text(encoding="utf-8")
    r = ollama.chat(model=model, messages=[{"role": "user", "content": prompt, "images": [png]}])
    text = tidy_output(r["message"]["content"])
    cached.write_text(text, encoding="utf-8")
    return text


def build_outputs(job, md):
    d = job_dir(job["id"])
    (d / "book.md").write_text(md, encoding="utf-8")
    cover = (d / "cover.png").read_bytes() if (d / "cover.png").exists() else None
    (d / "book.epub").write_bytes(epub_builder.build_reflowable(
        md, job["title"], job["author"], job["lang"], cover_png=cover))
    (d / "book.docx").write_bytes(docx_builder.build_docx(
        md, job["title"], job["author"], job["lang"]))


def process(job):
    d = job_dir(job["id"])
    doc = pymupdf.open(d / job["source"])
    job.update(state="running", total=len(doc), page=0, started=time.time(),
               error=None, warnings=0, eta=None)
    save_job(job)
    pages, failures_in_row, t0 = [], 0, time.time()
    for i, page_ in enumerate(doc, start=1):
        if job["cancel"]:
            job.update(state="cancelled", eta=None)
            save_job(job)
            return
        png = page_.get_pixmap(dpi=job["dpi"]).tobytes("png")
        if i == 1:
            (d / "cover.png").write_bytes(png)
        try:
            pages.append(ocr_page(png, job["model"], job["lang"]))
            failures_in_row = 0
        except Exception as e:
            failures_in_row += 1
            job["warnings"] += 1
            pages.append(f"[OCR failed on page {i}: {e}]")
            if failures_in_row >= 3:
                raise RuntimeError(f"OCR keeps failing — is Ollama running? ({e})")
        job["page"] = i
        job["eta"] = int((time.time() - t0) / i * (len(doc) - i))
        if i % 5 == 0:
            save_job(job)
    job["eta"] = None
    build_outputs(job, epub_builder.clean_pages(pages))
    job.update(state="done", finished=time.time())
    save_job(job)
    auth.log("convert_done", job.get("owner"),
             f"{job['title']} · {job['total']} pages · {int(job['finished'] - job['started'])}s")


def worker():
    while True:
        jid = work_q.get()
        job = jobs.get(jid)
        if not job or job["state"] != "queued":
            continue
        try:
            process(job)
        except Exception as e:
            job.update(state="error", error=str(e), eta=None)
            save_job(job)
            auth.log("convert_failed", job.get("owner"), f"{job['title']}: {e}")


# ================= book API =================
class MarkdownIn(BaseModel):
    markdown: str


@app.get("/api/status")
def status(u=Depends(current_user)):
    try:
        resp = ollama.list()
        models = resp.get("models", []) if isinstance(resp, dict) else resp.models
        have = set()
        for m in models:
            name = (m.get("model") or m.get("name")) if isinstance(m, dict) else m.model
            have |= {name, name.removesuffix(":latest")}
        ok, err = True, None
    except Exception as e:
        have, ok, err = set(), False, str(e)
    return {
        "ollama": ok, "error": err,
        "models": [{"id": k, "label": v, "installed": k in have} for k, v in MODELS.items()],
        "languages": [{"id": k, "label": v} for k, v in LANGS.items()],
    }


def visible(job, u):
    return u["role"] == "admin" or job.get("owner") == u["email"]


@app.get("/api/jobs")
def list_jobs(u=Depends(current_user)):
    mine = (public(j) for j in jobs.values() if visible(j, u))
    return sorted(mine, key=lambda j: j["created"], reverse=True)


@app.post("/api/jobs")
async def create_job(request: Request, file: UploadFile = File(...), model: str = Form(...),
                     lang: str = Form("en"), dpi: int = Form(300),
                     title: str = Form(""), author: str = Form(""), u=Depends(current_user)):
    ext = pathlib.Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED:
        raise HTTPException(400, f"Unsupported file type: {ext or 'unknown'}")
    if model not in MODELS:
        raise HTTPException(400, "Unknown model")
    jid = uuid.uuid4().hex[:12]
    d = job_dir(jid)
    d.mkdir(parents=True)
    with open(d / f"source{ext}", "wb") as out:
        while chunk := await file.read(1 << 20):
            out.write(chunk)
    try:
        pymupdf.open(d / f"source{ext}").close()
    except Exception:
        shutil.rmtree(d)
        raise HTTPException(400, "This file could not be opened. Is it a valid PDF or image?")
    stem = pathlib.Path(file.filename).stem
    job = {"id": jid, "filename": file.filename, "source": f"source{ext}",
           "title": title.strip() or stem, "author": author.strip(), "model": model,
           "lang": lang if lang in LANGS else "en", "dpi": max(150, min(dpi, 400)),
           "owner": u["email"], "state": "queued", "page": 0, "total": None, "error": None,
           "warnings": 0, "created": time.time(), "started": None, "finished": None,
           "eta": None, "cancel": False}
    jobs[jid] = job
    save_job(job)
    work_q.put(jid)
    auth.log("upload", u["email"], f"{job['title']} ({file.filename}) · {model} · {LANGS[job['lang']]}",
             client_ip(request))
    return public(job)


def get_job(jid, u):
    job = jobs.get(jid)
    if not job or not visible(job, u):
        raise HTTPException(404, "Book not found")
    return job


@app.post("/api/jobs/{jid}/cancel")
def cancel(jid: str, request: Request, u=Depends(current_user)):
    job = get_job(jid, u)
    if job["state"] == "queued":
        job["state"] = "cancelled"
    job["cancel"] = True
    save_job(job)
    auth.log("stop", u["email"], job["title"], client_ip(request))
    return public(job)


@app.post("/api/jobs/{jid}/retry")
def retry(jid: str, request: Request, u=Depends(current_user)):
    job = get_job(jid, u)
    if job["state"] in ("queued", "running"):
        raise HTTPException(409, "Already in progress")
    job.update(state="queued", cancel=False, error=None)
    save_job(job)
    work_q.put(jid)  # finished pages come from the cache, so this resumes quickly
    auth.log("resume", u["email"], job["title"], client_ip(request))
    return public(job)


@app.delete("/api/jobs/{jid}")
def delete(jid: str, request: Request, u=Depends(current_user)):
    job = get_job(jid, u)
    job["cancel"] = True
    jobs.pop(jid, None)
    shutil.rmtree(job_dir(jid), ignore_errors=True)
    auth.log("delete", u["email"], f"{job['title']} (owner {job.get('owner') or 'unknown'})",
             client_ip(request))
    return {"ok": True}


@app.get("/api/jobs/{jid}/markdown")
def get_markdown(jid: str, u=Depends(current_user)):
    get_job(jid, u)
    f = job_dir(jid) / "book.md"
    if not f.exists():
        raise HTTPException(404, "Not ready yet")
    return {"markdown": f.read_text(encoding="utf-8")}


@app.put("/api/jobs/{jid}/markdown")
def put_markdown(jid: str, body: MarkdownIn, request: Request, u=Depends(current_user)):
    job = get_job(jid, u)
    if job["state"] != "done":
        raise HTTPException(409, "Book is not finished yet")
    build_outputs(job, body.markdown)
    auth.log("proofread_saved", u["email"], job["title"], client_ip(request))
    return {"ok": True}


@app.get("/api/jobs/{jid}/download/{fmt}")
def download(jid: str, fmt: str, request: Request, u=Depends(current_user)):
    job = get_job(jid, u)
    files = {"epub": ("book.epub", "application/epub+zip"),
             "docx": ("book.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
             "md": ("book.md", "text/markdown")}
    if fmt not in files:
        raise HTTPException(404)
    name, mime = files[fmt]
    f = job_dir(jid) / name
    if not f.exists():
        raise HTTPException(404, "Not ready yet")
    safe = re.sub(r'[\\/:*?"<>|]+', "_", job["title"]).strip() or "book"
    auth.log("download", u["email"], f"{job['title']} · {fmt.upper()}", client_ip(request))
    return FileResponse(f, media_type=mime, filename=f"{safe}.{fmt}")


load_jobs()
threading.Thread(target=worker, daemon=True).start()

if __name__ == "__main__":
    import uvicorn
    import webbrowser

    lan = "--lan" in sys.argv
    host, port = ("0.0.0.0" if lan else "127.0.0.1"), int(os.environ.get("S2T_PORT", 8000))
    url = f"http://127.0.0.1:{port}"
    print(f"\n  Scan2Text is running → {url}\n  (Press Ctrl+C to stop)\n")
    if lan:
        print(f"  Network mode: others can open http://<this-computer's-IP>:{port}\n"
              f"  Only @{auth.ALLOWED_DOMAIN} accounts added by an admin can sign in.\n")
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
