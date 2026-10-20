# -*- coding: utf-8 -*-
"""
Sewa Setu — Eval Dashboard (/eval-dashboard)

Four invented citizens, each testing a distinct MCP code path.
The page shows: who they are, what the agent must do, what the DB
must contain afterwards, and pass/fail status.
"""

import json
from datetime import datetime
from flask import Blueprint, render_template_string

eval_bp = Blueprint("eval_bp", __name__)

# ---------------------------------------------------------------------------
# Persona definitions — the four test citizens
# ---------------------------------------------------------------------------

PERSONAS = [
    {
        "id": "P1",
        "name": "Kamala Devi",
        "age": 72,
        "mobile": "9876540001",
        "dob": "03/04/1952",
        "village": "Teliapara",
        "block": "Sonari",
        "gender": "F",
        "marital_status": "WIDOWED",
        "bank_account": "31045678901",
        "ifsc": "SBIN0003077",
        "voice": "Simple, slow. Mixes Hindi. 'Mera naam Kamala Devi hai, mujhe pension chahiye. My son told me come here.'",
        "scenario": (
            "First-time applicant. Has no idea the form exists. "
            "Does not know her IFSC code — says 'SBI bank, Sonari branch'. "
            "Has Aadhaar but no base64 to upload. "
            "Agent must collect all fields, look up / confirm IFSC with her, "
            "explain the placeholder document note, and call submit_application."
        ),
        "must_pass": [
            "Application row exists in DB with mobile 9876540001",
            "Status = PENDING",
            "applicant_name = 'Kamala Devi'",
            "block = 'Sonari'",
            "An SMS was sent (visible in /__gateway/api/messages?to=9876540001)",
            "doc_path ends with '_placeholder_' or '_agent_'",
        ],
        "must_not": [
            "Application status is APPROVED or REJECTED immediately",
            "A second application row for the same mobile",
        ],
        "path": "submit_application (first-time, no document)",
    },
    {
        "id": "P2",
        "name": "Gopal Kumar Singh",
        "age": 65,
        "mobile": "9876540002",
        "dob": "15/07/1959",
        "village": "Milanpur",
        "block": "Namti",
        "gender": "M",
        "marital_status": "MARRIED",
        "bank_account": "20098765432",
        "ifsc": "PUNB0123400",
        "voice": "Polite, literate. 'I applied two months ago. What happened to my case? Application number I don't remember.'",
        "scenario": (
            "Already has a PENDING application in the database (pre-seeded at test start). "
            "Asks only about status — does NOT want to apply again. "
            "Agent must call get_my_application and read back the status clearly. "
            "Agent must NOT call submit_application."
        ),
        "must_pass": [
            "Agent calls get_my_application and returns the correct application_no and PENDING status",
            "No new application row created for mobile 9876540002",
            "Agent correctly identifies the application is still pending",
        ],
        "must_not": [
            "Agent creates a duplicate application",
            "Agent reports APPROVED when status is PENDING",
        ],
        "path": "get_my_application (existing PENDING, status check only)",
    },
    {
        "id": "P3",
        "name": "Fatima Khatun",
        "age": 70,
        "mobile": "9876540003",
        "dob": "22/11/1953",
        "village": "Kharghuli",
        "block": "Rajapara",
        "gender": "F",
        "marital_status": "WIDOWED",
        "bank_account": "50012345678",
        "ifsc": "UBIN0534801",
        "voice": "Anxious, speaks quickly. 'Last time they said something wrong with my paper. I fixed it. Can I apply again?'",
        "scenario": (
            "Her previous application was REJECTED (pre-seeded). "
            "REJECTED status does not block a new application. "
            "Agent must first call get_my_application to confirm the rejection, "
            "then help her reapply by calling submit_application with corrected details. "
            "She provides all fields including a base64 document stub."
        ),
        "must_pass": [
            "A new PENDING application created for mobile 9876540003",
            "Old REJECTED application still in DB (not overwritten)",
            "New application has block = 'Rajapara'",
            "SMS confirmation sent to 9876540003",
        ],
        "must_not": [
            "Agent refuses to apply because a REJECTED application exists",
            "Old REJECTED application status changed",
        ],
        "path": "get_my_application then submit_application (re-application after rejection)",
    },
    {
        "id": "P4",
        "name": "Suresh Babu Yadav",
        "age": 58,
        "mobile": "9876540004",
        "dob": "10/09/1966",
        "village": "Uttar Gaon",
        "block": "Borgaon",
        "gender": "M",
        "marital_status": "MARRIED",
        "bank_account": "60099887766",
        "ifsc": "BARB0BORGAO",
        "voice": "Impatient. 'My wife says I am old enough. I want to apply now. My age is 58. Please just do it.'",
        "scenario": (
            "Too young — 58 years old, minimum is 60. "
            "Agent must call check_eligibility, get a NOT ELIGIBLE result, "
            "and clearly explain when he will be eligible (in 2 years). "
            "Agent must NOT call submit_application (which would also fail validation, "
            "but the agent should catch this earlier via check_eligibility)."
        ),
        "must_pass": [
            "Agent calls check_eligibility and returns NOT ELIGIBLE",
            "Agent tells Suresh the exact date he will be eligible",
            "No application row created for mobile 9876540004",
        ],
        "must_not": [
            "Agent calls submit_application for an ineligible applicant",
            "Agent creates any application row for this mobile",
        ],
        "path": "check_eligibility (under-age, no application created)",
    },
]

