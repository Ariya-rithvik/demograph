"""Ledgerly: a small fictional invoicing SaaS for DemoGraph to explore.

Every page is rendered from catalog.json on each request, so changing a price or
a button label (see mutate.py) changes the live product the way a real release would.
All data is synthetic.

    python sample_app/server.py --port 8765
"""

import argparse
import json
import os
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
DEFAULT_CATALOG = HERE / "catalog.json"
WORKING_CATALOG = HERE.parent / "data" / "sample_app" / "catalog.json"


def catalog_path() -> Path:
    if os.getenv("LEDGERLY_CATALOG"):
        return Path(os.environ["LEDGERLY_CATALOG"])
    return WORKING_CATALOG if WORKING_CATALOG.exists() else DEFAULT_CATALOG


def load_catalog(path: Path | None = None) -> dict:
    return json.loads((path or catalog_path()).read_text(encoding="utf-8"))


CSS = """
*{box-sizing:border-box}body{margin:0;font-family:Inter,Segoe UI,system-ui,sans-serif;color:#1d2433;background:#f6f7fb}
a{color:#3b5bdb;text-decoration:none}a:hover{text-decoration:underline}
header{display:flex;align-items:center;gap:28px;padding:16px 40px;background:#fff;border-bottom:1px solid #e4e7ef}
.logo{font-weight:800;font-size:20px;color:#1d2433}.logo span{color:#3b5bdb}
nav{display:flex;gap:20px;flex:1}nav a{color:#4a5368;font-weight:500}
main{max-width:1040px;margin:0 auto;padding:40px}
.btn{display:inline-block;border:0;border-radius:8px;padding:11px 18px;font:600 15px inherit;font-family:inherit;cursor:pointer;background:#3b5bdb;color:#fff}
.btn.ghost{background:#fff;color:#3b5bdb;border:1px solid #c5cff5}.btn.danger{background:#fff;color:#c92a2a;border:1px solid #f1b0b0}
.hero{padding:56px 0 32px}.hero h1{font-size:46px;margin:0 0 12px}.hero p{font-size:20px;color:#4a5368;margin:0 0 28px}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:20px}
.card{background:#fff;border:1px solid #e4e7ef;border-radius:12px;padding:24px}
.card h3{margin:0 0 8px}.muted{color:#6b7385}
.price{font-size:38px;font-weight:800;margin:8px 0}.price small{font-size:15px;color:#6b7385;font-weight:500}
.card ul{padding-left:18px;color:#4a5368;line-height:1.7}.card.featured{border:2px solid #3b5bdb}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:20px;margin:24px 0}
.stat .value{font-size:30px;font-weight:800}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:12px;overflow:hidden;border:1px solid #e4e7ef}
th,td{text-align:left;padding:14px 16px;border-bottom:1px solid #eef0f5}th{background:#fafbfd;color:#6b7385;font-weight:600}
.pill{display:inline-block;padding:3px 10px;border-radius:99px;font-size:13px;font-weight:600}
.Paid{background:#e6f6ec;color:#2b8a3e}.Overdue{background:#fdeaea;color:#c92a2a}.Sent{background:#e7edff;color:#3b5bdb}
form.card{max-width:520px;display:grid;gap:16px}label{display:grid;gap:6px;font-weight:600}
input,select{font:15px inherit;font-family:inherit;padding:10px 12px;border:1px solid #cfd5e3;border-radius:8px}
.toolbar{display:flex;justify-content:space-between;align-items:center}
.banner{background:#e6f6ec;color:#2b8a3e;border-radius:8px;padding:12px 16px;margin-bottom:20px;font-weight:600}
.toast{position:fixed;right:24px;bottom:24px;background:#1d2433;color:#fff;padding:14px 18px;border-radius:10px;display:none}
footer{text-align:center;color:#8a91a3;padding:40px}
"""

TOAST_JS = """
function toast(msg){const t=document.getElementById('toast');t.textContent=msg;t.style.display='block';
t.setAttribute('role','status');setTimeout(()=>{t.style.display='none'},4000)}
document.addEventListener('click',e=>{const b=e.target.closest('[data-href]');if(b){location.href=b.dataset.href}});
"""


