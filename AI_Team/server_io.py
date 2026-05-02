"""File and project bundle helpers for generated builds."""

import ast
import datetime
import html
import json
import os
import re
import sys
import zipfile

from server_config import (
    BUILDS_DIR,
    LATEST_OUTPUT_FILE,
    LATEST_PROJECT_FILE,
    LATEST_ZIP_FILE,
)


_FLASK_ROUTE_RE = re.compile(r"@app\.route\(\s*['\"]([^'\"]+)['\"]")
_FLASK_ROUTE_METHODS_RE = re.compile(
    r"@app\.route\(\s*['\"]([^'\"]+)['\"](?:[^)]*?methods\s*=\s*\[([^\]]*)\])?\s*\)",
    re.DOTALL,
)
# Matches any render_template() call — no longer restricted to index.*
_RENDER_TEMPLATE_RE = re.compile(r"render_template\(\s*['\"]")
_RENDER_TEMPLATE_NAME_RE = re.compile(r"render_template\(\s*['\"]([^'\"]+)['\"]")
# Captures template name + all kwarg names passed on the same call
_RENDER_CALL_RE = re.compile(
    r"render_template\(\s*['\"]([^'\"]+)['\"]([^)]*)\)", re.DOTALL
)
_KWARG_NAME_RE = re.compile(r"\b(\w+)\s*=")

# A template matches as a legacy fallback if it contains ANY of these strings.
_LEGACY_FALLBACK_MARKERS = (
    "generated app is running",
    "fallback page",
    "this template was auto-generated",
    "your app is running",
)


_BASIC_INDEX_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Generated App</title>
</head>
<body>
    <h1>Generated App Is Running</h1>
    <p>This page was created automatically because <code>templates/index.html</code> was missing.</p>
</body>
</html>
"""

# Smart interactive template — substitution tokens replaced at generation time.
# APP_TITLE, API_LIST_PATH, API_ADD_PATH, MAIN_FIELD, ADD_PLACEHOLDER
_SMART_UI_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>APP_TITLE</title>
<style>
*,*::before,*::after{box-sizing:border-box}
body{margin:0;font-family:"Segoe UI",system-ui,sans-serif;background:linear-gradient(135deg,#0f172a 0%,#1e293b 100%);min-height:100vh;color:#f1f5f9}
.wrap{max-width:720px;margin:0 auto;padding:40px 20px}
h1{margin:0 0 6px;font-size:2rem;color:#f8fafc}
.sub{margin:0 0 28px;color:#94a3b8;font-size:.95rem}
.card{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);border-radius:16px;padding:24px;margin-bottom:20px}
.row{display:flex;gap:10px}
input{flex:1;border:1px solid rgba(255,255,255,.15);border-radius:10px;padding:12px 16px;font-size:15px;background:rgba(255,255,255,.07);color:#f1f5f9;outline:none}
input:focus{border-color:#6366f1}
input::placeholder{color:#64748b}
button{border:0;border-radius:10px;padding:12px 20px;font-size:15px;font-weight:600;cursor:pointer;transition:opacity .15s}
button:hover{opacity:.85}
.btn-primary{background:#6366f1;color:#fff}
.btn-danger{background:#ef4444;color:#fff;padding:6px 12px;font-size:13px}
.btn-success{background:#22c55e;color:#fff;padding:6px 12px;font-size:13px}
ul{list-style:none;margin:0;padding:0}
li{display:flex;align-items:center;gap:10px;padding:12px 16px;border-radius:10px;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);margin-bottom:8px}
.item-text{flex:1;font-size:15px}
.done-text{text-decoration:line-through;color:#64748b}
.empty{color:#64748b;text-align:center;padding:24px}
.status{margin-top:14px;min-height:20px;font-size:14px;color:#6366f1}
</style>
</head>
<body>
<main class="wrap">
  <h1>APP_TITLE</h1>
  <p class="sub" id="counter">0 items</p>
  <div class="card">
    <div class="row">
      <input id="mainInput" type="text" placeholder="ADD_PLACEHOLDER" />
      <button class="btn-primary" onclick="addItem()">Add</button>
    </div>
  </div>
  <div class="card">
    <ul id="itemList"><li class="empty">Loading\u2026</li></ul>
    <div id="status" class="status"></div>
  </div>
</main>
<script>
const LIST_URL='API_LIST_PATH';
const ADD_URL='API_ADD_PATH';
const FIELD='MAIN_FIELD';
function setStatus(msg,err){const el=document.getElementById('status');el.textContent=msg;el.style.color=err?'#ef4444':'#6366f1';}
async function api(url,opts){const r=await fetch(url,{headers:{'Content-Type':'application/json'},...(opts||{})});const t=await r.text();let d;try{d=t?JSON.parse(t):{};}catch{d={_raw:t};}if(!r.ok)throw new Error(d.error||d.message||d._raw||('HTTP '+r.status));return d;}
function getText(item){if(item==null)return'';if(typeof item!=='object')return String(item);return item.task||item.title||item.name||item.text||item.content||item.message||item.description||item.value||JSON.stringify(item);}
function isDone(item){return typeof item==='object'&&item&&!!(item.done||item.completed||item.finished||item.checked);}
function renderItems(data){
  const ul=document.getElementById('itemList');ul.innerHTML='';
  let entries=Array.isArray(data)?data.map((v,i)=>[String(i),v]):Object.entries(data||{});
  document.getElementById('counter').textContent=entries.length+' item'+(entries.length===1?'':'s');
  if(!entries.length){ul.innerHTML='<li class="empty">Nothing here yet \u2014 add your first item!</li>';return;}
  for(const[id,item]of entries){
    const li=document.createElement('li');
    const done=isDone(item);
    const span=document.createElement('span');span.className='item-text'+(done?' done-text':'');span.textContent=getText(item);
    const t=document.createElement('button');t.className='btn-success';t.textContent=done?'Undo':'Done';t.onclick=()=>toggleItem(id,done);
    const d=document.createElement('button');d.className='btn-danger';d.textContent='Delete';d.onclick=()=>deleteItem(id);
    li.append(span,t,d);ul.appendChild(li);
  }
}
async function loadItems(){try{renderItems(await api(LIST_URL));setStatus('');}catch(e){setStatus('Could not load: '+e.message,true);document.getElementById('itemList').innerHTML='<li class="empty">Load failed.</li>';}}
async function addItem(){
  const inp=document.getElementById('mainInput');const val=inp.value.trim();
  if(!val){setStatus('Please enter something first.',true);return;}
  try{await api(ADD_URL,{method:'POST',body:JSON.stringify({[FIELD]:val,completed:false})});inp.value='';await loadItems();setStatus('Added!');}
  catch(e){setStatus('Add failed: '+e.message,true);}
}
async function toggleItem(id,wasDone){try{await api(LIST_URL+'/'+id,{method:'PUT',body:JSON.stringify({completed:!wasDone})});await loadItems();}catch(e){setStatus('Update failed: '+e.message,true);}}
async function deleteItem(id){try{await api(LIST_URL+'/'+id,{method:'DELETE'});await loadItems();}catch(e){setStatus('Delete failed: '+e.message,true);}}
document.getElementById('mainInput').addEventListener('keydown',e=>{if(e.key==='Enter')addItem();});
loadItems();
</script>
</body>
</html>
"""