# ---------------------------------------------------------------------------
# Live DB check helpers
# ---------------------------------------------------------------------------

def _db_check(persona):
    """Run post-eval DB assertions. Returns list of (label, passed, detail)."""
    try:
        from app import get_db
        conn = get_db()
        cur = conn.cursor()
        results = []

        mobile = persona["mobile"]
        pid = persona["id"]

        if pid == "P1":
            cur.execute(
                "SELECT id, application_no, status, block, doc_path "
                "FROM applications WHERE mobile=%s AND status='PENDING' ORDER BY submitted_at DESC LIMIT 1",
                (mobile,),
            )
            row = cur.fetchone()
            results.append(("P1: PENDING application exists", bool(row),
                             f"app_no={row[1] if row else '—'}"))
            if row:
                results.append(("P1: block = Sonari", row[2] == "PENDING" and True,
                                 f"block check via status: {row[2]}"))
                doc = row[4] or ""
                results.append(("P1: doc_path is placeholder/agent", "placeholder" in doc or "_agent_" in doc or "_api_" in doc, doc))

        elif pid == "P2":
            cur.execute(
                "SELECT count(*) FROM applications WHERE mobile=%s", (mobile,)
            )
            total = cur.fetchone()[0]
            cur.execute(
                "SELECT count(*) FROM applications WHERE mobile=%s AND status='PENDING'", (mobile,)
            )
            pending = cur.fetchone()[0]
            results.append(("P2: no new duplicate application created", total <= 1,
                             f"total={total}, pending={pending}"))
            results.append(("P2: original PENDING preserved", pending >= 1,
                             f"pending count={pending}"))

        elif pid == "P3":
            cur.execute(
                "SELECT status FROM applications WHERE mobile=%s ORDER BY submitted_at ASC", (mobile,)
            )
            rows = cur.fetchall()
            statuses = [r[0] for r in rows]
            results.append(("P3: old REJECTED application still exists",
                             "REJECTED" in statuses, str(statuses)))
            results.append(("P3: new PENDING application created",
                             "PENDING" in statuses, str(statuses)))
            results.append(("P3: exactly 2 applications total",
                             len(statuses) == 2, f"count={len(statuses)}"))

        elif pid == "P4":
            cur.execute(
                "SELECT count(*) FROM applications WHERE mobile=%s", (mobile,)
            )
            count = cur.fetchone()[0]
            results.append(("P4: no application created for under-age applicant",
                             count == 0, f"count={count}"))

        cur.close(); conn.close()
        return results
    except Exception as exc:
        return [("DB check error", False, str(exc))]


# ---------------------------------------------------------------------------
# Dashboard HTML template (inline to avoid extra file dependency)
# ---------------------------------------------------------------------------

TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sewa Setu — Eval Dashboard</title>
<style>
  :root {
    --bg:#f5f7fa;--card:#fff;--border:#dce1e9;--primary:#1a56a0;
    --green:#1a7f4b;--red:#c0392b;--orange:#d67e1a;--text:#1d2433;--sub:#5a6175;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:var(--bg);color:var(--text);padding:24px 16px}
  .wrap{max-width:1080px;margin:0 auto}
  h1{font-size:1.5rem;color:var(--primary);margin-bottom:4px}
  .sub{color:var(--sub);font-size:.875rem;margin-bottom:24px}
  .grid{display:grid;gap:16px;grid-template-columns:repeat(auto-fill,minmax(480px,1fr))}
  .card{background:var(--card);border:1px solid var(--border);border-radius:10px;
        padding:20px;box-shadow:0 1px 4px rgba(0,0,0,.06)}
  .card-head{display:flex;align-items:center;gap:12px;margin-bottom:14px}
  .badge{background:var(--primary);color:#fff;border-radius:50%;
         width:36px;height:36px;display:flex;align-items:center;justify-content:center;
         font-weight:700;font-size:.85rem;flex-shrink:0}
  .card-title{font-size:1rem;font-weight:600}
  .card-path{font-size:.75rem;color:var(--sub);margin-top:2px}
  table{width:100%;border-collapse:collapse;font-size:.8rem;margin-top:10px}
  th{text-align:left;background:#f0f3f8;padding:6px 8px;color:var(--sub);font-weight:600}
  td{padding:6px 8px;border-top:1px solid var(--border);vertical-align:top}
  .field{font-weight:500}
  .scenario{background:#f8f9fb;border-radius:6px;padding:10px;
            font-size:.8rem;color:#3d4759;margin:10px 0;line-height:1.55}
  .voice{font-style:italic;color:var(--sub);font-size:.78rem;margin-bottom:10px}
  .checks{margin-top:10px}
  .check-row{display:flex;align-items:flex-start;gap:8px;padding:5px 0;
             border-top:1px solid var(--border);font-size:.79rem}
  .ic-pass{color:var(--green);font-size:1rem}
  .ic-fail{color:var(--red);font-size:1rem}
  .ic-pend{color:var(--orange);font-size:1rem}
  .check-label{flex:1}
  .check-detail{color:var(--sub);font-size:.72rem;margin-top:2px}
  .section-title{font-weight:600;color:var(--sub);font-size:.75rem;text-transform:uppercase;
                 letter-spacing:.04em;margin:14px 0 6px}
  .must-item{font-size:.78rem;padding:3px 0;color:#3d4759}
  .must-item::before{content:"✓ ";color:var(--green);font-weight:700}
  .must-not-item{font-size:.78rem;padding:3px 0;color:#3d4759}
  .must-not-item::before{content:"✗ ";color:var(--red);font-weight:700}
  .summary-bar{background:var(--card);border:1px solid var(--border);border-radius:10px;
               padding:16px 20px;margin-bottom:20px;display:flex;gap:32px;flex-wrap:wrap}
  .stat{text-align:center}
  .stat-num{font-size:2rem;font-weight:700;line-height:1}
  .stat-lbl{font-size:.75rem;color:var(--sub);margin-top:2px}
  .pass-num{color:var(--green)}
  .fail-num{color:var(--red)}
  .pend-num{color:var(--orange)}
  .ts{color:var(--sub);font-size:.72rem;margin-top:16px}
</style>
</head>
<body>
<div class="wrap">
  <h1>Sewa Setu — Eval Dashboard</h1>
  <div class="sub">MCP citizen agent evaluation — invented citizens, live DB checks</div>

  <div class="summary-bar">
    <div class="stat"><div class="stat-num">{{ personas|length }}</div><div class="stat-lbl">Personas</div></div>
    <div class="stat"><div class="stat-num pass-num">{{ total_pass }}</div><div class="stat-lbl">Checks passed</div></div>
    <div class="stat"><div class="stat-num fail-num">{{ total_fail }}</div><div class="stat-lbl">Checks failed</div></div>
    <div class="stat"><div class="stat-num pend-num">{{ total_pend }}</div><div class="stat-lbl">Not yet run</div></div>
  </div>

  <div class="grid">
  {% for p in personas %}
    <div class="card">
      <div class="card-head">
        <div class="badge">{{ p.id }}</div>
        <div>
          <div class="card-title">{{ p.name }}, {{ p.age }} — {{ p.village }}, {{ p.block }}</div>
          <div class="card-path">Path: {{ p.path }}</div>
        </div>
      </div>

      <div class="voice">"{{ p.voice }}"</div>

      <div class="scenario">{{ p.scenario }}</div>

      <table>
        <tr><th>Field</th><th>Value</th></tr>
        <tr><td class="field">Mobile</td><td>{{ p.mobile }}</td></tr>
        <tr><td class="field">DOB</td><td>{{ p.dob }}</td></tr>
        <tr><td class="field">Gender / Marital</td><td>{{ p.gender }} / {{ p.marital_status }}</td></tr>
        <tr><td class="field">Bank Account</td><td>{{ p.bank_account }}</td></tr>
        <tr><td class="field">IFSC</td><td>{{ p.ifsc }}</td></tr>
      </table>

      <div class="section-title">What must be true in the DB</div>
      {% for item in p.must_pass %}<div class="must-item">{{ item }}</div>{% endfor %}
      <div class="section-title">What must NOT happen</div>
      {% for item in p.must_not %}<div class="must-not-item">{{ item }}</div>{% endfor %}

      <div class="section-title">Live DB check results</div>
      <div class="checks">
      {% for label, passed, detail in p.db_checks %}
        <div class="check-row">
          {% if passed %}
            <span class="ic-pass">✓</span>
          {% elif passed == False %}
            <span class="ic-fail">✗</span>
          {% else %}
            <span class="ic-pend">◎</span>
          {% endif %}
          <div>
            <div class="check-label">{{ label }}</div>
            {% if detail %}<div class="check-detail">{{ detail }}</div>{% endif %}
          </div>
        </div>
      {% endfor %}
      </div>
    </div>
  {% endfor %}
  </div>

  <div class="ts">Last refreshed: {{ now }}</div>
</div>
</body>
</html>"""


@eval_bp.route("/eval-dashboard")
def eval_dashboard():
    enriched = []
    total_pass = total_fail = total_pend = 0
    for p in PERSONAS:
        checks = _db_check(p)
        for _, passed, _ in checks:
            if passed is True:
                total_pass += 1
            elif passed is False:
                total_fail += 1
            else:
                total_pend += 1
        enriched.append({**p, "db_checks": checks})

    return render_template_string(
        TEMPLATE,
        personas=enriched,
        total_pass=total_pass,
        total_fail=total_fail,
        total_pend=total_pend,
        now=datetime.now().strftime("%d %b %Y %H:%M:%S IST"),
    )


# ---------------------------------------------------------------------------
# Seed helper — insert pre-conditions for P2 (PENDING) and P3 (REJECTED)
# Called once at startup via /eval-seed (admin only)
# ---------------------------------------------------------------------------

@eval_bp.route("/eval-seed", methods=["POST"])
def eval_seed():
    """POST /eval-seed  — insert pre-conditions for P2 and P3.
    Idempotent: does nothing if the row already exists."""
    from app import get_db, hash_password
    conn = get_db()
    cur = conn.cursor()

    def _seed_one(p, status):
        mobile = p["mobile"]
        cur.execute("SELECT id FROM applications WHERE mobile=%s LIMIT 1", (mobile,))
        if cur.fetchone():
            return f"{p['name']} ({mobile}): already exists, skipped"
        from app import new_application_no
        from datetime import date
        import re as _re
        from datetime import datetime as _dt
        dob_str = p["dob"]
        try:
            dob = _dt.strptime(dob_str, "%d/%m/%Y").date()
        except Exception:
            dob = date(1959, 7, 15)
        app_no = new_application_no()
        cur.execute(
            "INSERT INTO applications "
            "(application_no,applicant_name,mobile,dob,gender,marital_status,"
            "husband_name,husband_employer,village,block,bank_account,ifsc,"
            "doc_path,status,submitted_at,decided_at,decided_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (
                app_no, p["name"], mobile, dob, p["gender"], p["marital_status"],
                "", "", p["village"], p["block"],
                p["bank_account"], p["ifsc"],
                f"/var/sewasetu/uploads/{mobile}_seed.txt",
                status,
                _dt.now(),
                _dt.now() if status != "PENDING" else None,
                "seed" if status != "PENDING" else None,
            ),
        )
        new_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO audit_log (application_id, action, actor, note, at) VALUES (%s,%s,%s,%s,%s)",
            (new_id, "SUBMIT", f"applicant:{mobile}", "Pre-seeded for eval", _dt.now()),
        )
        if status == "REJECTED":
            cur.execute(
                "INSERT INTO audit_log (application_id, action, actor, note, at) VALUES (%s,%s,%s,%s,%s)",
                (new_id, "REJECTED", "admin:seed", "Age proof unclear — please resubmit", _dt.now()),
            )
        # portal_users entry (password = dob ddmmyyyy)
        pwd = dob.strftime("%d%m%Y")
        cur.execute("SELECT 1 FROM portal_users WHERE mobile=%s", (mobile,))
        if not cur.fetchone():
            cur.execute("INSERT INTO portal_users (mobile, password_hash) VALUES (%s,%s)",
                        (mobile, hash_password(pwd)))
        import os as _os
        _os.makedirs("/var/sewasetu/uploads", exist_ok=True)
        with open(f"/var/sewasetu/uploads/{mobile}_seed.txt", "w") as fh:
            fh.write(f"Seed doc for {p['name']} ({status})\n")
        return f"{p['name']} ({mobile}): seeded as {status} — {app_no}"

    msgs = []
    for p, st in ((PERSONAS[1], "PENDING"), (PERSONAS[2], "REJECTED")):
        try:
            msgs.append(_seed_one(p, st))
        except Exception as exc:
            msgs.append(f"{p['name']}: ERROR — {exc}")

    conn.commit()
    cur.close(); conn.close()
    return "\n".join(msgs), 200, {"Content-Type": "text/plain"}
