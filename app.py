import os
import smtplib
import sqlite3
from datetime import datetime
from pathlib import Path
import secrets
from dotenv import load_dotenv
from flask import Flask, g, render_template, request, redirect, url_for, flash, session, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from email.message import EmailMessage
import ssl

# --- Optional: OpenAI ---
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

load_dotenv()

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "app.db"
UPLOAD_DIR = BASE_DIR / "uploads"
AVATAR_DIR = UPLOAD_DIR / "avatars"
RECIPE_IMG_DIR = UPLOAD_DIR / "recipes"
for d in [UPLOAD_DIR, AVATAR_DIR, RECIPE_IMG_DIR]:
    d.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY","dev-secret")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB files

# --- SocketIO ---
from flask_socketio import SocketIO, join_room, emit

# Force threading mode to avoid eventlet compatibility issues with Python 3.13
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")

# ---------- Email / OTP ----------
def send_otp_email(to_email: str, otp_code: str):
    user = os.getenv("MAIL_USERNAME")
    pwd = os.getenv("MAIL_PASSWORD")
    if not user or not pwd:
        raise RuntimeError("MAIL_USERNAME/MAIL_PASSWORD must be set for Gmail OTP.")
    
    msg = EmailMessage()
    msg["Subject"] = "Your FoodHub+ Verification Code"
    msg["From"] = user
    msg["To"] = to_email
    msg.set_content(f"Your verification code is: {otp_code}\n\nPlease enter this code to verify your account.")
    
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(user, pwd)
        server.send_message(msg)

def generate_otp():
    return f"{secrets.randbelow(1000000):06d}"