# ── Per-template type generators ──────────────────────────────────────────────

_CSS_BASE = (
    "*,*::before,*::after{box-sizing:border-box}"
    "body{margin:0;font-family:'Segoe UI',system-ui,sans-serif;"
    "background:linear-gradient(135deg,#0f172a 0%,#1e293b 100%);"
    "min-height:100vh;color:#f1f5f9}"
    ".wrap{max-width:780px;margin:0 auto;padding:40px 20px}"
    "h1{margin:0 0 6px;font-size:2rem;color:#f8fafc}"
    ".sub{margin:0 0 24px;color:#94a3b8;font-size:.95rem}"
    ".card{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);"
    "border-radius:16px;padding:24px;margin-bottom:20px}"
    "input,textarea,select{width:100%;border:1px solid rgba(255,255,255,.15);"
    "border-radius:10px;padding:12px 16px;font-size:15px;"
    "background:rgba(255,255,255,.07);color:#f1f5f9;outline:none;margin-bottom:12px}"
    "input:focus,textarea:focus{border-color:#6366f1}"
    "input::placeholder,textarea::placeholder{color:#64748b}"
    "button{border:0;border-radius:10px;padding:12px 20px;font-size:15px;"
    "font-weight:600;cursor:pointer;transition:opacity .15s;width:100%}"
    "button:hover{opacity:.85}"
    ".btn{background:#6366f1;color:#fff}"
    ".btn-sm{width:auto;padding:6px 14px;font-size:13px}"
    ".btn-danger{background:#ef4444;color:#fff}"
    ".btn-success{background:#22c55e;color:#fff}"
    "a{color:#6366f1;text-decoration:none}"
    ".status{min-height:18px;font-size:13px;color:#6366f1;margin-top:10px}"
    ".error{color:#ef4444}"
)


def _tpl(title, body_html, extra_css="", extra_js=""):
    """Wrap body content in the standard dark-theme page shell."""
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n"
        f"<title>{html.escape(title)}</title>\n"
        f"<style>{_CSS_BASE}{extra_css}</style>\n"
        "</head>\n<body>\n"
        f"{body_html}\n"
        + (f"<script>\n{extra_js}\n</script>\n" if extra_js else "")
        + "</body>\n</html>\n"
    )


def _gen_base_template():
    """Jinja2 base/layout template with block slots."""
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n"
        "<title>{% block title %}App{% endblock %}</title>\n"
        "<style>" + _CSS_BASE + "</style>\n"
        "{% block head %}{% endblock %}\n"
        "</head>\n<body>\n"
        "<nav style=\"background:rgba(255,255,255,.06);padding:14px 28px;"
        "display:flex;gap:20px;align-items:center;border-bottom:1px solid rgba(255,255,255,.08)\">\n"
        "  <a href=\"/\" style=\"color:#6366f1;font-weight:700;font-size:1.1rem\">App</a>\n"
        "  {% block nav %}{% endblock %}\n"
        "</nav>\n"
        "<main class=\"wrap\">\n"
        "{% block content %}\n"
        "<p style=\"color:#94a3b8\">No content yet.</p>\n"
        "{% endblock %}\n"
        "</main>\n"
        "{% block scripts %}{% endblock %}\n"
        "</body>\n</html>\n"
    )