def money(c: dict, amount: float) -> str:
    return f"{c['currency']}{amount:,.0f}"


def layout(c: dict, title: str, body: str, app: bool = False) -> str:
    if app:
        links = [("/app", "Dashboard"), ("/app/invoices", "Invoices"),
                 ("/app/reports", "Reports"), ("/app/settings", "Settings")]
    else:
        links = [("/features", "Features"), ("/pricing", "Pricing"), ("/help", "Help"), ("/app", "Demo workspace")]
    nav = "".join(f'<a href="{href}">{escape(label)}</a>' for href, label in links)
    p = escape(c["product"])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)} · {p}</title><style>{CSS}</style></head><body>
<header><a class="logo" href="/">{p[:-2]}<span>{p[-2:]}</span></a><nav>{nav}</nav></header>
<main>{body}</main><footer>{p} is a fictional product used to demo DemoGraph.</footer>
<div id="toast" class="toast"></div><script>{TOAST_JS}</script></body></html>"""


def plan_by_id(c: dict, plan_id: str) -> dict:
    return next((p for p in c["plans"] if p["id"] == plan_id), c["plans"][0])


# --- Marketing pages ---------------------------------------------------------

def page_home(c, q):
    cards = "".join(
        f'<div class="card"><h3>{escape(f["name"])}</h3><p class="muted">{escape(f["summary"])}</p>'
        f'<a href="/features/{f["slug"]}">Learn more</a></div>' for f in c["features"])
    pro = plan_by_id(c, "pro")
    body = f"""<section class="hero"><h1>{escape(c["tagline"])}</h1>
<p>{escape(c["product"])} sends your invoices, reminds late payers, and shows you where the money is.</p>
<button class="btn" id="start-trial" onclick="location.href='/signup'">{escape(c["labels"]["start_trial"])}</button>
<a class="btn ghost" href="/pricing">See pricing</a>
<p class="muted" style="font-size:15px;margin-top:18px">Pro starts at {money(c, pro["price"])} a month. No card needed for the trial.</p>
</section><div class="grid">{cards}</div>"""
    return layout(c, "Home", body)


def page_features(c, q):
    cards = "".join(
        f'<div class="card"><h3><a href="/features/{f["slug"]}">{escape(f["name"])}</a></h3>'
        f'<p class="muted">{escape(f["summary"])}</p></div>' for f in c["features"])
    return layout(c, "Features", f"<h1>Features</h1><div class='grid'>{cards}</div>")


def page_feature(c, q, slug):
    f = next((x for x in c["features"] if x["slug"] == slug), None)
    if not f:
        return None
    body = f"""<h1>{escape(f["name"])}</h1><p style="font-size:19px">{escape(f["summary"])}</p>
<div class="card"><p>{escape(f["detail"])}</p>
<a class="btn" href="/app">Try it in the demo workspace</a></div>"""
    return layout(c, f["name"], body)


def page_pricing(c, q):
    cards = []
    for p in c["plans"]:
        items = "".join(f"<li>{escape(x)}</li>" for x in p["features"])
        featured = " featured" if p["id"] == "pro" else ""
        cards.append(f"""<div class="card{featured}" id="plan-{p["id"]}"><h3>{escape(p["name"])}</h3>
<div class="price">{money(c, p["price"])}<small> / month</small></div>
<p class="muted">{escape(p["limit"])}</p><ul>{items}</ul>
<button class="btn" data-href="/signup?plan={p["id"]}">Choose {escape(p["name"])}</button></div>""")
    body = f"<h1>Pricing</h1><p class='muted'>Every plan includes a 14-day free trial.</p><div class='grid'>{''.join(cards)}</div>"
    return layout(c, "Pricing", body)


def page_help(c, q):
    faqs = [
        ("Can I change plans later?", "Yes. Go to Settings and choose Change plan. The new price applies from your next bill."),
        ("Who receives reminders?", "Only the billing contact on each invoice. You can turn reminders off per client."),
        ("Can I export my data?", "Yes. Reports has an Export CSV button that downloads every invoice."),
    ]
    items = "".join(f"<div class='card' style='margin-bottom:16px'><h3>{escape(qn)}</h3><p>{escape(a)}</p></div>" for qn, a in faqs)
    return layout(c, "Help", f"<h1>Help center</h1>{items}")


def page_signup(c, q):
    chosen = (q.get("plan") or ["pro"])[0]
    options = "".join(
        f'<option value="{p["id"]}"{" selected" if p["id"] == chosen else ""}>{escape(p["name"])} · {money(c, p["price"])}/month</option>'
        for p in c["plans"])
    body = f"""<h1>Start your free trial</h1>
