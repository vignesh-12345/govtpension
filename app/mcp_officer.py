# -*- coding: utf-8 -*-
"""
Sewa Setu — MCP Officer Server (Streamable HTTP, 2025-03-26)

Endpoint : POST /mcp/officer
Auth     : Bearer JWT with scope=officer  (OAuth or admin-issued program token)

A block officer's assistant connects here to review pending applications,
approve or reject them, and monitor the queue. Citizens never see this endpoint.
The portal remains the system of record; every decision goes through the same
code path as the web admin UI.
"""

import os
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, Response

officer_mcp = Blueprint("officer_mcp", __name__)

MCP_VERSION = "2025-03-26"

# ---------------------------------------------------------------------------
# Tool manifest
# ---------------------------------------------------------------------------

OFFICER_TOOLS = [
    {
        "name": "get_dashboard_stats",
        "description": (
            "Return high-level statistics: application counts by status, "
            "applications past the 15-day SLA (overdue), and counts per block. "
            "Requires officer authentication."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_applications",
        "description": (
            "List applications with optional filters. Returns up to 50 per page. "
            "Requires officer authentication."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["PENDING", "APPROVED", "DEEMED_APPROVED", "REJECTED", "WITHDRAWN"],
                    "description": "Filter by status (default: PENDING)",
                    "default": "PENDING",
                },
                "block": {
                    "type": "string",
                    "enum": ["Sonari", "Rajapara", "Dhemaji Pathar", "Borgaon", "Namti", "Khelua"],
                    "description": "Filter by block (optional)",
                },
                "page": {
                    "type": "integer",
                    "description": "Page number, starting from 1 (default: 1)",
                    "default": 1,
                    "minimum": 1,
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_application_details",
        "description": (
            "Return full details of one application — personal info, bank details, "
            "document path, current status, and complete audit trail. "
            "Requires officer authentication."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "application_id": {
                    "type": "integer",
                    "description": "The numeric database ID of the application (from list_applications)",
                },
                "application_no": {
                    "type": "string",
                    "description": "The human-readable application number (e.g. SSP26123456). "
                                   "Used if application_id is not known.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "approve_application",
        "description": (
            "Approve a PENDING application. Records the decision with a timestamp "
            "and optional note, then sends an SMS to the applicant. "
            "Requires officer authentication."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "application_id": {
                    "type": "integer",
                    "description": "Database ID of the application to approve",
                },
                "note": {
                    "type": "string",
                    "description": "Optional note for the audit log (max 500 characters)",
                },
            },
            "required": ["application_id"],
        },
    },
    {
        "name": "reject_application",
        "description": (
            "Reject a PENDING application with a mandatory reason. Records the "
            "decision in the audit log and sends an SMS to the applicant. "
            "Requires officer authentication."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "application_id": {
                    "type": "integer",
                    "description": "Database ID of the application to reject",
                },
                "reason": {
                    "type": "string",
                    "description": "Reason for rejection — required and shown in the audit log",
                },
            },
            "required": ["application_id", "reason"],
        },
    },
    {
        "name": "search_by_mobile",
        "description": (
            "Find all applications submitted from a given mobile number, "
            "ordered oldest first. Useful to check an applicant's history. "
            "Requires officer authentication."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mobile": {
                    "type": "string",
                    "description": "10-digit mobile number",
                }
            },
            "required": ["mobile"],
        },
    },
    {
        "name": "list_overdue",
        "description": (
            "List PENDING applications that have exceeded the 15-day SLA, "
            "ordered by how long they have been waiting (oldest first). "
            "Requires officer authentication."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "block": {
                    "type": "string",
                    "enum": ["Sonari", "Rajapara", "Dhemaji Pathar", "Borgaon", "Namti", "Khelua"],
                    "description": "Optionally narrow to one block",
                }
            },
            "required": [],
        },
    },
]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _issuer():
    return os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")


def _require_officer():
    """
    Verify Bearer token. Returns (actor_str, None) on success or
    (None, error_message) on failure.
    """
    from oauth import verify_token
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None, "Missing Authorization header (Bearer token required)"
    token = auth[7:].strip()
    base = _issuer()
    for aud in (base + "/mcp/officer", base, "http://localhost:8000",
                "http://localhost:8000/mcp/officer"):
        claims = verify_token(token, aud)
        if claims:
            break
    else:
        return None, "Token is invalid, expired, or was issued for a different service"
    if claims.get("scope") != "officer":
        return None, "Token scope must be 'officer'"
    return claims.get("sub", "officer:unknown"), None


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