def _gen_login_template(action="/login"):
    return _tpl(
        "Sign In",
        """<div class="wrap" style="max-width:420px">
<div class="card">
  <h1 style="text-align:center;margin-bottom:4px">Sign In</h1>
  <p class="sub" style="text-align:center">Enter your credentials to continue</p>
  <form method="POST" action=\"""" + html.escape(action) + """\" id="loginForm">
    <input name="username" id="username" placeholder="Username or Email" required autocomplete="username">
    <input name="password" id="password" type="password" placeholder="Password" required autocomplete="current-password">
    <button class="btn" type="submit">Sign In</button>
    <div id="status" class="status"></div>
  </form>
  <p style="text-align:center;margin-top:14px;color:#64748b;font-size:14px">
    Don&#39;t have an account? <a href="/register">Register</a>
  </p>
</div></div>""",
        extra_js="""
document.getElementById('loginForm').addEventListener('submit', async e => {
  e.preventDefault();
  const st = document.getElementById('status');
  st.textContent = 'Signing in\u2026'; st.className = 'status';
  const body = {
    username: document.getElementById('username').value,
    password: document.getElementById('password').value,
  };
  try {
    const r = await fetch('/login', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    const d = await r.json().catch(() => ({}));
    if (r.ok) { window.location = d.redirect || '/'; }
    else { st.textContent = d.error || d.message || 'Login failed'; st.className = 'status error'; }
  } catch(e) { st.textContent = 'Network error'; st.className = 'status error'; }
});""",
    )


def _gen_register_template(action="/register"):
    return _tpl(
        "Register",
        """<div class="wrap" style="max-width:420px">
<div class="card">
  <h1 style="text-align:center;margin-bottom:4px">Create Account</h1>
  <p class="sub" style="text-align:center">Sign up to get started</p>
  <form id="regForm" method="POST" action=\"""" + html.escape(action) + """\" >
    <input name="username" id="username" placeholder="Username" required>
    <input name="email"    id="email"    type="email" placeholder="Email (optional)">
    <input name="password" id="password" type="password" placeholder="Password" required>
    <button class="btn" type="submit">Create Account</button>
    <div id="status" class="status"></div>
  </form>
  <p style="text-align:center;margin-top:14px;color:#64748b;font-size:14px">
    Already have an account? <a href="/login">Sign in</a>
  </p>
</div></div>""",
        extra_js="""
document.getElementById('regForm').addEventListener('submit', async e => {
  e.preventDefault();
  const st = document.getElementById('status');
  st.textContent = 'Creating account\u2026'; st.className = 'status';
  const body = {
    username: document.getElementById('username').value,
    email:    document.getElementById('email').value,
    password: document.getElementById('password').value,
  };
  try {
    const r = await fetch('/register', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    const d = await r.json().catch(() => ({}));
    if (r.ok) { window.location = d.redirect || '/login'; }
    else { st.textContent = d.error || d.message || 'Registration failed'; st.className = 'status error'; }
  } catch(e) { st.textContent = 'Network error'; st.className = 'status error'; }
});""",
    )


def _gen_calculator_template(api_path="/api/calculate"):
    return _tpl(
        "Calculator",
        """<div class="wrap" style="max-width:380px">
<div class="card">
  <h1 style="text-align:center;margin-bottom:16px">Calculator</h1>
  <div id="display" style="background:rgba(0,0,0,.4);border-radius:10px;padding:16px 20px;
    font-size:2rem;text-align:right;color:#f8fafc;margin-bottom:12px;min-height:60px;
    word-break:break-all;letter-spacing:1px">0</div>
  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px">
    <button class="btn-danger btn-sm" onclick="calcClear()" style="width:100%;grid-column:span 2">C</button>
    <button style="background:rgba(255,255,255,.1);color:#f1f5f9;border:0;border-radius:8px;
      padding:14px;font-size:18px;cursor:pointer" onclick="calcOp('%')">%</button>
    <button style="background:#6366f1;color:#fff;border:0;border-radius:8px;
      padding:14px;font-size:18px;cursor:pointer" onclick="calcOp('/')">&#247;</button>""" +
        "".join([
            f'<button style="background:rgba(255,255,255,.08);color:#f1f5f9;border:0;'
            f'border-radius:8px;padding:14px;font-size:18px;cursor:pointer" '
            f'onclick="calcDigit(\'{d}\')">{d}</button>\n    '
            f'<button style="background:rgba(255,255,255,.08);color:#f1f5f9;border:0;'
            f'border-radius:8px;padding:14px;font-size:18px;cursor:pointer" '
            f'onclick="calcDigit(\'{e}\')">{e}</button>\n    '
            f'<button style="background:rgba(255,255,255,.08);color:#f1f5f9;border:0;'
            f'border-radius:8px;padding:14px;font-size:18px;cursor:pointer" '
            f'onclick="calcDigit(\'{f}\')">{f}</button>\n    '
            f'<button style="background:#6366f1;color:#fff;border:0;border-radius:8px;'
            f'padding:14px;font-size:18px;cursor:pointer" onclick="calcOp(\'{op}\')">{op_html}</button>\n    '
            for d, e, f, op, op_html in [
                ("7","8","9","*","&times;"),
                ("4","5","6","-","&minus;"),
                ("1","2","3","+","+"),
            ]
        ]) +
        """<button style="background:rgba(255,255,255,.08);color:#f1f5f9;border:0;
      border-radius:8px;padding:14px;font-size:18px;cursor:pointer;grid-column:span 2"
      onclick="calcDigit('0')">0</button>
    <button style="background:rgba(255,255,255,.08);color:#f1f5f9;border:0;
      border-radius:8px;padding:14px;font-size:18px;cursor:pointer" onclick="calcDigit('.')">.</button>
    <button style="background:#22c55e;color:#fff;border:0;border-radius:8px;
      padding:14px;font-size:20px;font-weight:700;cursor:pointer" onclick="calcEquals()">=</button>
  </div>
</div></div>""",
        extra_js=f"""
let _expr = '';
const disp = document.getElementById('display');
function calcDigit(d){{ _expr += d; disp.textContent = _expr || '0'; }}
function calcOp(op){{ _expr += op; disp.textContent = _expr; }}
function calcClear(){{ _expr = ''; disp.textContent = '0'; }}
async function calcEquals() {{
  if (!_expr) return;
  const expr = _expr;
  try {{
    const r = await fetch('{html.escape(api_path)}', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{expression: expr, a: parseFloat(expr.split(/[+\\-*\\/]/)[0]),
        b: parseFloat(expr.split(/[+\\-*\\/]/).pop()),
        op: (expr.match(/[+\\-*\\/]/) || [''])[0]}})
    }});
    const d = await r.json();
    const val = d.result ?? d.answer ?? d.value ?? d.output;
    if (val !== undefined) {{ disp.textContent = val; _expr = String(val); }}
    else {{ throw new Error('no result'); }}
  }} catch(_) {{
    try {{ const v = Function('return ' + expr)(); disp.textContent = v; _expr = String(v); }}
    catch {{ disp.textContent = 'Error'; _expr = ''; }}
  }}
}}
document.addEventListener('keydown', e => {{
  if (e.key >= '0' && e.key <= '9') calcDigit(e.key);
  else if (['+','-','*','/','%'].includes(e.key)) calcOp(e.key);
  else if (e.key === 'Enter' || e.key === '=') calcEquals();
  else if (e.key === 'Backspace') {{ _expr = _expr.slice(0,-1); disp.textContent = _expr || '0'; }}
  else if (e.key === 'Escape') calcClear();
}});""",
    )


