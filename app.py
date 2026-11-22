import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
SERVERS_ROOT = BASE_DIR / "hosted_servers"
DATA_FILE = BASE_DIR / "panel_data.json"

TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)
SERVERS_ROOT.mkdir(parents=True, exist_ok=True)

app = Flask(
    __name__,
    template_folder=str(TEMPLATES_DIR),
    static_folder=str(STATIC_DIR),
)
app.secret_key = os.environ.get("SECRET_KEY", "ehab-omar-host-secret-key-2026")

RUNNING_PROCS = {}
START_TIME = time.time()


def load_db():
    if not DATA_FILE.exists():
        now_iso = datetime.utcnow().isoformat()
        data = {
            "users": [
                {
                    "username": "admin",
                    "email": "admin@omar-host.local",
                    "password_hash": generate_password_hash("admin123"),
                    "active": True,
                    "premium": True,
                    "is_admin": True,
                    "server_limit": 16,
                    "expiry": (datetime.utcnow() + timedelta(days=365)).isoformat(),
                    "created_at": now_iso,
                },
                {
                    "username": "user1",
                    "email": "user1@omar-host.local",
                    "password_hash": generate_password_hash("123456"),
                    "active": True,
                    "premium": False,
                    "is_admin": False,
                    "server_limit": 1,
                    "expiry": (datetime.utcnow() + timedelta(days=30)).isoformat(),
                    "created_at": now_iso,
                },
            ],
            "servers": [],
        }
        save_db(data)
        return data
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            data.setdefault("users", [])
            data.setdefault("servers", [])
            return data
    except Exception:
        return {"users": [], "servers": []}


def save_db(data):
    tmp = DATA_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)


def get_current_user(db):
    uname = session.get("username")
    if not uname:
        return None
    for u in db["users"]:
        if u["username"] == uname:
            return u
    return None