def _t_dashboard_stats():
    from app import get_db, SLA_DAYS
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT status, count(*) FROM applications GROUP BY status ORDER BY status")
    by_status = cur.fetchall()
    cur.execute(
        "SELECT count(*) FROM applications WHERE status='PENDING' AND submitted_at < %s",
        (datetime.now() - timedelta(days=SLA_DAYS),),
    )
    overdue = cur.fetchone()[0]
    cur.execute(
        "SELECT block, count(*) FROM applications WHERE status='PENDING' "
        "GROUP BY block ORDER BY count(*) DESC"
    )
    by_block = cur.fetchall()
    total = sum(c for _, c in by_status)
    cur.close(); conn.close()

    lines = ["DASHBOARD — Sewa Setu Old Age Pension", "=" * 40, ""]
    lines.append("Applications by status:")
    for status, cnt in by_status:
        lines.append(f"  {status:<20} {cnt:>6}")
    lines.append(f"  {'TOTAL':<20} {total:>6}")
    lines.append("")
    lines.append(f"⚠️  Overdue (>{SLA_DAYS}d SLA) : {overdue}")
    lines.append("")
    lines.append("Pending by block:")
    for blk, cnt in by_block:
        lines.append(f"  {blk:<25} {cnt:>4}")
    return "\n".join(lines)


def _t_list_applications(status, block, page):
    from app import get_db
    conn = get_db()
    cur = conn.cursor()
    offset = (max(1, page) - 1) * 50
    if block:
        cur.execute(
            "SELECT id, application_no, applicant_name, mobile, block, status, submitted_at "
            "FROM applications WHERE status=%s AND block=%s ORDER BY submitted_at ASC LIMIT 50 OFFSET %s",
            (status, block, offset),
        )
    else:
        cur.execute(
            "SELECT id, application_no, applicant_name, mobile, block, status, submitted_at "
            "FROM applications WHERE status=%s ORDER BY submitted_at ASC LIMIT 50 OFFSET %s",
            (status, offset),
        )
    rows = cur.fetchall()
    cur.close(); conn.close()
    if not rows:
        return (True, f"No applications found (status={status}{', block='+block if block else ''}, page={page}).")
    lines = [f"Applications — status={status}{', block='+block if block else ''}, page={page}", ""]
    for row in rows:
        id_, app_no, name, mobile, blk, st, sub_at = row
        sub = sub_at.strftime("%d/%m/%Y") if sub_at else "—"
        lines.append(f"ID {id_:>6}  {app_no:<14}  {name:<25}  {mobile}  {blk:<18}  {sub}")
    lines.append(f"\n{len(rows)} record(s). Use get_application_details with 'application_id' for full info.")
    return (True, "\n".join(lines))


