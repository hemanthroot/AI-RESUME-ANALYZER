from flask import Flask, render_template, request, jsonify, session, redirect
from dotenv import load_dotenv
import os
import google.generativeai as genai
from PyPDF2 import PdfReader
import json
import re
import docx
from supabase import create_client, Client
import socket
import uuid
import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash

# Load environment variables
load_dotenv()

def get_db_path():
    if os.environ.get("VERCEL") == "1":
        return "/tmp/admins.db"
    return "admins.db"

def init_admin_db():
    conn = sqlite3.connect(get_db_path())
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS admins
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  username TEXT UNIQUE NOT NULL,
                  password_hash TEXT NOT NULL)''')
    conn.commit()
    conn.close()

init_admin_db()

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = "uploads"
app.secret_key = os.getenv("FLASK_SECRET_KEY", "resume-matcher-secret-key-2024")
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

def get_or_create_session_id():
    """Returns a persistent UUID for the current browser session (creates one if needed)."""
    if 'user_session_id' not in session:
        session['user_session_id'] = str(uuid.uuid4())
        session.permanent = True
    return session['user_session_id']

def get_local_ip():
    """Returns the machine's LAN IP address for shareable links."""
    try:
        # Connect to external host to discover which local IP is used
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

@app.context_processor
def inject_server_url():
    """Injects the server's base URL into all templates for share links."""
    ip = get_local_ip()
    return {"server_base_url": f"http://{ip}:5000"}

# Supabase setup
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print("Supabase Init Error:", e)

# Gemini AI setup
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
model = genai.GenerativeModel("models/gemini-flash-latest")

# Function to extract text from PDF
def extract_text_from_pdf(pdf_path):
    reader = PdfReader(pdf_path)
    text = ""
    for page in reader.pages:
        page_text = page.extract_text()
        if page_text:
            text += page_text + "\n"
    return text

def extract_text_from_docx(docx_path):
    try:
        doc = docx.Document(docx_path)
        return "\n".join([para.text for para in doc.paragraphs])
    except Exception as e:
        print("Error reading word document:", e)
        return ""

def get_device_type(user_agent):
    user_agent = user_agent.lower()
    if 'mobi' in user_agent or 'android' in user_agent or 'iphone' in user_agent:
        return 'Mobile'
    return 'Desktop'

@app.route("/", methods=["GET", "POST"])
def index():
    session_id = get_or_create_session_id()
    
    # Capture referral link if present
    ref = request.args.get('ref')
    if ref and ref != session_id:
        session['referred_by'] = ref
        
    result = None

    if request.method == "POST":
        job_description = request.form.get("job_description")
        resume = request.files.get("resume")
        device_type = get_device_type(request.headers.get('User-Agent', ''))

        if resume and (resume.filename.endswith(".pdf") or resume.filename.endswith(".docx") or resume.filename.endswith(".doc")):
            filepath = os.path.join(app.config["UPLOAD_FOLDER"], resume.filename)
            resume.save(filepath)

            if resume.filename.endswith(".pdf"):
                resume_text = extract_text_from_pdf(filepath)
            else:
                resume_text = extract_text_from_docx(filepath)

            # Prompt for Gemini AI
            prompt = f"""
You are an expert ATS (Applicant Tracking System) and AI HR Recruiter.
Evaluate the candidate's resume strictly against the provided Job Description.

Extract the candidate's personal information from their resume.

Return ONLY valid JSON with the following formatting:
{{
  "candidate_name": "string (Extract candidate's full name from resume, or 'Unknown')",
  "email": "string (Extract candidate's email, or 'Not Provided')",
  "phone": "string (Extract candidate's phone number, or 'Not Provided')",
  "ats": number (match score out of 100),
  "summary": "string (brief executive summary of the candidate's fit)",
  "matched": ["string"] (skills and requirements met),
  "missing": ["string"] (skills and requirements missing),
  "recommendation": "string (e.g., 'Strong Match', 'Interview Recommended', 'Not Recommended')",
  "questions": ["string"] (list of 3 to 5 tailored interview questions based on the job description and candidate's experience)
}}

Job Description:
{job_description}

Resume Text:
{resume_text}
"""

            response = model.generate_content(prompt)

            # Try to parse JSON from Gemini
            try:
                clean_text = response.text.strip()
                clean_text = re.sub(r"```json|```", "", clean_text)
                result = json.loads(clean_text)
                
                # Save to Supabase specifically if it's initialized
                if supabase:
                    try:
                        session_id = get_or_create_session_id()
                        saved_candidate_id = None
                        
                        # 1. Save to Candidates if recommended (do this first to get candidate_id)
                        rec = result.get("recommendation", "")
                        if "strong match" in rec.lower() or "interview recommended" in rec.lower():
                            # Assign to referrer if referred, otherwise self
                            candidate_owner = session.get('referred_by', session_id)
                            cand_res = supabase.table('candidates').insert({
                                "name": result.get("candidate_name", "Unknown"),
                                "email": result.get("email", "Not Provided"),
                                "phone": result.get("phone", "Not Provided"),
                                "ats_score": result.get("ats", 0),
                                "recommendation": rec,
                                "session_id": candidate_owner
                            }).execute()
                            if cand_res.data:
                                saved_candidate_id = cand_res.data[0]["id"]
                        
                        # 2. Save History with session_id and optional candidate_id
                        supabase.table('history').insert({
                            "filename": resume.filename,
                            "job_description": job_description[:200] + "..." if len(job_description) > 200 else job_description,
                            "ats_score": result.get("ats", 0),
                            "device_type": device_type,
                            "session_id": session_id,
                            "candidate_id": saved_candidate_id
                        }).execute()
                    except Exception as db_err:
                        print("Supabase Insert Error:", db_err)

            except Exception as e:
                print("JSON Parse Error:", e)
                print("Raw Response:", response.text)
                result = {
                    "ats": 0,
                    "summary": "Error parsing AI response.",
                    "matched": [],
                    "missing": [],
                    "recommendation": "Manual Review Required",
                    "questions": [],
                    "candidate_name": "Unknown",
                    "email": "Unknown",
                    "phone": "Unknown"
                }
                
            return render_template("result.html", result=result)

    return render_template("index.html")

@app.route("/history")
def history():
    history_data = []
    if supabase:
        try:
            session_id = get_or_create_session_id()
            if session.get('is_admin'):
                res = supabase.table('history').select('*').order('id', desc=True).execute()
            else:
                res = supabase.table('history').select('*').eq('session_id', session_id).order('id', desc=True).execute()
            history_data = res.data
        except Exception as e:
            print("Error fetching history:", e)
    return render_template("history.html", history=history_data)

@app.route("/candidates")
def candidates():
    candidates_data = []
    if supabase:
        try:
            session_id = get_or_create_session_id()
            # Fetch candidates (all for admin, session-only for normal users)
            if session.get('is_admin'):
                res = supabase.table('candidates').select('*').order('id', desc=True).execute()
            else:
                res = supabase.table('candidates').select('*').eq('session_id', session_id).order('id', desc=True).execute()
            raw_candidates = res.data
            
            seen_candidates = set()
            candidates_data = []
            for cand in raw_candidates:
                name = str(cand.get('candidate_name', '')).strip().lower()
                email = str(cand.get('candidate_email', '')).strip().lower()
                identifier = (name, email)
                if identifier not in seen_candidates:
                    seen_candidates.add(identifier)
                    candidates_data.append(cand)
            
            # Fetch views and map them to candidates
            try:
                views_res = supabase.table('candidate_views').select('*').order('viewed_at', desc=True).execute()
                views_data = views_res.data
                
                # Group views by candidate_id
                views_by_candidate = {}
                for view in views_data:
                    c_id = view['candidate_id']
                    if c_id not in views_by_candidate:
                        views_by_candidate[c_id] = []
                    views_by_candidate[c_id].append({
                        'name': view['viewer_name'],
                        'device': view.get('device_type', 'Desktop')
                    })
                
                # Attach to candidate data
                for cand in candidates_data:
                    cand['views'] = views_by_candidate.get(cand['id'], [])
                    
            except Exception as v_err:
                print("Error fetching views:", v_err)
                for cand in candidates_data:
                    cand['views'] = []
                    
        except Exception as e:
            print("Error fetching candidates:", e)
    return render_template("candidates.html", candidates=candidates_data)

@app.route("/shared/candidate/<int:cand_id>", methods=['GET', 'POST'])
def shared_candidate(cand_id):
    if request.method == 'POST':
        viewer_name = request.form.get('viewer_name')
        if not viewer_name:
            return "Please enter a your name.", 400
            
        if supabase:
            try:
                # Detect viewer's device type
                viewer_device = get_device_type(request.headers.get('User-Agent', ''))
                # Save the view with device info
                supabase.table('candidate_views').insert({
                    'candidate_id': cand_id,
                    'viewer_name': viewer_name,
                    'device_type': viewer_device
                }).execute()
            except Exception as e:
                print(f"Error saving view for candidate {cand_id}:", e)
                
        # After saving, fetch the candidate and render the profile
        candidate_data = None
        if supabase:
            try:
                res = supabase.table('candidates').select('*').eq('id', cand_id).execute()
                if res.data and len(res.data) > 0:
                    candidate_data = res.data[0]
            except Exception as e:
                print(f"Error fetching candidate {cand_id}:", e)
                
        if candidate_data:
            return render_template("shared_candidate.html", candidate=candidate_data)
        else:
            return "Candidate not found.", 404
            
    # GET request - Show the "Enter Name" page
    return render_template("enter_name.html")

@app.route("/jobs")
def jobs():
    job_recommendations = []
    error = None
    
    if supabase:
        try:
            # Fetch the most recent 5 job descriptions from history
            res = supabase.table('history').select('job_description').order('id', desc=True).limit(5).execute()
            history_data = res.data
            
            if history_data:
                job_descriptions_text = "\n\n---\n\n".join(
                    [f"Evaluation {i+1}: {item['job_description']}"
                     for i, item in enumerate(history_data)]
                )
                
                prompt = f"""
You are a career advisor AI. Based on the following job descriptions that were recently evaluated, 
recommend 6 relevant job roles that the candidates could be well-suited for.

For each recommendation, provide:
1. A clear job title
2. A brief description (1-2 sentences)
3. A list of 3-4 key skills required
4. Approximate salary range (use USD)
5. A suitability badge: one of "High Match", "Good Fit", or "Worth Exploring"

Return ONLY valid JSON array using this exact format:
[
  {{
    "title": "Job Title",
    "description": "Brief description of the role.",
    "skills": ["Skill 1", "Skill 2", "Skill 3"],
    "salary": "$80,000 - $120,000",
    "badge": "High Match"
  }}
]

Job Descriptions from recent evaluations:
{job_descriptions_text}
"""
                response = model.generate_content(prompt)
                clean_text = response.text.strip()
                clean_text = re.sub(r"```json|```", "", clean_text)
                job_recommendations = json.loads(clean_text)
        except Exception as e:
            print("Error fetching job recommendations:", e)
            error = "Could not load recommendations. Please analyze a resume first."
    else:
        error = "Supabase is not configured."
        
    return render_template("jobs.html", jobs=job_recommendations, error=error)

@app.route("/admin/register", methods=["GET", "POST"])
def admin_register():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        
        if not username or not password:
            error = "Username and password are required."
        elif password != confirm_password:
            error = "Passwords do not match."
        else:
            hashed_pw = generate_password_hash(password)
            try:
                conn = sqlite3.connect('admins.db')
                c = conn.cursor()
                c.execute("INSERT INTO admins (username, password_hash) VALUES (?, ?)", (username, hashed_pw))
                conn.commit()
                conn.close()
                from flask import url_for
                return redirect(url_for('admin_login', success="Registration successful! You can now log in."))
            except sqlite3.IntegrityError:
                error = "Username already exists."
            except Exception as e:
                error = "An error occurred during registration."
                print("DB Error:", e)
                
    return render_template("admin_register.html", error=error)

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    success = request.args.get('success')
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        
        conn = sqlite3.connect('admins.db')
        c = conn.cursor()
        c.execute("SELECT password_hash FROM admins WHERE username=?", (username,))
        row = c.fetchone()
        conn.close()
        
        if row and check_password_hash(row[0], password):
            session['is_admin'] = True
            session['admin_username'] = username
            return redirect("/admin")
        else:
            error = "Invalid username or password."
            
    return render_template("admin_login.html", error=error)

@app.route("/admin/logout")
def admin_logout():
    session.pop('is_admin', None)
    return redirect("/admin/login")

@app.route("/admin")
def admin_dashboard():
    if not session.get('is_admin'):
        return redirect("/admin/login")
    
    all_history = []
    all_candidates = []
    all_users = []
    stats = {"total_history": 0, "total_candidates": 0, "avg_score": 0, "unique_sessions": 0}
    
    if supabase:
        try:
            h_res = supabase.table('history').select('*').order('id', desc=True).execute()
            all_history = h_res.data
            stats["total_history"] = len(all_history)
            if all_history:
                stats["avg_score"] = round(sum(h.get('ats_score', 0) for h in all_history) / len(all_history), 1)
                
                user_dict = {}
                for h in all_history:
                    sid = h.get('session_id')
                    if not sid: continue
                    if sid not in user_dict:
                        user_dict[sid] = {
                            "session_id": sid,
                            "scans": 0,
                            "avg_score": 0,
                            "devices": set(),
                            "last_active": h.get('created_at', 'Unknown')
                        }
                    
                    user_dict[sid]["scans"] += 1
                    user_dict[sid]["avg_score"] += h.get('ats_score', 0)
                    if h.get('device_type'):
                        user_dict[sid]["devices"].add(h['device_type'])
                
                all_users = list(user_dict.values())
                for u in all_users:
                    if u["scans"] > 0:
                        u["avg_score"] = round(u["avg_score"] / u["scans"], 1)
                    u["devices"] = ", ".join(u["devices"])
                
                all_users.sort(key=lambda x: x["scans"], reverse=True)
                stats["unique_sessions"] = len(all_users)
        except Exception as e:
            print("Admin history fetch error:", e)
        
        try:
            c_res = supabase.table('candidates').select('*').order('id', desc=True).execute()
            raw_candidates = c_res.data
            
            seen_cands = set()
            all_candidates = []
            for cand in raw_candidates:
                name = str(cand.get('candidate_name', '')).strip().lower()
                email = str(cand.get('candidate_email', '')).strip().lower()
                identifier = (name, email)
                if identifier not in seen_cands:
                    seen_cands.add(identifier)
                    all_candidates.append(cand)
                    
            stats["total_candidates"] = len(all_candidates)
        except Exception as e:
            print("Admin candidates fetch error:", e)
    
    return render_template("admin_dashboard.html", history=all_history, candidates=all_candidates, users=all_users, stats=stats)

@app.route("/settings", methods=["GET", "POST"])
def settings():
    message = ""
    if request.method == "POST":
        action = request.form.get("action")
        if action == "clear_db":
            if supabase:
                try:
                    session_id = get_or_create_session_id()
                    # Only clear the current user's data
                    supabase.table('history').delete().eq('session_id', session_id).execute()
                    supabase.table('candidates').delete().eq('session_id', session_id).execute()
                    message = "Your data has been cleared successfully!"
                except Exception as e:
                    print("Error clearing DB:", e)
                    message = "Error clearing data. Please check server logs."
            else:
                message = "Supabase is not configured. Add credentials in .env."
                
    return render_template("settings.html", message=message, supabase_configured=bool(supabase))

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0")