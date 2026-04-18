"""Flask web interface for the admin backend.

Run:
    python -m modules.admin_web
or via CLI wrapper:
    python -m modules.admin_web --host 0.0.0.0 --port 5055
"""

import argparse
import logging
import secrets
from functools import wraps
from pathlib import Path
from typing import Any, Dict

from config import settings
from modules.admin import AdminManager

logger = logging.getLogger(__name__)


_BASE_CSS = """
<style>
body { font-family: system-ui, sans-serif; max-width: 1000px; margin: 20px auto; padding: 0 16px; color:#222; }
h1,h2 { color:#1e3a8a; }
nav a { margin-right:12px; }
table { border-collapse: collapse; width:100%; margin:10px 0; }
th,td { border:1px solid #ccc; padding:6px 10px; text-align:left; font-size:14px; }
th { background:#f3f4f6; }
.flash { padding:8px; background:#fef3c7; border-left:4px solid #f59e0b; margin:10px 0; }
.ok   { background:#d1fae5; border-color:#10b981; }
.err  { background:#fee2e2; border-color:#ef4444; }
form.inline { display:inline; }
input,select,textarea { padding:6px; margin:4px 0; }
button { background:#1e3a8a; color:white; border:0; padding:8px 14px; cursor:pointer; border-radius:4px; }
button.danger { background:#dc2626; }
pre { background:#f9fafb; padding:8px; overflow:auto; font-size:12px; }
.chunk { border:1px solid #e5e7eb; padding:10px; margin:10px 0; background:#fafafa; }
</style>
"""