<form class="card" method="post" action="/signup">
<label>Full name<input name="name" required></label>
<label>Work email<input name="email" type="email" required></label>
<label>Company<input name="company"></label>
<label>Plan<select name="plan">{options}</select></label>
<button class="btn" type="submit">Create workspace</button></form>"""
    return layout(c, "Sign up", body)


# --- Demo workspace ------------------------------------------------------------

def page_dashboard(c, q):
    inv = c["invoices"]
    outstanding = sum(i["amount"] for i in inv if i["status"] != "Paid")
    overdue = sum(1 for i in inv if i["status"] == "Overdue")
    paid = sum(i["amount"] for i in inv if i["status"] == "Paid")
    banner = '<div class="banner">Your workspace is ready.</div>' if "welcome" in q else ""
    body = f"""{banner}<div class="toolbar"><h1>Good morning, {escape(c["workspace"]["name"])}</h1>
<button class="btn" id="new-invoice" onclick="location.href='/app/invoices/new'">{escape(c["labels"]["new_invoice"])}</button></div>
<div class="stats"><div class="card stat"><div class="muted">Outstanding</div><div class="value">{money(c, outstanding)}</div></div>
<div class="card stat"><div class="muted">Overdue invoices</div><div class="value">{overdue}</div></div>
<div class="card stat"><div class="muted">Paid this month</div><div class="value">{money(c, paid)}</div></div></div>
<p><a href="/app/invoices">View all invoices</a> · <a href="/app/reports">Open cash-flow report</a></p>"""
    return layout(c, "Dashboard", body, app=True)


def invoice_rows(c):
    return "".join(
        f'<tr><td><a href="/app/invoices/{i["id"]}">{i["id"]}</a></td><td>{escape(i["client"])}</td>'
        f'<td>{money(c, i["amount"])}</td><td><span class="pill {i["status"]}">{i["status"]}</span></td><td>{i["due"]}</td></tr>'
        for i in c["invoices"])


def page_invoices(c, q):
    banner = f'<div class="banner">Invoice {escape(q["created"][0])} created and sent.</div>' if "created" in q else ""
    body = f"""{banner}<div class="toolbar"><h1>Invoices</h1>
<button class="btn" onclick="location.href='/app/invoices/new'">{escape(c["labels"]["new_invoice"])}</button></div>
<table><thead><tr><th>Invoice</th><th>Client</th><th>Amount</th><th>Status</th><th>Due</th></tr></thead>
<tbody>{invoice_rows(c)}</tbody></table>"""
    return layout(c, "Invoices", body, app=True)


def page_invoice(c, q, inv_id):
    i = next((x for x in c["invoices"] if x["id"] == inv_id), None)
    if not i:
        return None
    reminder = ""
    if i["status"] != "Paid":
        reminder = (f'<button class="btn" id="send-reminder" onclick="toast(\'Reminder scheduled for {escape(i["client"])}\')">'
                    f'{escape(c["labels"]["send_reminder"])}</button>')
    body = f"""<p><a href="/app/invoices">&larr; All invoices</a></p><h1>{i["id"]}</h1>