def _gen_blog_template(list_url="/api/posts", add_url=None, title_field="title", body_field="content"):
    add_url = add_url or list_url
    return _tpl(
        "Blog",
        f"""<div class="wrap">
<h1>Blog</h1>
<p class="sub" id="postCount">Loading posts\u2026</p>
<div class="card">
  <h2 style="margin:0 0 14px;font-size:1.1rem;color:#94a3b8">New Post</h2>
  <input id="postTitle" placeholder="Title">
  <textarea id="postBody" placeholder="Write your post here\u2026" rows="4"
    style="resize:vertical"></textarea>
  <button class="btn" onclick="addPost()">Publish</button>
  <div id="addStatus" class="status"></div>
</div>
<div id="postList"></div>
</div>""",
        extra_css="""
.post-card{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);
  border-radius:12px;padding:20px;margin-bottom:14px}
.post-title{font-size:1.2rem;font-weight:600;color:#f8fafc;margin:0 0 8px}
.post-body{color:#94a3b8;font-size:14px;line-height:1.6;margin:0 0 10px}
.post-meta{font-size:12px;color:#475569}
.empty{color:#64748b;text-align:center;padding:32px}""",
        extra_js=f"""
const LIST_URL = '{html.escape(list_url)}';
const ADD_URL  = '{html.escape(add_url)}';
const TF = '{html.escape(title_field)}';
const BF = '{html.escape(body_field)}';

function getStr(obj, ...keys) {{
  for (const k of keys) if (obj && obj[k] !== undefined) return String(obj[k]);
  return JSON.stringify(obj);
}}

async function loadPosts() {{
  try {{
    const r = await fetch(LIST_URL);
    const data = await r.json();
    const posts = Array.isArray(data) ? data : (data.posts || data.items || data.results || []);
    document.getElementById('postCount').textContent = posts.length + ' post' + (posts.length===1?'':'s');
    const el = document.getElementById('postList');
    if (!posts.length) {{ el.innerHTML = '<p class="empty">No posts yet. Write the first one!</p>'; return; }}
    el.innerHTML = posts.map((p,i) => `
      <div class="post-card">
        <div class="post-title">${{getStr(p, TF, 'title','name','subject')}}</div>
        <div class="post-body">${{getStr(p, BF, 'content','body','text','description')}}</div>
        <div class="post-meta">Post #${{i+1}}</div>
      </div>`).join('');
  }} catch(e) {{ document.getElementById('postList').innerHTML = '<p class="empty">Failed to load posts.</p>'; }}
}}

async function addPost() {{
  const title = document.getElementById('postTitle').value.trim();
  const body  = document.getElementById('postBody').value.trim();
  const st    = document.getElementById('addStatus');
  if (!title) {{ st.textContent = 'Title is required'; st.className = 'status error'; return; }}
  st.textContent = 'Publishing\u2026'; st.className = 'status';
  try {{
    const r = await fetch(ADD_URL, {{method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{[TF]:title,[BF]:body}})}});
    if (!r.ok) throw new Error(await r.text());
    document.getElementById('postTitle').value = '';
    document.getElementById('postBody').value  = '';
    st.textContent = 'Published!'; st.className = 'status';
    await loadPosts();
  }} catch(e) {{ st.textContent = 'Failed: '+e.message; st.className = 'status error'; }}
}}

loadPosts();""",
    )


