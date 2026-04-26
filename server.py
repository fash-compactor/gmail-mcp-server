"""
Gmail MCP Server
Provides tools for reading unread emails and creating draft replies via Gmail API.
"""

import asyncio
import base64
import os
import re
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/documents.readonly",
]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(BASE_DIR, "token.json")
CREDENTIALS_PATH = os.path.join(BASE_DIR, "credentials.json")

# Optional: set STYLE_GUIDE_DOC_ID env var to enable the get_style_guide tool
STYLE_GUIDE_DOC_ID = os.environ.get("STYLE_GUIDE_DOC_ID", "")


def get_credentials() -> Credentials:
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_PATH):
                raise FileNotFoundError(
                    f"credentials.json not found at {CREDENTIALS_PATH}. "
                    "Download it from Google Cloud Console → APIs & Services → Credentials."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return creds


def get_gmail_service():
    return build("gmail", "v1", credentials=get_credentials())


def get_docs_service():
    return build("docs", "v1", credentials=get_credentials())


def _extract_body(message: dict) -> str:
    """Pull plain-text body from a full Gmail message object."""

    def _decode(data: str) -> str:
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")

    payload = message.get("payload", {})

    def _walk(part):
        mime = part.get("mimeType", "")
        if mime == "text/plain":
            data = part.get("body", {}).get("data", "")
            return _decode(data) if data else ""
        if mime.startswith("multipart/"):
            for sub in part.get("parts", []):
                result = _walk(sub)
                if result:
                    return result
        return ""

    body = _walk(payload)
    if not body:
        # Fall back to snippet (first 200 chars from Gmail index)
        body = message.get("snippet", "")
    return body[:3000]


def _header(message: dict, name: str) -> str:
    for h in message.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


# ── MCP Server ────────────────────────────────────────────────────────────────

server = Server("gmail-mcp-server")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    tools = [
        types.Tool(
            name="get_unread_emails",
            description=(
                "Retrieve unread emails from the Gmail inbox. "
                "Returns sender, subject, date, body (up to 3000 chars), "
                "message_id, and thread_id for each email."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "max_results": {
                        "type": "integer",
                        "description": "Max emails to return (1–50, default 10).",
                        "default": 10,
                    }
                },
            },
        ),
        types.Tool(
            name="create_draft_reply",
            description=(
                "Create a draft reply to a Gmail email. "
                "The draft is saved in Drafts — NOT sent automatically. "
                "Use message_id and thread_id from get_unread_emails."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Gmail message ID of the email being replied to.",
                    },
                    "thread_id": {
                        "type": "string",
                        "description": "Gmail thread ID to keep the reply threaded.",
                    },
                    "reply_body": {
                        "type": "string",
                        "description": "Plain-text body of the reply.",
                    },
                },
                "required": ["message_id", "thread_id", "reply_body"],
            },
        ),
    ]

    if STYLE_GUIDE_DOC_ID:
        tools.append(
            types.Tool(
                name="get_style_guide",
                description=(
                    "Fetch the email style guide from a Google Doc. "
                    "Use this before drafting replies to match the expected tone and format."
                ),
                inputSchema={"type": "object", "properties": {}},
            )
        )

    return tools


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.Content]:
    if name == "get_unread_emails":
        return await _get_unread_emails(arguments)
    if name == "create_draft_reply":
        return await _create_draft_reply(arguments)
    if name == "get_style_guide":
        return await _get_style_guide()
    raise ValueError(f"Unknown tool: {name}")


# ── Tool implementations ──────────────────────────────────────────────────────