<div class="card"><p class="muted">Billed to</p><h3>{escape(i["client"])}</h3>
<div class="price">{money(c, i["amount"])}</div>
<p><span class="pill {i["status"]}">{i["status"]}</span> · due {i["due"]}</p>{reminder}</div>"""
    return layout(c, i["id"], body, app=True)


def page_new_invoice(c, q):
    clients = sorted({i["client"] for i in c["invoices"]})
    options = "".join(f"<option>{escape(x)}</option>" for x in clients)
    body = f"""<h1>New invoice</h1><form class="card" method="post" action="/app/invoices/new">
<label>Client<select name="client">{options}</select></label>
<label>Amount<input name="amount" type="number" min="1" required></label>
<label>Due date<input name="due" type="date" required></label>
<button class="btn" type="submit">{escape(c["labels"]["create_invoice"])}</button></form>"""
    return layout(c, "New invoice", body, app=True)


def page_reports(c, q):
    by_status = {}
    for i in c["invoices"]:
        by_status[i["status"]] = by_status.get(i["status"], 0) + i["amount"]
    cards = "".join(f'<div class="card stat"><div class="muted">{s}</div><div class="value">{money(c, v)}</div></div>'
                    for s, v in by_status.items())
    body = f"""<div class="toolbar"><h1>Cash-flow report</h1>
<button class="btn ghost" id="export-csv" onclick="toast('CSV export started. Check your downloads in a moment.')">{escape(c["labels"]["export_csv"])}</button></div>
<div class="stats">{cards}</div>"""
    return layout(c, "Reports", body, app=True)


def page_settings(c, q):
    plan = plan_by_id(c, c["workspace"]["plan"])
    body = f"""<h1>Settings</h1><div class="card"><p class="muted">Workspace</p><h3>{escape(c["workspace"]["name"])}</h3>
<p>Current plan: <strong>{escape(plan["name"])}</strong> at <strong>{money(c, plan["price"])} a month</strong></p>
<a class="btn ghost" href="/pricing">Change plan</a></div>
<div class="card" style="margin-top:20px"><h3>Danger zone</h3><p class="muted">This cannot be undone.</p>
<button class="btn danger" onclick="if(confirm('Delete workspace?')){{toast('Workspace deleted')}}">Delete workspace</button></div>"""
    return layout(c, "Settings", body, app=True)


ROUTES = {
    "/": page_home, "/features": page_features, "/pricing": page_pricing, "/help": page_help,
    "/signup": page_signup, "/app": page_dashboard, "/app/invoices": page_invoices,
    "/app/invoices/new": page_new_invoice, "/app/reports": page_reports, "/app/settings": page_settings,
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # keep test output quiet
        if os.getenv("LEDGERLY_LOG"):
            super().log_message(fmt, *args)

    def _send(self, status: int, html: str | None = None, location: str | None = None):
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        data = (html or "").encode("utf-8")
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlsplit(self.path)
        path = url.path.rstrip("/") or "/"
        q = parse_qs(url.query)
        c = load_catalog(self.server.catalog)
        html = None
        if path in ROUTES:
            html = ROUTES[path](c, q)
        elif path.startswith("/features/"):
            html = page_feature(c, q, path.split("/")[-1])
        elif path.startswith("/app/invoices/"):
            html = page_invoice(c, q, path.split("/")[-1])
        if path == "/robots.txt":
            return self._send(200, "User-agent: *\nAllow: /\n")
        if html is None:
            return self._send(404, layout(c, "Not found", "<h1>Page not found</h1><a href='/'>Go home</a>"))
        self._send(200, html)

    def do_POST(self):
        path = urlsplit(self.path).path.rstrip("/")
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)  # stateless demo: submitted values are not stored
        if path == "/signup":
            return self._send(303, location="/app?welcome=1")
        if path == "/app/invoices/new":
            return self._send(303, location="/app/invoices?created=INV-2001")
        self._send(404, "not found")


def make_server(port: int = 8765, host: str = "127.0.0.1", catalog: Path | None = None) -> ThreadingHTTPServer:
    """catalog pins this server to one catalog file (tests); None follows catalog_path()."""
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.catalog = catalog
    return srv


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    srv = make_server(args.port)
    print(f"Ledgerly running on http://127.0.0.1:{args.port} (catalog: {catalog_path()})")
    srv.serve_forever()