def _t_get_application_details(app_id, app_no):
    from app import get_db
    conn = get_db()
    cur = conn.cursor()
    if app_id:
        cur.execute(
            "SELECT id, application_no, applicant_name, mobile, dob, gender, marital_status, "
            "husband_name, village, block, bank_account, ifsc, doc_path, status, "
            "submitted_at, decided_at, decided_by FROM applications WHERE id=%s", (app_id,)
        )
    elif app_no:
        cur.execute(
            "SELECT id, application_no, applicant_name, mobile, dob, gender, marital_status, "
            "husband_name, village, block, bank_account, ifsc, doc_path, status, "
            "submitted_at, decided_at, decided_by FROM applications WHERE application_no=%s", (app_no,)
        )
    else:
        cur.close(); conn.close()
        return (False, "Provide either application_id or application_no.")
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return (False, "Application not found.")
    (rid, rno, name, mobile, dob, gender, marital, husband, village, block,
     bank, ifsc, doc_path, status, sub_at, dec_at, dec_by) = row
    cur.execute(
        "SELECT at, action, actor, note FROM audit_log WHERE application_id=%s ORDER BY at ASC",
        (rid,),
    )
    audit = cur.fetchall()
    cur.close(); conn.close()

    lines = [
        f"APPLICATION DETAILS — {rno}",
        "=" * 40,
        f"DB ID          : {rid}",
        f"Status         : {status}",
        f"Name           : {name}",
        f"Mobile         : {mobile}",
        f"Date of Birth  : {dob.strftime('%d/%m/%Y') if dob else '—'}",
        f"Gender         : {gender}",
        f"Marital Status : {marital}",
        f"Spouse Name    : {husband or '—'}",
        f"Village        : {village}",
        f"Block          : {block}",
        f"Bank Account   : {bank}",
        f"IFSC           : {ifsc}",
        f"Document       : {doc_path or '—'}",
        f"Submitted      : {sub_at.strftime('%d/%m/%Y %H:%M') if sub_at else '—'}",
        f"Decided        : {dec_at.strftime('%d/%m/%Y %H:%M') if dec_at else '—'}",
        f"Decided By     : {dec_by or '—'}",
        "",
        "AUDIT TRAIL:",
    ]
    for at, action, actor, note in audit:
        ts = at.strftime("%d/%m/%Y %H:%M") if at else "—"
        lines.append(f"  {ts}  {action:<12}  {actor:<30}  {note or ''}")
    return (True, "\n".join(lines))


def _t_decide(app_id, new_status, note_text, actor):
    from app import get_db, send_sms
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE applications SET status=%s, decided_at=%s, decided_by=%s "
        "WHERE id=%s AND status='PENDING' "
        "RETURNING application_no, applicant_name, mobile",
        (new_status, datetime.now(), actor, app_id),
    )
    row = cur.fetchone()
    if row:
        app_no, name, mobile = row
        cur.execute(
            "INSERT INTO audit_log (application_id, action, actor, note, at) "
            "VALUES (%s,%s,%s,%s,%s)",
            (app_id, new_status, f"officer:{actor}", note_text[:500], datetime.now()),
        )
        conn.commit()
        cur.close(); conn.close()
        verb = "approved" if new_status == "APPROVED" else "rejected"
        send_sms(mobile,
                 f"Sewa Setu: Your pension application {app_no} has been {verb}. "
                 f"Check details at the status portal.")
        return (True,
                f"Application {app_no} ({name}) {new_status} ✓\n"
                f"Decision recorded at {datetime.now().strftime('%d/%m/%Y %H:%M')} by {actor}.")
    cur.close(); conn.close()
    return (False,
            f"Could not update application {app_id} — it may not be in PENDING status, "
            "or may not exist.")


def _t_search_by_mobile(mobile):
    from app import get_db
    if len(mobile) != 10 or not mobile.isdigit():
        return (False, "Please provide a valid 10-digit mobile number.")
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, application_no, applicant_name, status, submitted_at "
        "FROM applications WHERE mobile=%s ORDER BY submitted_at ASC",
        (mobile,),
    )
    rows = cur.fetchall()
    cur.close(); conn.close()
    if not rows:
        return (True, f"No applications found for mobile {mobile}.")
    lines = [f"Applications for mobile {mobile}:", ""]
    for id_, app_no, name, status, sub_at in rows:
        sub = sub_at.strftime("%d/%m/%Y") if sub_at else "—"
        lines.append(f"  ID {id_:>6}  {app_no:<14}  {name:<25}  {status:<15}  {sub}")
    return (True, "\n".join(lines))


def _t_list_overdue(block):
    from app import get_db, SLA_DAYS
    conn = get_db()
    cur = conn.cursor()
    cutoff = datetime.now() - timedelta(days=SLA_DAYS)
    if block:
        cur.execute(
            "SELECT id, application_no, applicant_name, mobile, block, submitted_at "
            "FROM applications WHERE status='PENDING' AND submitted_at<%s AND block=%s "
            "ORDER BY submitted_at ASC LIMIT 100",
            (cutoff, block),
        )
    else:
        cur.execute(
            "SELECT id, application_no, applicant_name, mobile, block, submitted_at "
            "FROM applications WHERE status='PENDING' AND submitted_at<%s "
            "ORDER BY submitted_at ASC LIMIT 100",
            (cutoff,),
        )
    rows = cur.fetchall()
    cur.close(); conn.close()
    if not rows:
        return (True, "No overdue applications found. Queue is within SLA ✓")
    lines = [
        f"OVERDUE APPLICATIONS (>{SLA_DAYS} days){', block='+block if block else ''}",
        f"Showing up to 100 oldest.", "",
    ]
    for id_, app_no, name, mobile, blk, sub_at in rows:
        sub = sub_at.strftime("%d/%m/%Y") if sub_at else "—"
        days_over = (datetime.now() - sub_at).days - SLA_DAYS if sub_at else "?"
        lines.append(f"  ID {id_:>6}  {app_no:<14}  {name:<25}  {blk:<18}  submitted {sub}  (+{days_over}d)")
    return (True, "\n".join(lines))


