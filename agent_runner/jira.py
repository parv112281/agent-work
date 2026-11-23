"""Fetch Jira tickets with comments and attachments. Standard library only.

Connection settings come from the [jira] section of the config file.
Attachments are downloaded into <attachments_dir>/<ticket-key>/.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Iterable


DEFAULT_ACCEPTANCE_FIELD_NAMES = (
    "acceptance criteria",
    "acceptance criterion",
    "acceptance",
    "ac",
)


@dataclass(frozen=True)
class MessageExtract:
    author: str
    created: str
    updated: str
    body: str


@dataclass(frozen=True)
class AttachmentExtract:
    filename: str
    author: str
    created: str
    size: int
    mime_type: str
    content_url: str
    local_path: str


@dataclass(frozen=True)
class TicketExtract:
    key: str
    title: str
    description: str
    acceptance_criteria: str
    messages: list[MessageExtract]
    attachments: list[AttachmentExtract]
    url: str


class JiraError(RuntimeError):
    pass


class JiraClient:
    def __init__(self, base_url: str, token: str, timeout: int = 30) -> None:
        self.base_url = normalize_base_url(base_url)
        self.token = token
        self.timeout = timeout

    def build_url(self, path_or_url: str, params: dict[str, str] | None = None) -> str:
        if path_or_url.startswith(("http://", "https://")):
            url = path_or_url
        else:
            url = urllib.parse.urljoin(self.base_url, path_or_url)
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        return url

    def build_request(self, url: str, accept: str = "*/*") -> urllib.request.Request:
        return urllib.request.Request(
            url,
            headers={
                "Accept": accept,
                "Authorization": f"Bearer {self.token}",
            },
        )

    def get_json(self, path: str, params: dict[str, str] | None = None) -> Any:
        request = self.build_request(self.build_url(path, params), accept="application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return json.loads(response.read().decode(charset))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise JiraError(f"Jira request failed: {exc.code} {exc.reason}: {body}") from exc
        except urllib.error.URLError as exc:
            raise JiraError(f"Jira request failed: {exc.reason}") from exc

    def download_file(self, url: str, destination: str) -> None:
        request = self.build_request(url)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                with open(destination, "wb") as output:
                    shutil.copyfileobj(response, output)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise JiraError(f"Attachment download failed: {exc.code} {exc.reason}: {body}") from exc
        except urllib.error.URLError as exc:
            raise JiraError(f"Attachment download failed: {exc.reason}") from exc

    def get_fields(self) -> list[dict[str, Any]]:
        fields = self.get_json("field")
        if not isinstance(fields, list):
            raise JiraError("Unexpected Jira field response shape.")
        return fields

    def get_issue(self, key: str, fields: Iterable[str]) -> dict[str, Any]:
        field_list = ",".join(dict.fromkeys(fields))
        issue = self.get_json(f"issue/{urllib.parse.quote(key)}", {"fields": field_list})
        if not isinstance(issue, dict):
            raise JiraError(f"Unexpected Jira issue response shape for {key}.")
        return issue

    def get_comments(self, key: str) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []
        start_at = 0
        while True:
            response = self.get_json(
                f"issue/{urllib.parse.quote(key)}/comment",
                {"startAt": str(start_at), "maxResults": "100"},
            )
            page = response.get("comments") if isinstance(response, dict) else None
            if not isinstance(page, list):
                raise JiraError(f"Unexpected Jira comment response shape for {key}.")
            comments.extend(page)

            total = int(response.get("total", len(comments)))
            max_results = int(response.get("maxResults", len(page) or 100))
            start_at += max_results
            if start_at >= total or not page:
                return comments


def normalize_base_url(base_url: str) -> str:
    base_url = base_url.strip()
    if not base_url.endswith("/"):
        base_url += "/"
    return base_url


def browse_url(base_url: str, key: str) -> str:
    root = base_url.split("/rest/api/", 1)[0].rstrip("/")
    return f"{root}/browse/{key}"


def plain_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(filter(None, (plain_text(item) for item in value))).strip()
    if isinstance(value, dict):
        for key in ("value", "name", "displayName", "text"):
            if key in value:
                return plain_text(value[key])
        if "content" in value:
            return plain_text(value["content"])
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).strip()


def person_name(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("displayName", "name", "emailAddress", "key"):
        if value.get(key):
            return str(value[key])
    return ""


def extract_acceptance_from_description(description: str) -> str:
    if not description:
        return ""

    heading = re.compile(
        r"(?im)^\s*(?:h[1-6]\.\s*)?(?:#+\s*)?"
        r"(acceptance\s+criteria|acceptance\s+criterion|acceptance)\s*:?\s*$"
    )
    match = heading.search(description)
    if not match:
        inline = re.search(
            r"(?is)\bacceptance\s+criteria\s*:\s*(.+?)(?:\n\s*\n[A-Z][^\n:]{1,80}:\s*|\Z)",
            description,
        )
        return inline.group(1).strip() if inline else ""

    start = match.end()
    next_heading = re.search(r"(?m)^\s*(?:h[1-6]\.\s*|#+\s*)?[A-Z][^\n]{1,80}:?\s*$", description[start:])
    end = start + next_heading.start() if next_heading else len(description)
    return description[start:end].strip()


def find_acceptance_field_id(fields: list[dict[str, Any]]) -> str | None:
    candidates: list[tuple[int, str]] = []
    for field in fields:
        field_id = str(field.get("id", ""))
        name = str(field.get("name", "")).strip().lower()
        if not field_id or not name:
            continue
        if name in DEFAULT_ACCEPTANCE_FIELD_NAMES:
            candidates.append((0, field_id))
        elif "acceptance" in name and "criteria" in name:
            candidates.append((1, field_id))
        elif "acceptance" in name:
            candidates.append((2, field_id))
    return sorted(candidates)[0][1] if candidates else None


def normalize_keys(values: Iterable[str]) -> list[str]:
    keys: list[str] = []
    for value in values:
        for key in re.split(r"[\s,]+", value.strip()):
            key = key.lstrip("#").upper()
            if key and key not in keys:
                keys.append(key)
    return keys


WINDOWS_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$", re.I)


def safe_filename(value: str) -> str:
    """A filename that is valid on Windows, macOS and Linux and stays in its directory."""
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value.strip())
    value = re.sub(r"\s+", " ", value).rstrip(". ")
    if WINDOWS_RESERVED.match(value):
        value = f"_{value}"
    return value or "attachment"


def unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    directory, filename = os.path.split(path)
    stem, extension = os.path.splitext(filename)
    counter = 2
    while True:
        candidate = os.path.join(directory, f"{stem}-{counter}{extension}")
        if not os.path.exists(candidate):
            return candidate
        counter += 1


def extract_messages(issue_fields: dict[str, Any], comments: list[dict[str, Any]]) -> list[MessageExtract]:
    field_comment = issue_fields.get("comment")
    if not comments and isinstance(field_comment, dict) and isinstance(field_comment.get("comments"), list):
        comments = field_comment["comments"]
    return [
        MessageExtract(
            author=person_name(comment.get("author")),
            created=plain_text(comment.get("created")),
            updated=plain_text(comment.get("updated")),
            body=plain_text(comment.get("body")),
        )
        for comment in comments
    ]


def extract_attachments(
    client: JiraClient, issue_fields: dict[str, Any], ticket_key: str, attachments_dir: str
) -> list[AttachmentExtract]:
    attachments = issue_fields.get("attachment")
    if not isinstance(attachments, list):
        return []

    extracted = []
    ticket_dir = os.path.join(attachments_dir, safe_filename(ticket_key))
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        filename = safe_filename(plain_text(attachment.get("filename")))
        content_url = plain_text(attachment.get("content"))
        local_path = ""
        if content_url:
            local_path = unique_path(os.path.join(ticket_dir, filename))
            client.download_file(content_url, local_path)
        extracted.append(
            AttachmentExtract(
                filename=filename,
                author=person_name(attachment.get("author")),
                created=plain_text(attachment.get("created")),
                size=int(attachment.get("size") or 0),
                mime_type=plain_text(attachment.get("mimeType")),
                content_url=content_url,
                local_path=local_path,
            )
        )
    return extracted


def extract_ticket(client: JiraClient, key: str, acceptance_field: str | None, attachments_dir: str) -> TicketExtract:
    fields = ["summary", "description", "comment", "attachment"]
    if acceptance_field:
        fields.append(acceptance_field)

    issue = client.get_issue(key, fields)
    issue_fields = issue.get("fields") or {}
    issue_key = str(issue.get("key") or key)
    description = plain_text(issue_fields.get("description"))
    acceptance = plain_text(issue_fields.get(acceptance_field)) if acceptance_field else ""
    if not acceptance:
        acceptance = extract_acceptance_from_description(description)
    try:
        comments = client.get_comments(issue_key)
    except JiraError as exc:
        print(f"Warning: paginated comment fetch skipped for {issue_key}: {exc}", file=sys.stderr)
        comments = []

    return TicketExtract(
        key=issue_key,
        title=plain_text(issue_fields.get("summary")),
        description=description,
        acceptance_criteria=acceptance,
        messages=extract_messages(issue_fields, comments),
        attachments=extract_attachments(client, issue_fields, issue_key, attachments_dir),
        url=browse_url(client.base_url, issue_key),
    )


def client_for(settings) -> JiraClient:
    return JiraClient(settings.url, settings.token(), settings.timeout)


def fetch_tickets(settings, keys: Iterable[str], attachments_dir: str) -> list[dict[str, Any]]:
    """Fetch tickets as plain dicts, downloading every attachment. Raises JiraError."""
    keys = normalize_keys(keys)
    if not keys:
        raise JiraError("No ticket keys given.")
    client = client_for(settings)
    acceptance_field = settings.acceptance_field or None
    if not acceptance_field:
        try:
            acceptance_field = find_acceptance_field_id(client.get_fields())
        except JiraError as exc:
            print(f"Warning: acceptance field discovery skipped: {exc}", file=sys.stderr)
    return [asdict(extract_ticket(client, key, acceptance_field, attachments_dir)) for key in keys]


def list_fields(settings, needle: str) -> list[tuple[str, str]]:
    needle = needle.lower()
    return [
        (str(field.get("id", "")), str(field.get("name", "")))
        for field in client_for(settings).get_fields()
        if needle in str(field.get("name", "")).lower() or needle in str(field.get("id", "")).lower()
    ]