def _gen_error_template(code="Error"):
    return _tpl(
        f"{code}",
        f"""<div class="wrap" style="text-align:center;padding-top:80px">
<div style="font-size:5rem;font-weight:900;color:rgba(239,68,68,.3)">{html.escape(str(code))}</div>
<h1 style="margin:8px 0 10px">Something went wrong</h1>
<p style="color:#94a3b8;margin-bottom:28px">
  {{"Page not found. The URL may be wrong or the page may have moved."
    if str(code) == "404" else
   "An internal error occurred. Please try again later."}}
</p>
<a href="/" style="background:#6366f1;color:#fff;padding:12px 28px;
  border-radius:10px;font-weight:600">Go Home</a>
</div>""",
    )


def _gen_dashboard_template(vars_passed):
    stat_items = " ".join(
        f'<div class="stat-card"><div class="stat-val" id="stat_{v}">-</div>'
        f'<div class="stat-label">{v.replace("_"," ").title()}</div></div>'
        for v in (vars_passed[:4] if vars_passed else ["total", "active", "today"])
    )
    return _tpl(
        "Dashboard",
        f"""<div class="wrap">
<h1>Dashboard</h1>
<p class="sub" id="lastUpdated">Fetching data\u2026</p>
<div class="stat-grid">{stat_items}</div>
<div class="card" style="margin-top:20px">
  <h2 style="margin:0 0 14px;font-size:1rem;color:#94a3b8">Recent Activity</h2>
  <ul id="activityList" style="list-style:none;margin:0;padding:0;color:#64748b">
    <li>Loading\u2026</li>
  </ul>
</div>
</div>""",
        extra_css="""
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;margin-bottom:20px}
.stat-card{background:rgba(99,102,241,.12);border:1px solid rgba(99,102,241,.25);
  border-radius:14px;padding:22px 18px;text-align:center}
.stat-val{font-size:2.2rem;font-weight:700;color:#6366f1}
.stat-label{font-size:13px;color:#94a3b8;margin-top:4px;text-transform:uppercase;letter-spacing:.8px}""",
        extra_js="""
async function loadDashboard() {
  try {
    const r = await fetch('/api/stats').catch(() => fetch('/api/dashboard').catch(() => null));
    if (r && r.ok) {
      const d = await r.json();
      document.querySelectorAll('[id^="stat_"]').forEach(el => {
        const key = el.id.replace('stat_','');
        if (d[key] !== undefined) el.textContent = d[key];
      });
    }
    const r2 = await fetch('/api/activity').catch(() => null);
    if (r2 && r2.ok) {
      const items = await r2.json();
      const ul = document.getElementById('activityList');
      ul.innerHTML = (Array.isArray(items) ? items : []).slice(0,8).map(x =>
        '<li style="padding:8px 0;border-bottom:1px solid rgba(255,255,255,.05)">'
        + (typeof x === 'string' ? x : JSON.stringify(x)) + '</li>').join('') || '<li>No activity yet.</li>';
    }
    document.getElementById('lastUpdated').textContent =
      'Updated: ' + new Date().toLocaleTimeString();
  } catch(e) {
    document.getElementById('lastUpdated').textContent = 'Could not load data.';
  }
}
loadDashboard();
setInterval(loadDashboard, 30000);""",
    )


def _gen_profile_template():
    return _tpl(
        "Profile",
        """<div class="wrap" style="max-width:500px">
<div class="card" style="text-align:center">
  <div style="width:80px;height:80px;border-radius:50%;background:#6366f1;
    margin:0 auto 16px;display:flex;align-items:center;justify-content:center;
    font-size:2rem;font-weight:700" id="avatar">?</div>
  <h1 id="profileName" style="margin:0 0 4px">Loading\u2026</h1>
  <p id="profileEmail" style="color:#94a3b8;margin:0 0 20px"></p>
</div>
<div class="card">
  <h2 style="margin:0 0 14px;font-size:1rem;color:#94a3b8">Edit Profile</h2>
  <input id="editName"  placeholder="Display name">
  <input id="editEmail" type="email" placeholder="Email">
  <button class="btn" onclick="saveProfile()">Save Changes</button>
  <div id="saveStatus" class="status"></div>
</div></div>""",
        extra_js="""
async function loadProfile() {
  try {
    const r = await fetch('/api/profile').catch(() => fetch('/api/me').catch(() => fetch('/api/user')));
    if (!r || !r.ok) return;
    const p = await r.json();
    const name = p.name || p.username || p.display_name || 'User';
    document.getElementById('profileName').textContent  = name;
    document.getElementById('profileEmail').textContent = p.email || '';
    document.getElementById('avatar').textContent       = name[0].toUpperCase();
    document.getElementById('editName').value  = name;
    document.getElementById('editEmail').value = p.email || '';
  } catch(_) {}
}
async function saveProfile() {
  const st = document.getElementById('saveStatus');
  st.textContent = 'Saving\u2026'; st.className = 'status';
  try {
    const r = await fetch('/api/profile', {method:'PUT',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:document.getElementById('editName').value,
                           email:document.getElementById('editEmail').value})});
    if (r.ok) { st.textContent = 'Saved!'; loadProfile(); }
    else { const d = await r.json().catch(()=>({})); st.textContent = d.error||'Save failed'; st.className='status error'; }
  } catch(e) { st.textContent='Network error'; st.className='status error'; }
}
loadProfile();""",
    )