# ---------------------------------------------------------------------------
# MCP dispatcher
# ---------------------------------------------------------------------------

def _dispatch(msg):
    method = msg.get("method", "")
    id_ = msg.get("id")
    params = msg.get("params") or {}

    if id_ is None and method.startswith("notifications/"):
        return None

    if method == "initialize":
        return _ok(id_, {
            "protocolVersion": MCP_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {
                "name": "seva-setu-officer",
                "version": "1.0",
                "description": "Sewa Setu — Block Officer MCP (Old Age Pension Portal)",
            },
        })

    if method == "tools/list":
        return _ok(id_, {"tools": OFFICER_TOOLS})

    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}

        actor, err = _require_officer()
        if err:
            return _rpc_err(id_, -32001, "Authentication required — " + err)

        if name == "get_dashboard_stats":
            return _tool_text(id_, _t_dashboard_stats())

        if name == "list_applications":
            status = args.get("status", "PENDING")
            block = args.get("block")
            page = int(args.get("page", 1))
            ok, txt = _t_list_applications(status, block, page)
            return _tool_text(id_, txt, is_error=not ok)

        if name == "get_application_details":
            app_id = args.get("application_id")
            app_no = args.get("application_no")
            ok, txt = _t_get_application_details(app_id, app_no)
            return _tool_text(id_, txt, is_error=not ok)

        if name == "approve_application":
            app_id = args.get("application_id")
            if not app_id:
                return _tool_text(id_, "application_id is required", is_error=True)
            note = args.get("note", "Approved via officer AI assistant")
            ok, txt = _t_decide(app_id, "APPROVED", note, actor)
            return _tool_text(id_, txt, is_error=not ok)

        if name == "reject_application":
            app_id = args.get("application_id")
            reason = args.get("reason", "").strip()
            if not app_id:
                return _tool_text(id_, "application_id is required", is_error=True)
            if not reason:
                return _tool_text(id_, "reason is required for rejection", is_error=True)
            ok, txt = _t_decide(app_id, "REJECTED", reason, actor)
            return _tool_text(id_, txt, is_error=not ok)

        if name == "search_by_mobile":
            ok, txt = _t_search_by_mobile(args.get("mobile", ""))
            return _tool_text(id_, txt, is_error=not ok)

        if name == "list_overdue":
            ok, txt = _t_list_overdue(args.get("block"))
            return _tool_text(id_, txt, is_error=not ok)

        return _rpc_err(id_, -32602, f"Unknown tool: {name}")

    if method == "ping":
        return _ok(id_, {})

    return _rpc_err(id_, -32601, f"Method not found: {method}")


# ---------------------------------------------------------------------------
# Flask route
# ---------------------------------------------------------------------------

def _cors():
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type, Accept, Mcp-Session-Id",
    }


@officer_mcp.route("/mcp/officer", methods=["GET", "POST", "DELETE", "OPTIONS"])
def officer_endpoint():
    if request.method == "OPTIONS":
        return Response("", status=204, headers=_cors())

    if request.method == "GET":
        def _s():
            yield 'event: message\ndata: {"jsonrpc":"2.0","method":"ping"}\n\n'
        return Response(_s(), content_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", **_cors()})

    if request.method == "DELETE":
        return Response("", status=200, headers=_cors())

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
        return Response("", status=202, headers=_cors())

    payload = responses if batch else responses[0]
    resp = jsonify(payload)
    for k, v in _cors().items():
        resp.headers[k] = v
    return resp