async def _get_unread_emails(arguments: dict) -> list[types.Content]:
    max_results = max(1, min(int(arguments.get("max_results", 10)), 50))

    try:
        service = get_gmail_service()
        result = (
            service.users()
            .messages()
            .list(userId="me", labelIds=["INBOX", "UNREAD"], maxResults=max_results)
            .execute()
        )
    except HttpError as e:
        return [types.TextContent(type="text", text=f"Gmail API error: {e}")]

    messages = result.get("messages", [])
    if not messages:
        return [types.TextContent(type="text", text="No unread emails in inbox.")]

    lines = [f"Found {len(messages)} unread email(s):\n"]
    for i, msg_ref in enumerate(messages, 1):
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=msg_ref["id"], format="full")
            .execute()
        )
        lines.append("=" * 60)
        lines.append(f"Email {i} of {len(messages)}")
        lines.append(f"From:       {_header(msg, 'From')}")
        lines.append(f"Subject:    {_header(msg, 'Subject')}")
        lines.append(f"Date:       {_header(msg, 'Date')}")
        lines.append(f"Message ID: {msg['id']}")
        lines.append(f"Thread ID:  {msg['threadId']}")
        lines.append(f"Body:\n{_extract_body(msg)}")
        lines.append("")

    return [types.TextContent(type="text", text="\n".join(lines))]


async def _create_draft_reply(arguments: dict) -> list[types.Content]:
    message_id = arguments["message_id"]
    thread_id = arguments["thread_id"]
    reply_body = arguments["reply_body"]

    try:
        service = get_gmail_service()

        original = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From", "Reply-To", "Subject", "Message-ID", "To"],
            )
            .execute()
        )
    except HttpError as e:
        return [types.TextContent(type="text", text=f"Failed to fetch original email: {e}")]

    reply_to = _header(original, "Reply-To") or _header(original, "From")
    original_subject = _header(original, "Subject")
    original_message_id = _header(original, "Message-ID")

    subject = original_subject if original_subject.lower().startswith("re:") else f"Re: {original_subject}"

    profile = service.users().getProfile(userId="me").execute()
    user_email = profile["emailAddress"]

    mime_msg = MIMEMultipart()
    mime_msg["To"] = reply_to
    mime_msg["From"] = user_email
    mime_msg["Subject"] = subject
    if original_message_id:
        mime_msg["In-Reply-To"] = original_message_id
        mime_msg["References"] = original_message_id

    mime_msg.attach(MIMEText(reply_body, "plain", "utf-8"))

    raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode("utf-8")

    try:
        draft = (
            service.users()
            .drafts()
            .create(
                userId="me",
                body={"message": {"raw": raw, "threadId": thread_id}},
            )
            .execute()
        )
    except HttpError as e:
        return [types.TextContent(type="text", text=f"Failed to create draft: {e}")]

    text = (
        f"Draft created successfully.\n\n"
        f"Draft ID:  {draft['id']}\n"
        f"To:        {reply_to}\n"
        f"Subject:   {subject}\n"
        f"Thread ID: {thread_id}\n\n"
        f"Open Gmail Drafts to review and send."
    )
    return [types.TextContent(type="text", text=text)]


async def _get_style_guide() -> list[types.Content]:
    if not STYLE_GUIDE_DOC_ID:
        return [types.TextContent(type="text", text="STYLE_GUIDE_DOC_ID is not configured.")]

    try:
        docs = get_docs_service()
        doc = docs.documents().get(documentId=STYLE_GUIDE_DOC_ID).execute()
    except HttpError as e:
        return [types.TextContent(type="text", text=f"Could not fetch style guide: {e}")]

    # Extract plain text from the Doc's structural elements
    text_parts = []
    for element in doc.get("body", {}).get("content", []):
        paragraph = element.get("paragraph")
        if not paragraph:
            continue
        for run in paragraph.get("elements", []):
            text_run = run.get("textRun")
            if text_run:
                text_parts.append(text_run.get("content", ""))

    content = "".join(text_parts).strip()
    if not content:
        return [types.TextContent(type="text", text="Style guide document appears to be empty.")]

    return [types.TextContent(type="text", text=f"Email Style Guide:\n\n{content}")]


# ── Entry point ───────────────────────────────────────────────────────────────


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