# ── Classification ─────────────────────────────────────────────────────────────

def _classify_template(template_name, vars_passed):
    """Determine which kind of template to generate based on filename and context."""
    stem = re.sub(r"\.[a-z0-9]+$", "", template_name.lower())

    if stem in ("base", "layout", "base_layout", "app", "wrapper", "skeleton"):
        return "base"
    if stem in ("login", "signin", "sign_in"):
        return "login"
    if stem in ("register", "signup", "sign_up", "registration"):
        return "register"
    if stem in ("404", "500", "403", "error", "not_found", "forbidden"):
        return "error"
    if stem in ("profile", "account", "me", "settings", "user"):
        return "profile"
    if stem in ("dashboard", "admin", "panel", "overview"):
        return "dashboard"
    if stem in ("calculator", "calc"):
        return "calculator"
    if stem in ("blog", "posts", "articles", "news") or "posts" in vars_passed:
        return "blog"
    return "crud"


def _extract_template_calls(source):
    """Return {template_name: [kwarg_names]} for every render_template() call."""
    result = {}
    for m in _RENDER_CALL_RE.finditer(source or ""):
        name = m.group(1)
        kwargs = [k for k in _KWARG_NAME_RE.findall(m.group(2)) if k not in ("method", "host", "port", "debug")]
        if name not in result:
            result[name] = []
        result[name].extend(k for k in kwargs if k not in result[name])
    return result


def _generate_template_for_name(template_name, vars_passed, source):
    """Generate the most appropriate HTML for a specific template name."""
    kind = _classify_template(template_name, vars_passed)

    if kind == "base":
        return _gen_base_template(), "base"
    if kind == "login":
        # Try to find the actual login route
        login_route = next(
            (r for r in extract_flask_routes(source) if "login" in r or "signin" in r), "/login"
        )
        return _gen_login_template(login_route), "login"
    if kind == "register":
        reg_route = next(
            (r for r in extract_flask_routes(source) if "register" in r or "signup" in r), "/register"
        )
        return _gen_register_template(reg_route), "register"
    if kind == "error":
        stem = re.sub(r"\.[a-z0-9]+$", "", template_name.lower())
        return _gen_error_template(stem if stem.isdigit() else "Error"), "error"
    if kind == "profile":
        return _gen_profile_template(), "profile"
    if kind == "dashboard":
        return _gen_dashboard_template(vars_passed), "dashboard"
    if kind == "calculator":
        calc_route = next(
            (r for r in extract_flask_routes(source) if "calc" in r or "compute" in r or "evaluate" in r),
            "/api/calculate",
        )
        return _gen_calculator_template(calc_route), "calculator"
    if kind == "blog":
        # Infer list and add routes from source
        routes_info = []
        seen = set()
        for mm in _FLASK_ROUTE_METHODS_RE.finditer(source or ""):
            p = mm.group(1)
            raw = mm.group(2) or ""
            methods = [x.strip().strip("'\"").upper() for x in raw.split(",") if x.strip().strip("'\"")] or ["GET"]
            if p not in seen:
                seen.add(p)
                routes_info.append({"path": p, "methods": methods})
        list_url = next((r["path"] for r in routes_info if "GET" in r["methods"] and "<" not in r["path"] and r["path"] != "/"), "/api/posts")
        add_url  = next((r["path"] for r in routes_info if "POST" in r["methods"] and "<" not in r["path"]), list_url)
        resource = list_url.strip("/").split("/")[-1]
        tf = _guess_field_name(resource)
        return _gen_blog_template(list_url, add_url, tf, "content"), "blog"

    # Default: smart CRUD
    return generate_smart_ui_template(source)