# ---------- DB helpers ----------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            bio TEXT,
            avatar_url TEXT,
            is_verified INTEGER NOT NULL DEFAULT 0,
            otp_code TEXT,
            otp_expires TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS blogs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS recipes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            ingredients TEXT NOT NULL,
            steps TEXT NOT NULL,
            image_url TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            description TEXT,
            is_public INTEGER NOT NULL DEFAULT 1,
            owner_id INTEGER NOT NULL,
            invite_code TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (owner_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS group_members (
            id INTEGER PRIMARY KEY AUTOinCREMENT,
            group_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL DEFAULT 'member',
            UNIQUE(group_id, user_id),
            FOREIGN KEY (group_id) REFERENCES groups(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (group_id) REFERENCES groups(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        """
    )
    db.commit()

with app.app_context():
    init_db()

# ---------- Auth helpers ----------
def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

@app.before_request
def load_user():
    g.user = current_user()

def login_required(view):
    from functools import wraps
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not g.user:
            flash("Please log in to continue.")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapper

# ---------- Routes ----------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/register", methods=["GET","POST"])
def register():
    if request.method == "POST":
        username = request.form["username"].strip()
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        if not username or not email or not password:
            flash("All fields are required.")
            return redirect(url_for("register"))
        db = get_db()
        try:
            db.execute(
                "INSERT INTO users (username,email,password_hash,bio,created_at) VALUES (?,?,?,?,?)",
                (username, email, generate_password_hash(password), "", datetime.utcnow().isoformat())
            )
            db.commit()
        except sqlite3.IntegrityError:
            flash("Username or email already exists.")
            return redirect(url_for("register"))
        # Fetch new user and set OTP
        user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        otp = generate_otp()
        expires = (datetime.utcnow().timestamp() + 600)  # 10 minutes
        db.execute("UPDATE users SET otp_code=?, otp_expires=? WHERE id=?", (otp, str(expires), user["id"]))
        db.commit()
        try:
            send_otp_email(email, otp)
        except Exception as e:
            # Fail hard (as requested earlier): clean user and show error
            db.execute("DELETE FROM users WHERE id=?", (user["id"],))
            db.commit()
            flash("Failed to send OTP via Gmail. Set MAIL_USERNAME/MAIL_PASSWORD and allow 'App Passwords' for your account.")
            return redirect(url_for("register"))
        # Store pending user for verification step
        session["pending_email"] = email
        flash("OTP sent to your email. Please verify to activate your account.")
        return redirect(url_for("verify"))
    return render_template("register.html")

@app.route("/verify", methods=["GET","POST"])
def verify():
    email = session.get("pending_email")
    if not email:
        flash("No pending verification session. Please register first.")
        return redirect(url_for("register"))
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not user:
        flash("User not found for verification.")
        return redirect(url_for("register"))
    if request.method == "POST":
        code = (request.form.get("otp","") or "").strip()
        # Validate OTP and expiry
        try:
            expires_ts = float(user["otp_expires"] or "0")
        except ValueError:
            expires_ts = 0.0
        if not code or code != (user["otp_code"] or "") or datetime.utcnow().timestamp() > expires_ts:
            flash("Invalid or expired OTP.")
            return redirect(url_for("verify"))
        db.execute("UPDATE users SET is_verified=1, otp_code=NULL, otp_expires=NULL WHERE id=?", (user["id"],))
        db.commit()
        session.pop("pending_email", None)
        flash("Email verified! You can now log in.")
        return redirect(url_for("login"))
    return render_template("verify.html")

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            if not user["is_verified"]:
                session["pending_email"] = email
                flash("Please verify your email before logging in.")
                return redirect(url_for("verify"))
            session["user_id"] = user["id"]
            return redirect(url_for("index"))
        flash("Invalid credentials.")
    return render_template("login.html")

@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

# ---------- Profile ----------
@app.get("/u/<username>")
def profile(username):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not user:
        flash("User not found.")
        return redirect(url_for("index"))
    return render_template("profile.html", user=user)

@app.route("/profile/edit", methods=["GET","POST"])
@login_required
def edit_profile():
    db = get_db()
    user = g.user
    if request.method == "POST":
        bio = request.form.get("bio","").strip()
        avatar = request.files.get("avatar")
        avatar_name = None
        if avatar and avatar.filename:
            fn = secure_filename(avatar.filename)
            ext = os.path.splitext(fn)[1].lower()
            if ext not in [".png",".jpg",".jpeg",".webp"]:
                flash("Unsupported image type.")
                return redirect(url_for("edit_profile"))
            avatar_name = f"user_{user['id']}{ext}"
            avatar.save(AVATAR_DIR / avatar_name)
        if avatar_name:
            db.execute("UPDATE users SET bio=?, avatar_url=? WHERE id=?", (bio, avatar_name, user["id"]))
        else:
            db.execute("UPDATE users SET bio=? WHERE id=?", (bio, user["id"]))
        db.commit()
        flash("Profile updated.")
        return redirect(url_for("profile", username=user["username"]))
    # GET
    u = db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
    return render_template("edit_profile.html", user=u)

@app.get("/uploads/avatars/<path:filename>")
def uploaded_avatar(filename):
    return send_from_directory(AVATAR_DIR, filename)

@app.get("/uploads/recipes/<path:filename>")
def uploaded_recipe_image(filename):
    return send_from_directory(RECIPE_IMG_DIR, filename)

# ---------- Blogs ----------
@app.get("/blogs")
@login_required
def blogs():
    db = get_db()
    my_blogs = db.execute("SELECT * FROM blogs WHERE user_id=? ORDER BY id DESC", (g.user["id"],)).fetchall()
    return render_template("blogs.html", my_blogs=my_blogs)

@app.post("/blogs/create")
@login_required
def create_blog():
    title = request.form["title"].strip()
    content = request.form["content"].strip()
    if not title or not content:
        flash("Title and content are required.")
        return redirect(url_for("blogs"))
    db = get_db()
    db.execute("INSERT INTO blogs (user_id,title,content,created_at) VALUES (?,?,?,?)",
               (g.user["id"], title, content, datetime.utcnow().isoformat()))
    db.commit()
    flash("Blog published.")
    return redirect(url_for("blogs"))

# ---------- Recipes ----------
@app.get("/recipes")
@login_required
def recipes():
    db = get_db()
    my_recipes = db.execute("SELECT * FROM recipes WHERE user_id=? ORDER BY id DESC", (g.user["id"],)).fetchall()
    return render_template("recipes.html", my_recipes=my_recipes)

@app.post("/recipes/create")
@login_required
def create_recipe():
    title = request.form["title"].strip()
    description = request.form.get("description","").strip()
    ingredients = request.form["ingredients"].strip()
    steps = request.form["steps"].strip()
    photo = request.files.get("photo")
    image_url = None
    if photo and photo.filename:
        fn = secure_filename(photo.filename)
        ext = os.path.splitext(fn)[1].lower()
        if ext not in [".png",".jpg",".jpeg",".webp"]:
            flash("Unsupported image type.")
            return redirect(url_for("recipes"))
        image_url = f"recipe_{g.user['id']}_{int(datetime.utcnow().timestamp())}{ext}"
        photo.save(RECIPE_IMG_DIR / image_url)
    db = get_db()
    db.execute("""
        INSERT INTO recipes (user_id,title,description,ingredients,steps,image_url,created_at)
        VALUES (?,?,?,?,?,?,?)
    """, (g.user["id"], title, description, ingredients, steps, image_url, datetime.utcnow().isoformat()))
    db.commit()
    flash("Recipe saved.")
    return redirect(url_for("recipes"))

# ---------- Groups & Chat (Public + Private with invite codes) ----------
@app.get("/groups")
@login_required
def groups():
    db = get_db()
    rows = db.execute("SELECT * FROM groups ORDER BY id DESC").fetchall()
    all_groups = []
    for r in rows:
        is_member = db.execute("SELECT 1 FROM group_members WHERE group_id=? AND user_id=?", (r["id"], g.user["id"])).fetchone()
        owner_row = db.execute("SELECT username FROM users WHERE id=?", (r["owner_id"],)).fetchone()
        d = dict(r)
        d["is_member"] = bool(is_member)
        d["owner_name"] = owner_row["username"] if owner_row else "Unknown"
        all_groups.append(d)
    return render_template("groups.html", all_groups=all_groups, g=g)

@app.post("/groups/create")
@login_required
def create_group():
    name = request.form["name"].strip()
    description = request.form.get("description","").strip()
    is_public = 1 if request.form.get("is_public","1") == "1" else 0
    db = get_db()
    try:
        invite_code = None if is_public == 1 else secrets.token_urlsafe(8)
        db.execute("INSERT INTO groups (name,description,is_public,owner_id,invite_code,created_at) VALUES (?,?,?,?,?,?)",
                   (name, description, is_public, g.user["id"], invite_code, datetime.utcnow().isoformat()))
        db.commit()
    except sqlite3.IntegrityError:
        flash("A group with that name already exists.")
        return redirect(url_for("groups"))
    gid = db.execute("SELECT id FROM groups WHERE name=?", (name,)).fetchone()["id"]
    db.execute("INSERT OR IGNORE INTO group_members (group_id,user_id,role) VALUES (?,?,?)",
               (gid, g.user["id"], "owner"))
    db.commit()
    if is_public == 0:
        flash("Private group created. Share the invite code shown in the list so others can join.")
    else:
        flash("Public group created.")
    return redirect(url_for("groups"))

@app.post("/groups/join/<int:group_id>")
@login_required
def join_group(group_id):
    db = get_db()
    grp = db.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
    if not grp:
        flash("Group not found."); return redirect(url_for("groups"))
    if not grp["is_public"]:
        flash("This group is private. Use invite code to join.")
        return redirect(url_for("groups"))
    db.execute("INSERT OR IGNORE INTO group_members (group_id,user_id,role) VALUES (?,?,?)",
               (group_id, g.user["id"], "member"))
    db.commit()
    flash("Joined group.")
    return redirect(url_for("enter_group", group_id=group_id))

@app.post("/groups/join_code")
@login_required
def join_by_code():
    code_str = (request.form.get("invite_code","") or "").strip()
    if not code_str:
        flash("Invite code required.")
        return redirect(url_for("groups"))
    db = get_db()
    grp = db.execute("SELECT * FROM groups WHERE invite_code=?", (code_str,)).fetchone()
    if not grp:
        flash("Invalid invite code.")
        return redirect(url_for("groups"))
    db.execute("INSERT OR IGNORE INTO group_members (group_id,user_id,role) VALUES (?,?,?)",
               (grp["id"], g.user["id"], "member"))
    db.commit()
    flash("Joined private group.")
    return redirect(url_for("enter_group", group_id=grp["id"]))

@app.get("/groups/<int:group_id>")
@login_required
def enter_group(group_id):
    db = get_db()
    grp = db.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
    if not grp:
        flash("Group not found."); return redirect(url_for("groups"))
    m = db.execute("SELECT 1 FROM group_members WHERE group_id=? AND user_id=?", (group_id, g.user["id"])).fetchone()
    if not m:
        flash("Join the group first.")
        return redirect(url_for("groups"))
    messages = db.execute("""
        SELECT messages.*, users.username FROM messages
        JOIN users ON users.id = messages.user_id
        WHERE group_id=? ORDER BY id ASC
    """,(group_id,)).fetchall()
    return render_template("group_room.html", group=grp, messages=messages)

@socketio.on("join")
def on_join(data):
    room = data.get("room")
    join_room(room)

@socketio.on("message")
def on_message(data):
    text = (data.get("text") or "").strip()
    room = data.get("room")
    
    if not text:
        return
    
    # Get user ID from session (not from g.user which is not available in SocketIO context)
    user_id = session.get("user_id")
    if not user_id:
        return
    
    # Get a new database connection for this thread
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    
    # Get user info from database
    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        db.close()
        return
    
    # room format: group_<id>
    try:
        gid = int(room.split("_")[1])
    except Exception:
        db.close()
        return
    
    # Insert the message into the database
    db.execute("INSERT INTO messages (group_id,user_id,content,created_at) VALUES (?,?,?,?)",
               (gid, user_id, text, datetime.utcnow().isoformat()))
    db.commit()
    
    # Format the timestamp for display
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    
    # Emit the message to all clients in the room
    emit("message", {
        "username": user["username"],
        "text": text,
        "created_at": timestamp
    }, room=room)
    
    db.close()

# ---------- AI Suggestion ----------
def offline_recipe(ingredients_list):
    title = f"Quick {' & '.join(ingredients_list[:2])} Surprise" if ingredients_list else "Quick Pantry Surprise"
    steps = [
        "Prep all ingredients and heat a pan.",
        "Sauté aromatics, add mains, and season to taste.",
        "Simmer until flavors meld. Serve hot."
    ]
    return {
        "title": title,
        "summary": "A speedy, flexible recipe generated offline when no API key is set.",
        "ingredients": [i.strip() for i in ingredients_list if i.strip()],
        "steps": steps
    }

def openai_recipe(ingredients_list):
    api_key = os.getenv("OPENAI_API_KEY","")
    if not api_key or OpenAI is None:
        return offline_recipe(ingredients_list)
    client = OpenAI(api_key=api_key)
    prompt = (
        "Create a concise recipe using ONLY these ingredients (you may assume pantry basics: salt, pepper, oil):\n"
        f"{', '.join(ingredients_list)}\n"
        "Return JSON with keys: title, summary, ingredients (list), steps (list). Keep it simple."
    )
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content":prompt}],
            temperature=0.7
        )
        content = resp.choices[0].message.content
        import json, re
        match = re.search(r'\{.*\}', content, re.S)
        if match:
            return json.loads(match.group(0))
    except Exception as e:
        pass
    return offline_recipe(ingredients_list)

@app.route("/ai", methods=["GET","POST"])
@login_required
def ai_suggest():
    suggestion = None
    if request.method == "POST":
        ingredients = request.form["ingredients"]
        ing_list = [s.strip() for s in ingredients.split(",") if s.strip()]
        suggestion = openai_recipe(ing_list)
    return render_template("ai.html", suggestion=suggestion)

# ---------- CLI ----------
@app.cli.command("init-db")
def _init_db_cmd():
    init_db()
    print("Database initialized.")

if __name__ == "__main__":
    # Use socketio.run so chat works in dev
    socketio.run(app, host="0.0.0.0", port=5000, debug=False)