def login_required_api(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        db = load_db()
        user = get_current_user(db)
        if not user:
            return jsonify({"success": False, "message": "غير مسجل الدخول"}), 401
        if not user.get("active", True):
            return jsonify({"success": False, "message": "الحساب محظور"}), 403
        return fn(db=db, user=user, *args, **kwargs)
    return wrapper


def admin_required_api(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        db = load_db()
        user = get_current_user(db)
        if not user:
            return jsonify({"success": False, "message": "غير مسجل الدخول"}), 401
        if not user.get("is_admin"):
            return jsonify({"success": False, "message": "صلاحيات الأدمن مطلوبة"}), 403
        return fn(db=db, user=user, *args, **kwargs)
    return wrapper


def safe_slug(name, fallback="server"):
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", (name or "").strip()).strip("_").lower()
    return cleaned[:32] if cleaned else f"{fallback}_{int(time.time()) % 100000}"


def safe_rel_path(rel):
    rel = (rel or "").replace("\\", "/").lstrip("/")
    parts = [p for p in rel.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        return None
    return "/".join(parts)


def get_server_dir(srv):
    owner = safe_slug(srv.get("owner", "user"), "user")
    folder = safe_slug(srv.get("folder", "srv"), "srv")
    p = (SERVERS_ROOT / owner / folder).resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def find_server(db, key_or_folder):
    target = (key_or_folder or "").strip()
    for s in db["servers"]:
        if (
            s.get("key") == target
            or s.get("folder") == target
            or (s.get("key") or "").endswith(f"::{target}")
        ):
            return s
    return None


def user_can_access_server(user, srv):
    return bool(user.get("is_admin") or srv.get("owner") == user.get("username"))


def is_proc_running(key):
    proc = RUNNING_PROCS.get(key)
    if proc is None:
        return False
    return proc.poll() is None


def append_log(srv, line):
    logs = srv.get("logs", "")
    ts = datetime.now().strftime("%H:%M:%S")
    srv["logs"] = (logs + f"[{ts}] {line}\n")[-12000:]


def start_server_process(srv):
    key = srv["key"]
    stop_server_process(srv, silent=True)
    srv_dir = get_server_dir(srv)
    startup = safe_rel_path(srv.get("startup_file") or "main.py") or "main.py"
    target_file = srv_dir / startup

    if not target_file.exists():
        if startup.endswith(".js"):
            target_file.write_text(
                f'console.log("Started {srv.get("display_name", folder)}");\nsetInterval(() => {{}}, 60000);\n',
                encoding="utf-8",
            )
        else:
            target_file.write_text(
                f'import time\nprint("Started {srv.get("display_name", srv.get("folder"))}")\ntime.sleep(2)\n',
                encoding="utf-8",
            )

    log_path = srv_dir / "_console.log"
    with open(log_path, "a", encoding="utf-8") as lf:
        lf.write(f"\n--- Starting {startup} ---\n")

    cmd = ["node", str(target_file)] if startup.endswith(".js") else [sys.executable, "-u", str(target_file)]
    try:
        lf = open(log_path, "a", encoding="utf-8")
        proc = subprocess.Popen(
            cmd,
            cwd=str(srv_dir),
            stdout=lf,
            stderr=subprocess.STDOUT,
            text=True,
        )
        RUNNING_PROCS[key] = proc
        srv["status"] = "Running"
        srv["mem"] = "36 MB"
        append_log(srv, f"Process started ({startup}) PID={proc.pid}")
    except Exception as exc:
        srv["status"] = "Offline"
        srv["mem"] = "0 MB"
        append_log(srv, f"Start error: {exc}")


def stop_server_process(srv, silent=False):
    key = srv["key"]
    proc = RUNNING_PROCS.pop(key, None)
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    if srv.get("status") != "Banned":
        srv["status"] = "Offline"
    srv["mem"] = "0 MB"
    if not silent:
        append_log(srv, "Process stopped.")


# ==================== HTML PAGE ROUTES ====================

@app.route("/")
def page_index():
    db = load_db()
    if not get_current_user(db):
        return redirect(url_for("page_login"))
    return render_template("index.html")


@app.route("/login")
def page_login():
    return render_template("login.html")


@app.route("/create")
def page_create():
    return render_template("create.html")


@app.route("/admin")
def page_admin():
    db = load_db()
    user = get_current_user(db)
    if not user:
        return redirect(url_for("page_login"))
    if not user.get("is_admin"):
        return redirect(url_for("page_index"))
    return render_template("admin.html")


@app.route("/logout")
def page_logout():
    session.clear()
    return redirect(url_for("page_login"))


@app.route("/omar-bro.png")
def page_logo():
    logo_path = STATIC_DIR / "omar-bro.png"
    if logo_path.exists():
        return send_from_directory(str(STATIC_DIR), "omar-bro.png")
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128">'
        '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0%" stop-color="#00d4ff"/><stop offset="100%" stop-color="#7b61ff"/></linearGradient></defs>'
        '<rect width="128" height="128" rx="28" fill="#0b1021"/>'
        '<text x="64" y="76" font-size="44" font-family="sans-serif" font-weight="900" '
        'text-anchor="middle" fill="url(#g)">OH</text></svg>'
    )
    resp = make_response(svg, 200)
    resp.headers["Content-Type"] = "image/svg+xml"
    return resp


@app.route("/srv/<folder>")
def page_srv_preview(folder):
    db = load_db()
    srv = find_server(db, folder)
    if not srv:
        return jsonify({"success": False, "message": "السيرفر غير موجود"}), 404
    return jsonify({
        "success": True,
        "folder": srv["folder"],
        "name": srv.get("display_name"),
        "status": "Banned" if srv.get("banned") else ("Running" if is_proc_running(srv["key"]) else srv.get("status", "Offline")),
        "startup_file": srv.get("startup_file", "main.py"),
    })


# ==================== AUTH API ====================

@app.route("/api/auth/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify({"success": False, "message": "يرجى إدخال اسم المستخدم وكلمة المرور"}), 400

    db = load_db()
    user = next((u for u in db["users"] if u["username"] == username), None)
    if not user or not check_password_hash(user["password_hash"], password):
        return jsonify({"success": False, "message": "اسم المستخدم أو كلمة المرور غير صحيحة"}), 401
    if not user.get("active", True):
        return jsonify({"success": False, "message": "هذا الحساب محظور"}), 403

    session["username"] = user["username"]
    session["is_admin"] = bool(user.get("is_admin"))
    return jsonify({
        "success": True,
        "username": user["username"],
        "is_admin": bool(user.get("is_admin")),
    })


@app.route("/api/auth/create", methods=["POST"])
def api_create_account():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""
    password2 = data.get("password2") or ""

    if not re.match(r"^[A-Za-z0-9_.]{3,24}$", username):
        return jsonify({"success": False, "message": "اسم المستخدم يجب أن يكون بين 3 و24 حرفاً (أحرف/أرقام/_/.)."}), 400
    if "@" not in email or "." not in email.split("@")[-1]:
        return jsonify({"success": False, "message": "أدخل بريداً إلكترونياً صحيحاً"}), 400
    if len(password) < 6:
        return jsonify({"success": False, "message": "كلمة المرور يجب أن تكون 6 أحرف على الأقل"}), 400
    if password != password2:
        return jsonify({"success": False, "message": "كلمتا المرور غير متطابقتين"}), 400

    db = load_db()
    if any(u["username"].lower() == username.lower() for u in db["users"]):
        return jsonify({"success": False, "message": "اسم المستخدم مستخدم مسبقاً"}), 409

    now_iso = datetime.utcnow().isoformat()
    db["users"].append({
        "username": username,
        "email": email,
        "password_hash": generate_password_hash(password),
        "active": True,
        "premium": False,
        "is_admin": False,
        "server_limit": 1,
        "expiry": (datetime.utcnow() + timedelta(days=30)).isoformat(),
        "created_at": now_iso,
    })
    save_db(db)
    return jsonify({"success": True, "message": "تم إنشاء الحساب بنجاح"})


# ==================== SERVERS & FILES API ====================

@app.route("/servers", methods=["GET"])
@login_required_api
def list_servers(db, user):
    items = db["servers"] if user.get("is_admin") else [s for s in db["servers"] if s.get("owner") == user["username"]]
    out = []
    for s in items:
        running = is_proc_running(s["key"])
        status = "Banned" if s.get("banned") else ("Running" if running else s.get("status", "Offline"))
        out.append({
            "key": s["key"],
            "folder": s["folder"],
            "title": s.get("display_name") or s["folder"],
            "subtitle": s.get("subtitle") or f"Owner: {s.get('owner')}",
            "status": status,
            "startup_file": s.get("startup_file") or "main.py",
            "generated_url": url_for("page_srv_preview", folder=s["folder"], _external=True),
            "ip": request.host.split(":")[0],
        })
    return jsonify({"success": True, "is_admin": bool(user.get("is_admin")), "servers": out})


@app.route("/add", methods=["POST"])
@login_required_api
def add_server(db, user):
    data = request.get_json(silent=True) or {}
    raw_name = (data.get("name") or "").strip()
    if not raw_name:
        return jsonify({"success": False, "message": "اسم السيرفر مطلوب"}), 400

    srv_type = (data.get("server_type") or "Python").strip()
    is_js = "js" in srv_type.lower() or "node" in srv_type.lower()
    default_startup = (data.get("startup_file") or "").strip() or ("index.js" if is_js else "main.py")

    owner = user["username"]
    owned = [s for s in db["servers"] if s.get("owner") == owner]
    limit = int(user.get("server_limit") or (16 if user.get("premium") or user.get("is_admin") else 1))
    if not user.get("is_admin") and len(owned) >= limit:
        return jsonify({"success": False, "message": f"تم الوصول للحد الأقصى ({limit} سيرفر)"}), 403

    folder = safe_slug(raw_name, "srv")
    key = f"{owner}::{folder}"
    if any(s["key"] == key for s in db["servers"]):
        folder = f"{folder}_{int(time.time()) % 1000}"
        key = f"{owner}::{folder}"

    srv = {
        "key": key,
        "folder": folder,
        "display_name": raw_name,
        "title": raw_name,
        "subtitle": f"{srv_type} · {folder}",
        "owner": owner,
        "startup_file": default_startup,
        "status": "Offline",
        "banned": False,
        "mem": "0 MB",
        "logs": "",
        "created_at": datetime.utcnow().isoformat(),
    }
    srv_dir = get_server_dir(srv)
    starter = srv_dir / default_startup
    if not starter.exists():
        if is_js:
            starter.write_text(f'console.log("Server {raw_name} ready");\nsetInterval(() => {{}}, 60000);\n', encoding="utf-8")
        else:
            starter.write_text(f'print("Server {raw_name} ready")\n', encoding="utf-8")

    append_log(srv, f"Created server '{raw_name}' ({default_startup})")
    db["servers"].append(srv)
    save_db(db)
    return jsonify({
        "success": True,
        "key": key,
        "folder": folder,
        "generated_url": url_for("page_srv_preview", folder=folder, _external=True),
    })


@app.route("/server/delete/<path:key>", methods=["POST", "DELETE"])
@login_required_api
def delete_server(db, user, key):
    srv = find_server(db, key)
    if not srv:
        return jsonify({"success": False, "message": "السيرفر غير موجود"}), 404
    if not user_can_access_server(user, srv):
        return jsonify({"success": False, "message": "غير مصرح"}), 403

    stop_server_process(srv, silent=True)
    srv_dir = get_server_dir(srv)
    if srv_dir.exists():
        shutil.rmtree(srv_dir, ignore_errors=True)
    db["servers"] = [s for s in db["servers"] if s["key"] != srv["key"]]
    save_db(db)
    return jsonify({"success": True, "message": "تم حذف السيرفر"})


@app.route("/server/stats/<path:key>", methods=["GET"])
@login_required_api
def server_stats(db, user, key):
    srv = find_server(db, key)
    if not srv:
        return jsonify({"success": False, "message": "السيرفر غير موجود"}), 404
    if not user_can_access_server(user, srv):
        return jsonify({"success": False, "message": "غير مصرح"}), 403

    srv_dir = get_server_dir(srv)
    log_file = srv_dir / "_console.log"
    file_logs = ""
    if log_file.exists():
        try:
            file_logs = log_file.read_text(encoding="utf-8", errors="replace")[-8000:]
        except Exception:
            file_logs = ""

    running = is_proc_running(srv["key"])
    if srv.get("banned"):
        status = "Banned"
        mem = "0 MB"
    elif running:
        status = "Running"
        mem = srv.get("mem") or "36 MB"
    else:
        status = "Offline"
        mem = "0 MB"

    combined_logs = (srv.get("logs") or "") + ("\n" + file_logs if file_logs else "")
    return jsonify({
        "success": True,
        "logs": combined_logs.strip() or "Ready.",
        "status": status,
        "mem": mem,
        "ip": request.host.split(":")[0],
        "generated_url": url_for("page_srv_preview", folder=srv["folder"], _external=True),
        "startup_file": srv.get("startup_file") or "main.py",
    })


@app.route("/server/action/<path:key>/<act>", methods=["POST"])
@login_required_api
def server_action(db, user, key, act):
    srv = find_server(db, key)
    if not srv:
        return jsonify({"success": False, "message": "السيرفر غير موجود"}), 404
    if not user_can_access_server(user, srv):
        return jsonify({"success": False, "message": "غير مصرح"}), 403
    if srv.get("banned"):
        return jsonify({"success": False, "message": "السيرفر محظور"}), 403

    if act == "start":
        start_server_process(srv)
    elif act == "restart":
        stop_server_process(srv, silent=True)
        start_server_process(srv)
    elif act == "stop":
        stop_server_process(srv)
    else:
        return jsonify({"success": False, "message": "إجراء غير صالح"}), 400

    save_db(db)
    return jsonify({"success": True, "status": srv.get("status")})


@app.route("/server/set-startup/<path:key>", methods=["POST"])
@login_required_api
def set_startup(db, user, key):
    srv = find_server(db, key)
    if not srv:
        return jsonify({"success": False, "message": "السيرفر غير موجود"}), 404
    if not user_can_access_server(user, srv):
        return jsonify({"success": False, "message": "غير مصرح"}), 403

    data = request.get_json(silent=True) or {}
    rel = safe_rel_path(data.get("file") or "main.py")
    if not rel:
        return jsonify({"success": False, "message": "مسار غير صالح"}), 400

    srv["startup_file"] = rel
    append_log(srv, f"Startup file set to {rel}")
    save_db(db)
    return jsonify({"success": True, "startup_file": rel})


@app.route("/files/list/<path:key>", methods=["GET"])
@login_required_api
def files_list(db, user, key):
    srv = find_server(db, key)
    if not srv:
        return jsonify({"success": False, "message": "السيرفر غير موجود"}), 404
    if not user_can_access_server(user, srv):
        return jsonify({"success": False, "message": "غير مصرح"}), 403

    rel_dir = safe_rel_path(request.args.get("path", ""))
    if rel_dir is None:
        return jsonify({"success": False, "message": "مسار غير صالح"}), 400

    base = get_server_dir(srv)
    target = (base / rel_dir).resolve()
    if not str(target).startswith(str(base.resolve())) or not target.exists():
        return jsonify({"success": False, "message": "المجلد غير موجود"}), 404

    dirs = []
    files = []
    for entry in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        if entry.name == "_console.log":
            continue
        if entry.is_dir():
            dirs.append({"name": entry.name})
        else:
            sz = entry.stat().st_size
            files.append({"name": entry.name, "size": f"{sz} B" if sz < 1024 else f"{sz/1024:.1f} KB"})

    return jsonify({"success": True, "path": rel_dir, "dirs": dirs, "files": files})


@app.route("/files/mkdir/<path:key>", methods=["POST"])
@login_required_api
def files_mkdir(db, user, key):
    srv = find_server(db, key)
    if not srv or not user_can_access_server(user, srv):
        return jsonify({"success": False}), 403
    data = request.get_json(silent=True) or {}
    rel_dir = safe_rel_path(data.get("path", "")) or ""
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", (data.get("name") or "").strip())
    if not name:
        return jsonify({"success": False, "message": "اسم المجلد مطلوب"}), 400
    base = get_server_dir(srv).resolve()
    new_dir = (base / rel_dir / name).resolve()
    if not str(new_dir).startswith(str(base)):
        return jsonify({"success": False}), 400
    new_dir.mkdir(parents=True, exist_ok=True)
    return jsonify({"success": True})


@app.route("/files/rename/<path:key>", methods=["POST"])
@login_required_api
def files_rename(db, user, key):
    srv = find_server(db, key)
    if not srv or not user_can_access_server(user, srv):
        return jsonify({"success": False}), 403
    data = request.get_json(silent=True) or {}
    rel_dir = safe_rel_path(data.get("path", "")) or ""
    old_n = (data.get("old") or "").strip()
    new_n = (data.get("new") or "").strip()
    if not old_n or not new_n:
        return jsonify({"success": False}), 400
    base = get_server_dir(srv).resolve()
    src = (base / rel_dir / old_n).resolve()
    dst = (base / rel_dir / new_n).resolve()
    if not (str(src).startswith(str(base)) and str(dst).startswith(str(base))) or not src.exists():
        return jsonify({"success": False}), 404
    src.rename(dst)
    return jsonify({"success": True})


@app.route("/files/delete/<path:key>", methods=["POST"])
@login_required_api
def files_delete(db, user, key):
    srv = find_server(db, key)
    if not srv or not user_can_access_server(user, srv):
        return jsonify({"success": False}), 403
    data = request.get_json(silent=True) or {}
    rel_dir = safe_rel_path(data.get("path", "")) or ""
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"success": False}), 400
    base = get_server_dir(srv).resolve()
    target = (base / rel_dir / name).resolve()
    if target == base or not str(target).startswith(str(base)) or not target.exists():
        return jsonify({"success": False}), 404
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return jsonify({"success": True})


@app.route("/files/upload/<path:key>", methods=["POST"])
@login_required_api
def files_upload(db, user, key):
    srv = find_server(db, key)
    if not srv or not user_can_access_server(user, srv):
        return jsonify({"success": False}), 403
    rel_dir = safe_rel_path(request.args.get("path", "")) or ""
    base = get_server_dir(srv).resolve()
    uploaded = request.files.getlist("files")
    relpaths = request.form.getlist("relpaths")
    for idx, f in enumerate(uploaded):
        rel = relpaths[idx] if idx < len(relpaths) and relpaths[idx] else f.filename
        clean_rel = safe_rel_path(rel)
        if not clean_rel:
            continue
        dest = (base / rel_dir / clean_rel).resolve()
        if not str(dest).startswith(str(base)):
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        f.save(str(dest))
    return jsonify({"success": True})


@app.route("/files/content/<path:key>", methods=["GET"])
@login_required_api
def files_content(db, user, key):
    srv = find_server(db, key)
    if not srv or not user_can_access_server(user, srv):
        return jsonify({"success": False}), 403
    rel_file = safe_rel_path(request.args.get("file", ""))
    if not rel_file:
        return jsonify({"success": False, "content": ""}), 400
    base = get_server_dir(srv).resolve()
    target = (base / rel_file).resolve()
    if not str(target).startswith(str(base)) or not target.is_file():
        return jsonify({"success": False, "content": ""}), 404
    content = target.read_text(encoding="utf-8", errors="replace")
    return jsonify({"success": True, "content": content})


@app.route("/files/save/<path:key>", methods=["POST"])
@login_required_api
def files_save(db, user, key):
    srv = find_server(db, key)
    if not srv or not user_can_access_server(user, srv):
        return jsonify({"success": False}), 403
    data = request.get_json(silent=True) or {}
    rel_file = safe_rel_path(data.get("file", ""))
    if not rel_file:
        return jsonify({"success": False}), 400
    base = get_server_dir(srv).resolve()
    target = (base / rel_file).resolve()
    if not str(target).startswith(str(base)):
        return jsonify({"success": False}), 400
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(data.get("content", ""), encoding="utf-8")
    append_log(srv, f"Saved {rel_file}")
    save_db(db)
    return jsonify({"success": True})


# ==================== ADMIN API ====================

@app.route("/api/admin/quickstats", methods=["GET"])
@admin_required_api
def admin_quickstats(db, user):
    servers = db["servers"]
    users = db["users"]
    return jsonify({
        "success": True,
        "stats": {
            "servers_total": len(servers),
            "servers_running": sum(1 for s in servers if not s.get("banned") and is_proc_running(s["key"])),
            "servers_installing": sum(1 for s in servers if not s.get("banned") and s.get("status") in ("Installing", "Starting")),
            "servers_banned": sum(1 for s in servers if s.get("banned")),
            "users_total": len(users),
            "users_active": sum(1 for u in users if u.get("active", True)),
            "users_premium": sum(1 for u in users if u.get("premium")),
        },
    })


@app.route("/api/admin/servers", methods=["GET"])
@admin_required_api
def admin_servers(db, user):
    out = []
    for s in db["servers"]:
        running = is_proc_running(s["key"])
        st = "Banned" if s.get("banned") else ("Running" if running else s.get("status", "Offline"))
        out.append({
            "key": s["key"],
            "folder": s["folder"],
            "display_name": s.get("display_name") or s["folder"],
            "owner": s.get("owner"),
            "startup_file": s.get("startup_file") or "main.py",
            "status": st,
            "banned": bool(s.get("banned")),
        })
    return jsonify({"success": True, "servers": out})


@app.route("/api/admin/server/ban", methods=["POST"])
@admin_required_api
def admin_ban_server(db, user):
    data = request.get_json(silent=True) or {}
    srv = find_server(db, data.get("key") or data.get("folder"))
    if not srv:
        return jsonify({"success": False, "message": "السيرفر غير موجود"}), 404
    banned = bool(data.get("banned"))
    srv["banned"] = banned
    if banned:
        stop_server_process(srv, silent=True)
        srv["status"] = "Banned"
    elif srv.get("status") == "Banned":
        srv["status"] = "Offline"
    save_db(db)
    return jsonify({"success": True})


@app.route("/api/admin/server/delete", methods=["POST"])
@admin_required_api
def admin_delete_server(db, user):
    data = request.get_json(silent=True) or {}
    srv = find_server(db, data.get("key") or data.get("folder"))
    if not srv:
        return jsonify({"success": False, "message": "السيرفر غير موجود"}), 404
    stop_server_process(srv, silent=True)
    shutil.rmtree(get_server_dir(srv), ignore_errors=True)
    db["servers"] = [s for s in db["servers"] if s["key"] != srv["key"]]
    save_db(db)
    return jsonify({"success": True})


@app.route("/api/admin/users", methods=["GET"])
@admin_required_api
def admin_users(db, user):
    out = []
    for u in db["users"]:
        srv_count = sum(1 for s in db["servers"] if s.get("owner") == u["username"])
        out.append({
            "username": u["username"],
            "email": u.get("email", ""),
            "servers": srv_count,
            "server_limit": int(u.get("server_limit") or (16 if u.get("premium") else 1)),
            "active": bool(u.get("active", True)),
            "premium": bool(u.get("premium")),
            "expiry": u.get("expiry"),
        })
    return jsonify({"success": True, "users": out})


@app.route("/api/admin/user/update", methods=["POST"])
@admin_required_api
def admin_update_user(db, user):
    data = request.get_json(silent=True) or {}
    uname = (data.get("username") or "").strip()
    target = next((u for u in db["users"] if u["username"] == uname), None)
    if not target:
        return jsonify({"success": False, "message": "المستخدم غير موجود"}), 404

    if "active" in data and isinstance(data["active"], bool):
        target["active"] = data["active"]
    if "premium" in data and isinstance(data["premium"], bool):
        target["premium"] = data["premium"]
        if data["premium"] and int(target.get("server_limit") or 1) < 16:
            target["server_limit"] = 16
        elif not data["premium"] and not target.get("is_admin") and "server_limit" not in data:
            target["server_limit"] = 1
    if "server_limit" in data:
        target["server_limit"] = max(1, min(100, int(data["server_limit"])))
    if "add_days" in data and int(data["add_days"]) > 0:
        base_dt = datetime.utcnow()
        try:
            if target.get("expiry"):
                parsed = datetime.fromisoformat(target["expiry"])
                if parsed > base_dt:
                    base_dt = parsed
        except Exception:
            pass
        target["expiry"] = (base_dt + timedelta(days=int(data["add_days"]))).isoformat()
    if data.get("new_password"):
        target["password_hash"] = generate_password_hash(str(data["new_password"]))

    save_db(db)
    return jsonify({"success": True})


@app.route("/api/admin/user/delete", methods=["POST"])
@admin_required_api
def admin_delete_user(db, user):
    data = request.get_json(silent=True) or {}
    uname = (data.get("username") or "").strip()
    if uname == user["username"]:
        return jsonify({"success": False, "message": "لا يمكنك حذف حسابك الحالي"}), 400
    for s in [s for s in db["servers"] if s.get("owner") == uname]:
        stop_server_process(s, silent=True)
        shutil.rmtree(get_server_dir(s), ignore_errors=True)
    db["servers"] = [s for s in db["servers"] if s.get("owner") != uname]
    db["users"] = [u for u in db["users"] if u["username"] != uname]
    save_db(db)
    return jsonify({"success": True})


@app.route("/api/admin/create-user", methods=["POST"])
@admin_required_api
def admin_create_user(db, user):
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not re.match(r"^[A-Za-z0-9_.]{3,24}$", username) or len(password) < 4:
        return jsonify({"success": False, "message": "اسم المستخدم أو كلمة المرور غير صالحة"}), 400
    if any(u["username"].lower() == username.lower() for u in db["users"]):
        return jsonify({"success": False, "message": "اسم المستخدم موجود مسبقاً"}), 409

    premium = bool(data.get("premium"))
    limit = int(data.get("server_limit") or (16 if premium else 1))
    days = int(data.get("days") or 30)
    db["users"].append({
        "username": username,
        "email": (data.get("email") or f"{username}@omar-host.local").strip(),
        "password_hash": generate_password_hash(password),
        "active": True,
        "premium": premium,
        "is_admin": False,
        "server_limit": limit,
        "expiry": (datetime.utcnow() + timedelta(days=days)).isoformat(),
        "created_at": datetime.utcnow().isoformat(),
    })
    save_db(db)
    return jsonify({"success": True})


@app.route("/api/admin/system", methods=["GET"])
@admin_required_api
def admin_system(db, user):
    total_files = 0
    for root, _, files in os.walk(SERVERS_ROOT):
        total_files += len(files)
    uptime_sec = int(time.time() - START_TIME)
    hours, rem = divmod(uptime_sec, 3600)
    mins, secs = divmod(rem, 60)

    cpu_pct = "12"
    mem_pct = "28"
    mem_used = "180 MB"
    mem_total = "1 GB"
    disk_pct = "15"
    disk_used = "1.2 GB"
    disk_total = "10 GB"

    try:
        import psutil  # type: ignore
        cpu_pct = str(round(psutil.cpu_percent(interval=0.1), 1))
        vm = psutil.virtual_memory()
        mem_pct = str(round(vm.percent, 1))
        mem_used = f"{vm.used // (1024*1024)} MB"
        mem_total = f"{vm.total // (1024*1024)} MB"
        du = shutil.disk_usage(str(BASE_DIR))
        disk_pct = str(round((du.used / du.total) * 100, 1))
        disk_used = f"{du.used / (1024**3):.1f} GB"
        disk_total = f"{du.total / (1024**3):.1f} GB"
    except Exception:
        du = shutil.disk_usage(str(BASE_DIR))
        disk_pct = str(round((du.used / du.total) * 100, 1))
        disk_used = f"{du.used / (1024**3):.1f} GB"
        disk_total = f"{du.total / (1024**3):.1f} GB"

    return jsonify({
        "success": True,
        "system": {
            "cpu_percent": cpu_pct,
            "mem_percent": mem_pct,
            "mem_used": mem_used,
            "mem_total": mem_total,
            "disk_percent": disk_pct,
            "disk_used": disk_used,
            "disk_total": disk_total,
            "os": f"{sys.platform}",
            "uptime": f"{hours}h {mins}m {secs}s",
            "python_version": sys.version.split()[0],
            "ip": request.host.split(":")[0],
            "total_files": total_files,
        },
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