def write_text(path, content):
    """Write text content to disk with UTF-8 encoding."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def read_text(path):
    """Read UTF-8 text from disk."""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def safe_project_slug(raw):
    """Create a filesystem-safe project folder suffix."""
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", (raw or "app").strip()).strip("_").lower()
    return cleaned[:40] or "app"


def get_latest_project_dir():
    """Return the latest generated project directory if available."""
    if os.path.exists(LATEST_PROJECT_FILE):
        try:
            path = read_text(LATEST_PROJECT_FILE).strip()
            if path and os.path.isdir(path) and os.path.exists(os.path.join(path, "main.py")):
                return path
        except Exception:
            pass

    candidates = []
    try:
        for name in os.listdir(BUILDS_DIR):
            full = os.path.join(BUILDS_DIR, name)
            if os.path.isdir(full) and name.startswith("build_") and os.path.exists(os.path.join(full, "main.py")):
                candidates.append(full)
    except FileNotFoundError:
        return None

    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def get_latest_built_file():
    """Return the latest runnable app file from the Builds folder."""
    project_dir = get_latest_project_dir()
    if project_dir:
        main_file = os.path.join(project_dir, "main.py")
        if os.path.exists(main_file):
            return main_file

    if os.path.exists(LATEST_OUTPUT_FILE):
        return LATEST_OUTPUT_FILE

    candidates = []
    try:
        for filename in os.listdir(BUILDS_DIR):
            if filename.startswith("built_app_") and filename.endswith(".py"):
                candidates.append(os.path.join(BUILDS_DIR, filename))
    except FileNotFoundError:
        return None

    if candidates:
        return max(candidates, key=os.path.getmtime)

    return None


def ensure_main_has_entrypoint(code):
    """Append a minimal fallback entrypoint if no __main__ block exists."""
    if "if __name__ == \"__main__\":" in code or "if __name__ == '__main__':" in code:
        return code

    return (
        code.rstrip()
        + "\n\n"
        + "if __name__ == \"__main__\":\n"
        + "    print(\"Generated app loaded. Add a main entrypoint for interactive behavior.\")\n"
    )


def infer_requirements_from_code(code):
    """Infer third-party dependencies from import statements."""
    module_to_package = {
        "bs4": "beautifulsoup4",
        "cv2": "opencv-python",
        "PIL": "Pillow",
        "yaml": "PyYAML",
        "sklearn": "scikit-learn",
    }
    stdlib_modules = set(getattr(sys, "stdlib_module_names", set()))
    ignored = {"app", "tests", "main", "__future__"}

    try:
        tree = ast.parse(code)
    except Exception:
        return []

    discovered = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                discovered.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            discovered.add(node.module.split(".")[0])

    packages = []
    for module in sorted(discovered):
        if module in ignored or module in stdlib_modules:
            continue
        packages.append(module_to_package.get(module, module))
    return packages


def build_project_preview(files):
    """Return a readable multi-file preview for the UI code pane."""
    chunks = []
    for rel_path in sorted(files):
        chunks.append(f"# FILE: {rel_path}\n{files[rel_path].rstrip()}\n")
    return "\n".join(chunks).strip()


def extract_flask_routes(source):
    """Extract distinct Flask route paths from source code."""
    routes = []
    for match in _FLASK_ROUTE_RE.finditer(source or ""):
        route = (match.group(1) or "").strip()
        if route and route not in routes:
            routes.append(route)
    return routes


def _guess_field_name(resource_name):
    """Map a plural resource name to its primary field name (e.g. 'todos' → 'task')."""
    mapping = {
        "todos": "task", "tasks": "task", "notes": "note", "items": "item",
        "messages": "message", "posts": "content", "entries": "entry",
        "users": "name", "products": "name", "books": "title",
        "movies": "title", "songs": "title", "events": "title",
        "records": "name", "contacts": "name",
    }
    name = (resource_name or "item").lower()
    return mapping.get(name, name.rstrip("s") or "value")


def generate_smart_ui_template(source):
    """Generate a proper interactive dark-theme UI template from Flask source analysis."""
    # Extract routes with their HTTP methods
    routes_info = []
    seen = set()
    for m in _FLASK_ROUTE_METHODS_RE.finditer(source or ""):
        path = m.group(1)
        raw = m.group(2) or ""
        methods = [x.strip().strip("'\"").upper() for x in raw.split(",") if x.strip().strip("'\"")] or ["GET"]
        key = path
        if key not in seen:
            seen.add(key)
            routes_info.append({"path": path, "methods": methods})

    # Pick the best LIST_URL (first non-root GET route without path params)
    list_url = None
    for r in routes_info:
        p = r["path"]
        if p in ("/", "/index") or "<" in p:
            continue
        if "GET" in r["methods"] or not r["methods"]:
            list_url = p
            break

    # Pick the best ADD_URL (first POST route without path params)
    add_url = None
    for r in routes_info:
        p = r["path"]
        if "<" in p:
            continue
        if "POST" in r["methods"]:
            add_url = p
            break

    # Fallback: if we couldn't find dedicated list/add routes, use the same path
    if not list_url and not add_url:
        list_url = "/items"
        add_url = "/items"
    elif not list_url:
        list_url = add_url
    elif not add_url:
        add_url = list_url

    resource_name = list_url.strip("/").split("/")[-1] if list_url else "items"
    main_field = _guess_field_name(resource_name)
    app_title = resource_name.replace("_", " ").replace("-", " ").title()
    placeholder = f"New {_guess_field_name(resource_name)}\u2026"

    return (
        _SMART_UI_TEMPLATE
        .replace("APP_TITLE", html.escape(app_title))
        .replace("API_LIST_PATH", list_url)
        .replace("API_ADD_PATH", add_url)
        .replace("MAIN_FIELD", main_field)
        .replace("ADD_PLACEHOLDER", placeholder)
    ), "smart"


def extract_template_names(source):
    """Return all unique template filenames referenced by render_template() calls."""
    names, seen = [], set()
    for m in _RENDER_TEMPLATE_NAME_RE.finditer(source or ""):
        name = m.group(1)
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def generate_index_template_from_code(source):
    """Generate a best-effort template for Flask apps that use render_template()."""
    if not _RENDER_TEMPLATE_RE.search(source or ""):
        return "", ""
    return generate_smart_ui_template(source)


def _looks_like_legacy_fallback_template(content):
    lowered = (content or "").lower()
    return any(marker in lowered for marker in _LEGACY_FALLBACK_MARKERS)


def ensure_generated_templates_for_project(project_dir, source):
    """Ensure Flask template files exist for every render_template() call in source."""
    # Build {name: [vars_passed]} so each template gets the right generator
    calls_map = _extract_template_calls(source)
    if not calls_map:
        return {"status": "not_needed", "path": "", "kind": ""}

    templates_dir = os.path.join(project_dir, "templates")
    first_result = None

    for template_filename, vars_passed in calls_map.items():
        target_template = os.path.join(templates_dir, template_filename)

        if os.path.exists(target_template):
            try:
                existing = read_text(target_template)
            except Exception:
                existing = ""
            if _looks_like_legacy_fallback_template(existing):
                new_content, new_kind = _generate_template_for_name(
                    template_filename, vars_passed, source
                )
                write_text(target_template, new_content)
                result = {"status": "upgraded", "path": target_template, "kind": new_kind}
            else:
                result = {"status": "exists", "path": target_template, "kind": "existing"}
            first_result = first_result or result
            continue

        # Check alternate locations inside the project
        found = False
        for candidate in [
            os.path.join(project_dir, "app", "templates", template_filename),
            os.path.join(project_dir, "src", "templates", template_filename),
        ]:
            if os.path.exists(candidate):
                write_text(target_template, read_text(candidate))
                result = {"status": "copied", "path": target_template, "kind": "copied"}
                first_result = first_result or result
                found = True
                break

        if not found:
            content, kind = _generate_template_for_name(
                template_filename, vars_passed, source
            )
            write_text(target_template, content or _BASIC_INDEX_TEMPLATE)
            result = {"status": "generated", "path": target_template, "kind": kind or "basic"}
            first_result = first_result or result

    return first_result or {"status": "not_needed", "path": "", "kind": ""}


def create_project_bundle(code, tests, plan, review, user_request):
    """Create a multi-file app folder, zip it, and refresh latest pointers."""
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = safe_project_slug(user_request)
    project_dir_name = f"build_{timestamp}_{slug}"
    project_dir = os.path.join(BUILDS_DIR, project_dir_name)
    os.makedirs(project_dir, exist_ok=True)

    main_code = ensure_main_has_entrypoint(code)
    reqs = infer_requirements_from_code(main_code)

    tests_code = tests if (tests or "").strip() else "def test_placeholder():\n    assert True\n"
    files = {
        "main.py": main_code,
        "requirements.txt": "\n".join(reqs) + ("\n" if reqs else ""),
        "README.md": (
            "# Generated App\n\n"
            "## Run\n"
            "1. Install dependencies: `python -m pip install -r requirements.txt`\n"
            "2. Start the app: `python main.py`\n\n"
            "This project was generated by AI Office Builder.\n"
        ),
        "app/__init__.py": "\"\"\"Generated application package.\"\"\"\n",
        "app/config.py": (
            "import os\n\n"
            "ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\n"
        ),
        "tests/test_generated.py": tests_code,
        "build_meta.json": json.dumps(
            {
                "created_at": timestamp,
                "request": user_request,
                "plan": plan,
                "review": review,
                "entrypoint": "main.py",
            },
            indent=2,
        )
        + "\n",
    }

    # Generate the right template per name — blog gets blog UI, login gets login form, etc.
    calls_map = _extract_template_calls(main_code)
    for tname, vars_passed in calls_map.items():
        content, _ = _generate_template_for_name(tname, vars_passed, main_code)
        files[f"templates/{tname}"] = content or _BASIC_INDEX_TEMPLATE

    for rel_path, content in files.items():
        write_text(os.path.join(project_dir, rel_path), content)

    write_text(LATEST_OUTPUT_FILE, main_code)
    legacy_archive = os.path.join(BUILDS_DIR, f"built_app_{timestamp}.py")
    write_text(legacy_archive, main_code)

    write_text(LATEST_PROJECT_FILE, project_dir)

    zip_name = f"{project_dir_name}.zip"
    zip_path = os.path.join(BUILDS_DIR, zip_name)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel_path in files:
            abs_path = os.path.join(project_dir, rel_path)
            zf.write(abs_path, arcname=rel_path)

    with zipfile.ZipFile(LATEST_ZIP_FILE, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel_path in files:
            abs_path = os.path.join(project_dir, rel_path)
            zf.write(abs_path, arcname=rel_path)

    return {
        "project_dir": project_dir,
        "entrypoint": os.path.join(project_dir, "main.py"),
        "requirements": os.path.join(project_dir, "requirements.txt"),
        "zip_path": zip_path,
        "latest_zip_path": LATEST_ZIP_FILE,
        "legacy_file": legacy_archive,
        "code_preview": build_project_preview(files),
    }


def refresh_project_zip(project_dir):
    """Recreate project zip files for a given project folder."""
    project_name = os.path.basename(project_dir.rstrip("\\/"))
    zip_path = os.path.join(BUILDS_DIR, f"{project_name}.zip")

    def _write_zip(target_zip):
        with zipfile.ZipFile(target_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(project_dir):
                for filename in files:
                    abs_path = os.path.join(root, filename)
                    rel_path = os.path.relpath(abs_path, project_dir)
                    if "__pycache__" in rel_path:
                        continue
                    zf.write(abs_path, arcname=rel_path)

    _write_zip(zip_path)
    _write_zip(LATEST_ZIP_FILE)
    return zip_path


def save_repaired_project_main(project_dir, repaired_code):
    """Persist repaired main.py and refresh legacy/latest artifacts."""
    main_file = os.path.join(project_dir, "main.py")
    write_text(main_file, repaired_code)
    write_text(LATEST_OUTPUT_FILE, repaired_code)

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    legacy_archive = os.path.join(BUILDS_DIR, f"built_app_{timestamp}.py")
    write_text(legacy_archive, repaired_code)

    zip_path = refresh_project_zip(project_dir)
    return {
        "main_file": main_file,
        "legacy_file": legacy_archive,
        "zip_path": zip_path,
    }