def create_app(admin_manager: AdminManager = None):
    try:
        from flask import (
            Flask, request, redirect, url_for, session,
            render_template_string, flash, jsonify, abort,
        )
        from werkzeug.utils import secure_filename
    except ImportError as e:
        raise ImportError(
            "Flask + werkzeug required. Install with: pip install Flask"
        ) from e

    admin = admin_manager or AdminManager()
    cfg = settings.admin

    app = Flask(__name__)
    app.secret_key = cfg.secret_key or secrets.token_hex(16)
    app.config["MAX_CONTENT_LENGTH"] = cfg.max_upload_mb * 1024 * 1024

    # ---------- auth ----------

    def login_required(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not session.get("logged_in"):
                return redirect(url_for("login", next=request.path))
            return f(*args, **kwargs)
        return wrapper

    LOGIN_HTML = _BASE_CSS + """
    <h1>Admin Login</h1>
    {% with messages = get_flashed_messages() %}
      {% for m in messages %}<div class="flash err">{{m}}</div>{% endfor %}
    {% endwith %}
    <form method="post">
      <label>Username <input name="username" autofocus></label><br>
      <label>Password <input type="password" name="password"></label><br>
      <button>Login</button>
    </form>
    """

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            u = request.form.get("username", "").strip()
            p = request.form.get("password", "")
            if u == cfg.username and p == cfg.password:
                session["logged_in"] = True
                nxt = request.args.get("next") or url_for("dashboard")
                return redirect(nxt)
            flash("Invalid credentials")
        return render_template_string(LOGIN_HTML)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ---------- layout ----------

    NAV = """
    <nav>
      <a href="{{ url_for('dashboard') }}">Dashboard</a> |
      <a href="{{ url_for('upload_page') }}">Upload Doc</a> |
      <a href="{{ url_for('content_page') }}">Content</a> |
      <a href="{{ url_for('questions_page') }}">Questions</a> |
      <a href="{{ url_for('bulk_questions_page') }}">Bulk Upload</a> |
      <a href="{{ url_for('flags_page') }}">Flags</a> |
      <a href="{{ url_for('report_page') }}">Report</a> |
      <a href="{{ url_for('logout') }}">Logout</a>
    </nav><hr>
    """

    def render(body_html: str, **ctx):
        page = _BASE_CSS + NAV + body_html
        return render_template_string(page, **ctx)

    # ---------- dashboard ----------

    DASH_HTML = """
    <h1>Admin Dashboard</h1>
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% for cat, m in messages %}<div class="flash {{cat}}">{{m}}</div>{% endfor %}
    {% endwith %}
    <h2>Recent uploads</h2>
    <table>
      <tr><th>File</th><th>Exam</th><th>Subject</th><th>Topic</th><th>Chunks</th><th>Status</th><th>When</th></tr>
      {% for u in uploads %}
        <tr>
          <td>{{u.filename}}</td><td>{{u.exam_type}}</td>
          <td>{{u.subject}}</td><td>{{u.topic}}</td>
          <td>{{u.chunk_count}}</td><td>{{u.status}}</td>
          <td>{{u.uploaded_at}}</td>
        </tr>
      {% endfor %}
    </table>
    """

    @app.route("/")
    @login_required
    def dashboard():
        uploads = admin.list_uploads()[:20]
        return render(DASH_HTML, uploads=uploads)

    # ---------- upload ----------

    UPLOAD_HTML = """
    <h1>Upload Document</h1>
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% for cat, m in messages %}<div class="flash {{cat}}">{{m}}</div>{% endfor %}
    {% endwith %}
    <form method="post" enctype="multipart/form-data">
      <label>File: <input type="file" name="file" required></label><br>
      <label>Exam:
        <select name="exam_type">
          {% for e in exams %}<option value="{{e}}">{{e}}</option>{% endfor %}
        </select>
      </label><br>
      <label>Subject: <input name="subject" value="General"></label><br>
      <label>Topic: <input name="topic" value="General"></label><br>
      <label>Language:
        <select name="language">
          <option value="en">English</option><option value="ta">Tamil</option>
        </select>
      </label><br>
      <button>Upload & Preview</button>
    </form>
    """

    PREVIEW_HTML = """
    <h1>Preview: {{p.filename}}</h1>
    <p><b>Upload ID:</b> {{p.upload_id}}</p>
    <p>
      Exam: <b>{{p.exam_type}}</b> | Subject: <b>{{p.subject}}</b> |
      Topic: <b>{{p.topic}}</b> | Language: {{p.language}}
    </p>
    <p>Pages: {{p.page_count}} | Chunks: {{p.chunk_count}} |
       OCR: {{p.ocr_used}} ({{p.ocr_confidence}}) |
       Tables: {{p.tables_found}} | Tamil valid: {{p.tamil_valid}}</p>
    {% if p.duplicate_of %}
      <div class="flash err">Duplicate of upload {{p.duplicate_of}}</div>
    {% endif %}
    <h2>Chunk Preview (first {{p.chunks|length}})</h2>
    {% for c in p.chunks %}
      <div class="chunk">
        <b>#{{c.index}}</b> · {{c.token_count}} tokens
        <pre>{{c.text[:800]}}{% if c.text|length > 800 %}...{% endif %}</pre>
      </div>
    {% endfor %}
    <form method="post" action="{{ url_for('commit_upload', upload_id=p.upload_id) }}" class="inline">
      <button>Commit to RAG</button>
    </form>
    <form method="post" action="{{ url_for('discard_upload', upload_id=p.upload_id) }}" class="inline">
      <button class="danger">Discard</button>
    </form>
    """

    @app.route("/upload", methods=["GET", "POST"])
    @login_required
    def upload_page():
        if request.method == "POST":
            f = request.files.get("file")
            if not f or not f.filename:
                flash("No file selected", "err")
                return redirect(url_for("upload_page"))
            staged = Path(cfg.upload_dir) / "_incoming"
            staged.mkdir(parents=True, exist_ok=True)
            dest = staged / secure_filename(f.filename)
            f.save(dest)
            try:
                preview = admin.process_and_preview(
                    str(dest),
                    {
                        "exam_type": request.form.get("exam_type"),
                        "subject": request.form.get("subject", "General"),
                        "topic": request.form.get("topic", "General"),
                        "language": request.form.get("language", "en"),
                    },
                )
                return render(PREVIEW_HTML, p=preview)
            except Exception as e:
                logger.exception("upload failed")
                flash(f"Processing failed: {e}", "err")
                return redirect(url_for("upload_page"))
        return render(UPLOAD_HTML, exams=settings.exam.supported_exams)

    @app.route("/upload/<upload_id>/commit", methods=["POST"])
    @login_required
    def commit_upload(upload_id):
        try:
            res = admin.commit_upload(upload_id)
            flash(f"Committed {res['chunks_committed']} chunks", "ok")
        except Exception as e:
            flash(f"Commit failed: {e}", "err")
        return redirect(url_for("dashboard"))

    @app.route("/upload/<upload_id>/discard", methods=["POST"])
    @login_required
    def discard_upload(upload_id):
        admin.discard_upload(upload_id)
        flash("Upload discarded", "ok")
        return redirect(url_for("dashboard"))

    # ---------- content management ----------

    CONTENT_HTML = """
    <h1>Manage Content</h1>
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% for cat, m in messages %}<div class="flash {{cat}}">{{m}}</div>{% endfor %}
    {% endwith %}
    <h2>Delete by source filename</h2>
    <form method="post" action="{{ url_for('delete_source') }}">
      <input name="source" placeholder="e.g. history.pdf" required>
      <button class="danger">Delete all chunks from source</button>
    </form>
    <h2>Update chunk metadata</h2>
    <form method="post" action="{{ url_for('update_chunk_metadata') }}">
      <input name="chunk_id" placeholder="chunk UUID" required><br>
      <input name="subject" placeholder="new subject">
      <input name="topic" placeholder="new topic">
      <input name="exam_type" placeholder="new exam_type">
      <button>Update Metadata</button>
    </form>
    """

    @app.route("/content")
    @login_required
    def content_page():
        return render(CONTENT_HTML)

    @app.route("/content/delete-source", methods=["POST"])
    @login_required
    def delete_source():
        source = request.form.get("source", "").strip()
        if not source:
            flash("Source required", "err")
            return redirect(url_for("content_page"))
        try:
            res = admin.manage_content("delete_source", source=source)
            flash(f"Removed {res['removed']} chunks from {source}", "ok")
        except Exception as e:
            flash(f"Delete failed: {e}", "err")
        return redirect(url_for("content_page"))

    @app.route("/content/update-metadata", methods=["POST"])
    @login_required
    def update_chunk_metadata():
        cid = request.form.get("chunk_id", "").strip()
        meta = {
            k: v for k, v in {
                "subject": request.form.get("subject"),
                "topic": request.form.get("topic"),
                "exam_type": request.form.get("exam_type"),
            }.items() if v
        }
        try:
            res = admin.manage_content("update_metadata", content_id=cid, metadata=meta)
            flash(f"Updated {cid}: {res['metadata']}", "ok")
        except Exception as e:
            flash(f"Update failed: {e}", "err")
        return redirect(url_for("content_page"))

    # ---------- questions ----------

    Q_LIST_HTML = """
    <h1>Questions</h1>
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% for cat, m in messages %}<div class="flash {{cat}}">{{m}}</div>{% endfor %}
    {% endwith %}
    <form method="get" action="{{ url_for('questions_page') }}">
      <input name="exam_type" placeholder="Exam" value="{{f.exam_type or ''}}">
      <input name="subject" placeholder="Subject" value="{{f.subject or ''}}">
      <input name="topic" placeholder="Topic" value="{{f.topic or ''}}">
      <select name="difficulty">
        <option value="">-- difficulty --</option>
        <option {{'selected' if f.difficulty=='beginner'}}>beginner</option>
        <option {{'selected' if f.difficulty=='medium'}}>medium</option>
        <option {{'selected' if f.difficulty=='advanced'}}>advanced</option>
      </select>
      <button>Filter</button>
      <a href="{{ url_for('new_question') }}">+ New question</a>
    </form>
    <table>
      <tr><th>ID</th><th>Text</th><th>Subj/Topic</th><th>Diff</th><th>Exam</th><th>Actions</th></tr>
      {% for q in questions %}
        <tr>
          <td>{{q.question_id[:8]}}</td>
          <td>{{q.text[:80]}}</td>
          <td>{{q.subject}} / {{q.topic}}</td>
          <td>{{q.difficulty}}</td>
          <td>{{q.exam_type}}</td>
          <td>
            <a href="{{ url_for('edit_question', qid=q.question_id) }}">edit</a> ·
            <form method="post" action="{{ url_for('flag_question_route', qid=q.question_id) }}" class="inline">
              <button>flag</button>
            </form>
            <form method="post" action="{{ url_for('delete_question_route', qid=q.question_id) }}" class="inline">
              <button class="danger">del</button>
            </form>
          </td>
        </tr>
      {% endfor %}
    </table>
    """

    Q_FORM_HTML = """
    <h1>{{title}}</h1>
    <form method="post">
      <label>Text<br><textarea name="text" rows="3" cols="80" required>{{q.text or ''}}</textarea></label><br>
      <label>A <input name="option_a" value="{{q.option_a or ''}}" required></label><br>
      <label>B <input name="option_b" value="{{q.option_b or ''}}" required></label><br>
      <label>C <input name="option_c" value="{{q.option_c or ''}}" required></label><br>
      <label>D <input name="option_d" value="{{q.option_d or ''}}" required></label><br>
      <label>Correct:
        <select name="correct_answer">
          {% for o in ['A','B','C','D'] %}
            <option {{'selected' if q.correct_answer==o}}>{{o}}</option>
          {% endfor %}
        </select>
      </label><br>
      <label>Explanation<br><textarea name="explanation" rows="2" cols="80">{{q.explanation or ''}}</textarea></label><br>
      <label>Subject <input name="subject" value="{{q.subject or 'General'}}"></label>
      <label>Topic <input name="topic" value="{{q.topic or 'General'}}"></label>
      <label>Exam <input name="exam_type" value="{{q.exam_type or 'TNPSC'}}"></label>
      <label>Diff
        <select name="difficulty">
          {% for d in ['beginner','medium','advanced'] %}
            <option {{'selected' if q.difficulty==d}}>{{d}}</option>
          {% endfor %}
        </select>
      </label>
      <label>Year <input name="year" value="{{q.year or ''}}" size="6"></label>
      <label>Lang
        <select name="language">
          <option value="en" {{'selected' if q.language=='en'}}>en</option>
          <option value="ta" {{'selected' if q.language=='ta'}}>ta</option>
        </select>
      </label><br>
      <button>Save</button>
    </form>
    """

    @app.route("/questions")
    @login_required
    def questions_page():
        filters = {
            "exam_type": request.args.get("exam_type") or None,
            "subject": request.args.get("subject") or None,
            "topic": request.args.get("topic") or None,
            "difficulty": request.args.get("difficulty") or None,
        }
        questions = admin.question_bank.load_questions(
            **{k: v for k, v in filters.items() if v},
            limit=100, shuffle=False,
        )
        return render(Q_LIST_HTML, questions=questions, f=filters)

    @app.route("/questions/new", methods=["GET", "POST"])
    @login_required
    def new_question():
        if request.method == "POST":
            data = {k: request.form.get(k, "") for k in [
                "text", "option_a", "option_b", "option_c", "option_d",
                "correct_answer", "explanation", "subject", "topic",
                "exam_type", "difficulty", "language",
            ]}
            yr = request.form.get("year")
            data["year"] = int(yr) if yr and yr.isdigit() else None
            try:
                qid = admin.add_question(data)
                flash(f"Added question {qid}", "ok")
                return redirect(url_for("questions_page"))
            except Exception as e:
                flash(f"Save failed: {e}", "err")
        return render(Q_FORM_HTML, title="New Question", q={})

    @app.route("/questions/<qid>/edit", methods=["GET", "POST"])
    @login_required
    def edit_question(qid):
        existing = admin.question_bank.get_question(qid)
        if not existing:
            flash("Question not found", "err")
            return redirect(url_for("questions_page"))
        if request.method == "POST":
            updates = {k: request.form.get(k) for k in [
                "text", "option_a", "option_b", "option_c", "option_d",
                "correct_answer", "explanation", "subject", "topic",
                "exam_type", "difficulty", "language",
            ]}
            yr = request.form.get("year")
            updates["year"] = int(yr) if yr and yr.isdigit() else None
            try:
                admin.update_question(qid, updates)
                flash("Saved", "ok")
                return redirect(url_for("questions_page"))
            except Exception as e:
                flash(f"Update failed: {e}", "err")
        return render(Q_FORM_HTML, title="Edit Question", q=existing.__dict__)

    @app.route("/questions/<qid>/delete", methods=["POST"])
    @login_required
    def delete_question_route(qid):
        try:
            ok = admin.delete_question(qid)
            flash("Deleted" if ok else "Not found", "ok" if ok else "err")
        except Exception as e:
            flash(f"Delete failed: {e}", "err")
        return redirect(url_for("questions_page"))

    @app.route("/questions/<qid>/flag", methods=["POST"])
    @login_required
    def flag_question_route(qid):
        reason = request.form.get("reason", "flagged via UI")
        admin.flag_question(qid, reason=reason, flagged_by="admin")
        flash("Flagged for review", "ok")
        return redirect(url_for("questions_page"))

    # ---------- bulk question upload ----------

    BULK_HTML = """
    <h1>Bulk Upload Questions</h1>
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% for cat, m in messages %}<div class="flash {{cat}}">{{m}}</div>{% endfor %}
    {% endwith %}
    <p>Accepts CSV, XLSX, or JSON. Required columns:
       <code>text, option_a, option_b, option_c, option_d, correct_answer, subject, exam_type</code>.
       Optional: <code>topic, difficulty, explanation, year, language</code>.</p>
    <form method="post" enctype="multipart/form-data">
      <input type="file" name="file" required>
      <button>Upload</button>
    </form>
    """

    @app.route("/questions/bulk", methods=["GET", "POST"])
    @login_required
    def bulk_questions_page():
        if request.method == "POST":
            f = request.files.get("file")
            if not f or not f.filename:
                flash("No file", "err")
                return redirect(url_for("bulk_questions_page"))
            staged = Path(cfg.upload_dir) / "_questions"
            staged.mkdir(parents=True, exist_ok=True)
            from werkzeug.utils import secure_filename as _sf
            dest = staged / _sf(f.filename)
            f.save(dest)
            try:
                result = admin.bulk_upload_questions(str(dest))
                flash(
                    f"Inserted {result.inserted}, skipped {result.skipped}."
                    + (f" Errors: {result.errors[:3]}" if result.errors else ""),
                    "ok" if result.inserted else "err",
                )
            except Exception as e:
                flash(f"Bulk upload failed: {e}", "err")
            return redirect(url_for("questions_page"))
        return render(BULK_HTML)

    # ---------- flagged questions ----------

    FLAGS_HTML = """
    <h1>Flagged Questions</h1>
    <table>
      <tr><th>ID</th><th>Reason</th><th>By</th><th>When</th><th>Actions</th></tr>
      {% for fl in flags %}
        <tr>
          <td>{{fl.question_id[:8]}}</td>
          <td>{{fl.reason}}</td><td>{{fl.flagged_by}}</td><td>{{fl.flagged_at}}</td>
          <td>
            <a href="{{ url_for('edit_question', qid=fl.question_id) }}">edit</a>
            <form method="post" action="{{ url_for('resolve_flag_route', qid=fl.question_id) }}" class="inline">
              <button>resolve</button>
            </form>
          </td>
        </tr>
      {% endfor %}
    </table>
    <h2>Duplicate Question Text</h2>
    <table>
      <tr><th>ID A</th><th>ID B</th></tr>
      {% for a,b in dups %}<tr><td>{{a[:8]}}</td><td>{{b[:8]}}</td></tr>{% endfor %}
    </table>
    """

    @app.route("/flags")
    @login_required
    def flags_page():
        flags = admin.list_flagged_questions()
        dups = admin.find_duplicate_questions()
        return render(FLAGS_HTML, flags=flags, dups=dups)

    @app.route("/flags/<qid>/resolve", methods=["POST"])
    @login_required
    def resolve_flag_route(qid):
        admin.resolve_flag(qid)
        flash("Resolved", "ok")
        return redirect(url_for("flags_page"))

    # ---------- analytics / report ----------

    REPORT_HTML = """
    <h1>Content Report</h1>
    <p><b>Generated:</b> {{r.generated_at}}</p>
    <p>Committed uploads: <b>{{r.total_committed_uploads}}</b> ·
       Pending: <b>{{r.pending_uploads}}</b> ·
       Flagged questions: <b>{{r.flagged_questions}}</b> ·
       Total questions: <b>{{r.question_total}}</b></p>
    <h2>Questions per exam</h2>
    <table><tr><th>Exam</th><th>Count</th></tr>
      {% for q in r.questions_per_exam %}<tr><td>{{q.exam_type}}</td><td>{{q.n}}</td></tr>{% endfor %}
    </table>
    <h2>Questions per difficulty</h2>
    <table><tr><th>Difficulty</th><th>Count</th></tr>
      {% for q in r.questions_per_difficulty %}<tr><td>{{q.difficulty}}</td><td>{{q.n}}</td></tr>{% endfor %}
    </table>
    <h2>Most accessed topics</h2>
    <table><tr><th>Topic</th><th>Subject</th><th>Hits</th></tr>
      {% for t in r.most_accessed_topics %}
        <tr><td>{{t.topic}}</td><td>{{t.subject}}</td><td>{{t.hits}}</td></tr>
      {% endfor %}
    </table>
    <h2>Hot question topics (engagement)</h2>
    <table><tr><th>Subject</th><th>Topic</th><th>Attempts</th><th>Accuracy</th></tr>
      {% for t in r.hot_question_topics %}
        <tr><td>{{t.subject}}</td><td>{{t.topic}}</td><td>{{t.attempted}}</td><td>{{t.accuracy}}%</td></tr>
      {% endfor %}
    </table>
    <h2>Content gaps (syllabus topics with no mapped chunks)</h2>
    <table><tr><th>Exam</th><th>Subject</th><th>Topic</th></tr>
      {% for g in r.content_gaps %}
        <tr><td>{{g.exam}}</td><td>{{g.subject}}</td><td>{{g.topic}}</td></tr>
      {% endfor %}
    </table>
    """

    @app.route("/report")
    @login_required
    def report_page():
        r = admin.generate_content_report()
        return render(REPORT_HTML, r=r)

    @app.route("/api/report.json")
    @login_required
    def report_json():
        return jsonify(admin.generate_content_report())

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=settings.admin.web_host)
    parser.add_argument("--port", type=int, default=settings.admin.web_port)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    app = create_app()
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
