# -*- coding: utf-8 -*-
"""
Sewa Setu — MCP Citizen Server (Streamable HTTP, 2025-03-26)

Endpoint : POST /mcp/citizen
Auth     : Bearer JWT with scope=citizen  (portal OAuth or program token)
No-auth  : get_scheme_info, check_eligibility

A citizen's assistant connects here to help elderly applicants check eligibility,
file applications, view status, and withdraw pending applications.
The portal remains the system of record; every state change goes through the
same validate_application / create_application path used by the web form.
"""

import os
import json
import base64
from datetime import datetime
from flask import Blueprint, request, jsonify, Response

citizen_mcp = Blueprint("citizen_mcp", __name__)

MCP_VERSION = "2025-03-26"

# Pilot rollout: only these blocks accept applications through the agent channel.
# The Director specifically asked for a phased approach — the web portal still
# accepts all six blocks; the MCP is gated to this subset until proven at scale.
# To expand the pilot, add more block names here (must match schema.sql CHECK).
MCP_PILOT_BLOCKS = ["Sonari", "Rajapara", "Borgaon", "Namti"]

# ---------------------------------------------------------------------------
# Tool manifest
# ---------------------------------------------------------------------------

CITIZEN_TOOLS = [
    {
        "name": "get_scheme_info",
        "description": (
            "Return public information about the Sewa Setu Old Age Pension scheme: "
            "application window, eligibility criteria, required documents, "
            "covered blocks, and how to track an application. "
            "No authentication required."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "check_eligibility",
        "description": (
            "Check whether a person born on a given date is eligible for the pension "
            "(minimum age 60). Returns eligible/not-eligible and, if not eligible, "
            "when they will be. No authentication required."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "dob": {
                    "type": "string",
                    "description": "Date of birth in DD/MM/YYYY format, e.g. 15/08/1950",
                }
            },
            "required": ["dob"],
        },
    },
    {
        "name": "get_my_application",
        "description": (
            "Return the status and key details of the authenticated citizen's most "
            "recent pension application. Requires citizen authentication."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "submit_application",
        "description": (
            "Submit a new Old Age Pension application for the authenticated citizen. "
            "Requires name, date of birth, gender, marital status, village, block, "
            "bank account, and IFSC code. An age-proof document may be supplied as "
            "base64; if omitted a placeholder is stored so the citizen can bring the "
            "original to the block office. Sends an SMS confirmation. "
            "Requires citizen authentication."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "applicant_name": {
                    "type": "string",
                    "description": "Full name of the applicant (as on government ID)",
                },
                "dob": {
                    "type": "string",
                    "description": "Date of birth in DD/MM/YYYY format",
                },
                "gender": {
                    "type": "string",
                    "enum": ["M", "F", "O"],
                    "description": "M = Male, F = Female, O = Other",
                },
                "marital_status": {
                    "type": "string",
                    "enum": ["MARRIED", "WIDOWED", "UNMARRIED", "DIVORCED"],
                },
                "husband_name": {
                    "type": "string",
                    "description": "Spouse's name — required when marital_status is MARRIED or WIDOWED",
                },
                "village": {
                    "type": "string",
                    "description": "Village where the applicant lives",
                },
                "block": {
                    "type": "string",
                    "enum": MCP_PILOT_BLOCKS,
                    "description": (
                        "Administrative block of the applicant's address. "
                        "Note: the AI assistant channel currently covers only the pilot blocks. "
                        "For other blocks, applicants must use the web portal."
                    ),
                },
                "bank_account": {
                    "type": "string",
                    "description": "Bank account number, digits only, 9–18 digits",
                },
                "ifsc": {
                    "type": "string",
                    "description": "Bank branch IFSC code — 11 characters, e.g. SBIN0003077",
                },
                "document_type": {
                    "type": "string",
                    "enum": ["aadhaar", "birth_certificate", "voter_id", "pension_book", "other"],
                    "description": "Type of age-proof document",
                    "default": "aadhaar",
                },
                "document_base64": {
                    "type": "string",
                    "description": (
                        "Base64-encoded age-proof document (PDF / JPG / PNG, max 5 MB). "
                        "If omitted a text placeholder is stored; the citizen must present "
                        "the original at the block office before the file is processed."
                    ),
                },
            },
            "required": [
                "applicant_name", "dob", "gender", "marital_status",
                "village", "block", "bank_account", "ifsc",
            ],
        },
    },
    {
        "name": "withdraw_application",
        "description": (
            "Withdraw the authenticated citizen's current PENDING application so they "
            "can correct details and reapply. Only PENDING applications can be withdrawn. "
            "Requires citizen authentication."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _issuer():
    return os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")


def _require_citizen():
    """
    Verify Bearer token. Returns (mobile_str, None) on success or
    (None, error_string) on failure. Tries several audiences because
    program tokens use the issuer as audience while OAuth tokens use
    the resource URL supplied during the authorization request.
    """
    from oauth import verify_token
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None, "Missing Authorization header (Bearer token required)"
    token = auth[7:].strip()
    base = _issuer()
    for aud in (base + "/mcp/citizen", base, "http://localhost:8000",
                "http://localhost:8000/mcp/citizen"):
        claims = verify_token(token, aud)
        if claims:
            break
    else:
        return None, "Token is invalid, expired, or was issued for a different service"
    if claims.get("scope") != "citizen":
        return None, "Token scope must be 'citizen'"
    sub = claims.get("sub", "")
    if not sub.startswith("citizen:"):
        return None, "Token subject is not a citizen account"
    return sub.split(":", 1)[1], None


# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------

def _ok(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _rpc_err(id_, code, msg):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": msg}}


def _tool_text(id_, text, is_error=False):
    return _ok(id_, {"content": [{"type": "text", "text": text}], "isError": is_error})


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _t_scheme_info():
    from app import SCHEME_DEADLINE, MIN_AGE, BLOCKS, deadline_remaining
    rem = deadline_remaining()
    dl = SCHEME_DEADLINE.strftime("%d %B %Y, %H:%M IST")
    window = f"OPEN — closes {dl} ({rem} hours remaining)" if rem else f"CLOSED (deadline was {dl})"
    return f"""SEWA SETU — OLD AGE PENSION SCHEME
Government of Purvanchal, Department of Social Welfare

APPLICATION WINDOW  : {window}

ELIGIBILITY RULES:
  • Age {MIN_AGE} years or older as of today's date
  • Resident of Purvanchal state
  • Only one active application (PENDING / APPROVED) per mobile number

AGE-PROOF DOCUMENTS ACCEPTED (any one):
  • Aadhaar Card
  • Birth Certificate
  • Voter ID (must show date of birth)
  • Pension Book from another government scheme

BLOCKS COVERED BY THIS ASSISTANT (pilot):
  {', '.join(MCP_PILOT_BLOCKS)}
  (For other blocks, applicants must use the web portal at /apply)

WHAT YOU NEED TO APPLY:
  1. 10-digit mobile number (receives OTP for web portal, or used as citizen ID here)
  2. Full name and date of birth (DD/MM/YYYY)
  3. Gender and marital status
  4. Village name and block
  5. Bank account number and IFSC code
  6. Age-proof document (can be submitted later at block office if not available now)

TRACKING YOUR APPLICATION:
  • Status portal: /status  — login with mobile + DOB as DDMMYYYY (e.g. 15081950)
  • Through this assistant: use get_my_application (requires login)

DECISION SLA: 15 days. Applications not decided within 15 days are automatically
deemed approved under the Purvanchal Right to Public Services Act."""


def _t_check_eligibility(dob_str):
    from app import parse_dob, age_on, MIN_AGE, now_ist
    dob = parse_dob(dob_str)
    if dob is None:
        return None, "Invalid date. Use DD/MM/YYYY format, e.g. 15/08/1950"
    today = now_ist().date()
    age = age_on(dob, today)
    if age >= MIN_AGE:
        return True, f"ELIGIBLE ✓\nAge: {age} years (born {dob.strftime('%d/%m/%Y')})\nMeets the minimum age of {MIN_AGE} years."
    yrs = MIN_AGE - age
    eligible_on = dob.replace(year=dob.year + MIN_AGE)
    return False, (f"NOT YET ELIGIBLE ✗\nAge: {age} years (born {dob.strftime('%d/%m/%Y')})\n"
                   f"Will be eligible on {eligible_on.strftime('%d/%m/%Y')} — {yrs} more year{'s' if yrs > 1 else ''} to go.")


def _t_get_my_application(mobile):
    from app import get_db
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT application_no, applicant_name, dob, gender, marital_status, "
        "village, block, status, submitted_at, decided_at, decided_by "
        "FROM applications WHERE mobile = %s ORDER BY submitted_at DESC LIMIT 1",
        (mobile,),
    )
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        return (False,
                "No application found for your mobile number.\n"
                "Use submit_application to file a new application.")
    app_no, name, dob, gender, marital, village, block, status, sub_at, dec_at, dec_by = row
    labels = {
        "PENDING":        "⏳ PENDING — awaiting review by block officer",
        "APPROVED":       "✅ APPROVED — pension approved",
        "DEEMED_APPROVED":"✅ DEEMED APPROVED — auto-approved after 15-day SLA",
        "REJECTED":       "❌ REJECTED",
        "WITHDRAWN":      "↩️ WITHDRAWN",
    }
    lines = [
        f"Application No : {app_no}",
        f"Status         : {labels.get(status, status)}",
        f"Name           : {name}",
        f"Date of Birth  : {dob.strftime('%d/%m/%Y') if dob else '—'}",
        f"Village/Block  : {village}, {block}",
        f"Submitted      : {sub_at.strftime('%d/%m/%Y %H:%M') if sub_at else '—'}",
    ]
    if dec_at:
        lines.append(f"Decided        : {dec_at.strftime('%d/%m/%Y %H:%M')} by {dec_by or '—'}")
    if status in ("REJECTED", "WITHDRAWN"):
        lines.append("\nYou may submit a new application using submit_application.")
    elif status == "PENDING":
        lines.append("\nUse withdraw_application to cancel this application and reapply with corrections.")
    return (True, "\n".join(lines))


def _t_submit_application(mobile, args):
    from app import (get_db, validate_application, create_application,
                     active_application, UPLOAD_DIR, send_sms)
    # Pilot gate — only enabled blocks can submit through the agent channel
    block = args.get("block", "")
    if block not in MCP_PILOT_BLOCKS:
        return (False,
                f"'{block}' is not yet covered by the AI assistant pilot. "
                f"Pilot blocks: {', '.join(MCP_PILOT_BLOCKS)}. "
                f"Please use the web portal at /apply for other blocks.")

    conn = get_db()
    cur = conn.cursor()
    existing = active_application(cur, mobile)
    cur.close(); conn.close()
    if existing:
        return (False,
                f"An active application already exists: {existing[1]} (status: {existing[2]}).\n"
                "Use withdraw_application first if you need to reapply.")

    # ---- document -------------------------------------------------------
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    doc_b64 = args.get("document_base64", "")
    doc_type = args.get("document_type", "aadhaar")
    if doc_b64:
        try:
            data = base64.b64decode(doc_b64)
            if data[:4] == b"%PDF":
                ext = "pdf"
            elif data[:2] == b"\xff\xd8":
                ext = "jpg"
            elif data[:8] == b"\x89PNG\r\n\x1a\n":
                ext = "png"
            else:
                ext = "bin"
            fname = f"{mobile}_api_{doc_type}.{ext}"
            doc_path = os.path.join(UPLOAD_DIR, fname)
            with open(doc_path, "wb") as fh:
                fh.write(data)
        except Exception as exc:
            return (False, f"Could not decode document: {exc}\nPlease submit the document at the block office.")
    else:
        fname = f"{mobile}_agent_placeholder_{doc_type}.txt"
        doc_path = os.path.join(UPLOAD_DIR, fname)
        with open(doc_path, "w") as fh:
            fh.write(
                f"Age-proof placeholder — submitted via AI assistant.\n"
                f"Document type : {doc_type}\n"
                f"Mobile        : {mobile}\n"
                f"Timestamp     : {datetime.now().isoformat()}\n"
                f"Action needed : Applicant must present the original document "
                f"({doc_type}) at the block office before the application is processed.\n"
            )

    errors, cleaned = validate_application(args, doc_path)
    if errors:
        return (False, "Could not submit — please correct the following:\n• " + "\n• ".join(errors))

    conn = get_db()
    cur = conn.cursor()
    try:
        new_id, app_no = create_application(cur, mobile, cleaned)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        return (False, f"Database error: {exc}")
    finally:
        cur.close(); conn.close()

    send_sms(
        mobile,
        f"Sewa Setu: Your pension application {app_no} was submitted via the AI assistant. "
        f"Track at /status — login with mobile and DOB as DDMMYYYY.",
    )
    doc_note = "" if doc_b64 else (
        "\n\n⚠️  No age-proof document was uploaded. "
        "Please bring the original to your block office before the 15-day review window closes."
    )
    return (True,
            f"Application submitted ✓\n\n"
            f"Application No : {app_no}\n"
            f"Name           : {cleaned['applicant_name']}\n"
            f"Village/Block  : {cleaned['village']}, {cleaned['block']}\n"
            f"Status         : PENDING\n"
            f"\nSMS sent to {mobile}.\n"
            f"To track: go to /status — mobile {mobile}, password = DOB as DDMMYYYY."
            f"{doc_note}")


def _t_withdraw_application(mobile):
    from app import get_db
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, application_no FROM applications "
        "WHERE mobile = %s AND status = 'PENDING' ORDER BY submitted_at DESC LIMIT 1",
        (mobile,),
    )
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return (False, "No PENDING application found. Only PENDING applications can be withdrawn.")
    app_id, app_no = row
    cur.execute(
        "UPDATE applications SET status='WITHDRAWN', decided_at=%s, decided_by='APPLICANT:AGENT' "
        "WHERE id=%s AND status='PENDING' RETURNING application_no",
        (datetime.now(), app_id),
    )
    done = cur.fetchone()
    if done:
        cur.execute(
            "INSERT INTO audit_log (application_id, action, actor, note, at) "
            "VALUES (%s,'WITHDRAW',%s,'Withdrawn via AI assistant',%s)",
            (app_id, f"applicant:{mobile}", datetime.now()),
        )
        conn.commit()
        cur.close(); conn.close()
        return (True, f"Application {app_no} withdrawn ✓\nYou may now submit a new application.")
    cur.close(); conn.close()
    return (False, "Could not withdraw — application may have already been decided.")


# ---------------------------------------------------------------------------
# MCP dispatcher
# ---------------------------------------------------------------------------

def _dispatch(msg):
    method = msg.get("method", "")
    id_ = msg.get("id")
    params = msg.get("params") or {}

    # Notifications are fire-and-forget; no response expected
    if id_ is None and method.startswith("notifications/"):
        return None

    if method == "initialize":
        return _ok(id_, {
            "protocolVersion": MCP_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {
                "name": "seva-setu-citizen",
                "version": "1.0",
                "description": "Sewa Setu — Citizen AI Agent MCP (Old Age Pension Portal)",
            },
        })

    if method == "tools/list":
        return _ok(id_, {"tools": CITIZEN_TOOLS})

    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}

        # Public (no auth)
        if name == "get_scheme_info":
            return _tool_text(id_, _t_scheme_info())
        if name == "check_eligibility":
            ok, txt = _t_check_eligibility(args.get("dob", ""))
            if ok is None:
                return _tool_text(id_, txt, is_error=True)
            return _tool_text(id_, txt, is_error=not ok)

        # Requires citizen token
        mobile, err = _require_citizen()
        if err:
            return _rpc_err(id_, -32001, "Authentication required — " + err)

        if name == "get_my_application":
            ok, txt = _t_get_my_application(mobile)
            return _tool_text(id_, txt, is_error=not ok)
        if name == "submit_application":
            ok, txt = _t_submit_application(mobile, args)
            return _tool_text(id_, txt, is_error=not ok)
        if name == "withdraw_application":
            ok, txt = _t_withdraw_application(mobile)
            return _tool_text(id_, txt, is_error=not ok)

        return _rpc_err(id_, -32602, f"Unknown tool: {name}")

    if method == "ping":
        return _ok(id_, {})

    return _rpc_err(id_, -32601, f"Method not found: {method}")


# ---------------------------------------------------------------------------
# Flask route
# ---------------------------------------------------------------------------

def _cors_headers():
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type, Accept, Mcp-Session-Id",
    }


@citizen_mcp.route("/mcp/citizen", methods=["GET", "POST", "DELETE", "OPTIONS"])
def citizen_endpoint():
    if request.method == "OPTIONS":
        return Response("", status=204, headers=_cors_headers())

    if request.method == "GET":
        # Minimal SSE keep-alive for clients that open a server-sent event channel
        def _stream():
            yield 'event: message\ndata: {"jsonrpc":"2.0","method":"ping"}\n\n'
        return Response(_stream(), content_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", **_cors_headers()})

    if request.method == "DELETE":
        return Response("", status=200, headers=_cors_headers())

    # POST — main JSON-RPC path
    try:
        body = request.get_json(force=True, silent=False)
    except Exception:
        return jsonify(_rpc_err(None, -32700, "Parse error")), 400
    if body is None:
        return jsonify(_rpc_err(None, -32600, "Empty request body")), 400

    batch = isinstance(body, list)
    msgs = body if batch else [body]
    responses = [r for r in map(_dispatch, msgs) if r is not None]

    if not responses:
        return Response("", status=202, headers=_cors_headers())

    payload = responses if batch else responses[0]
    resp = jsonify(payload)
    for k, v in _cors_headers().items():
        resp.headers[k] = v
    return resp
