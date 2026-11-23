"""Reserve tagged tasks from Logseq journals, start them, and write results back.

A task is a TODO/LATER block whose first line carries every required tag.
Blocks that already carry `agent-run::` are skipped, so a task is never picked
up twice.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
TASK_RE = re.compile(r"^(\s*)- (TODO|LATER)\s")
REPO_RE = re.compile(r"(?<![\w-])(reference-repos|repos|repo)::[ \t]*([^\n]*?)(?=[ \t]+(?:#[^\s]*|\[\[|[\w-]+::)|$)", re.M)
BASE_RE = re.compile(r"(?<![\w-])(base-branches|base)::[ \t]*([^\n]*?)(?=[ \t]+(?:#[^\s]*|\[\[|[\w-]+::)|$)", re.M)
FILE_LINK_RE = re.compile(r"\[[^\]\n]*\]\(\s*(?:<([^>\n]+)>|([^\s)]+))(?:\s+[\"'][^\n]*?[\"'])?\s*\)")
PAGE_LINK_RE = re.compile(r"(?<!#)\[\[([^\]\n]+)\]\]")
MARKER_RE = re.compile(r"^(TODO|LATER|NOW|DOING|DONE|WAITING|WAIT|CANCELED|CANCELLED|IN-PROGRESS)\b")
BULLET_RE = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+(.*)$")
HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.*)$")


class JournalError(Exception):
    pass


def tag_re(tag):
    return re.compile(rf"#{re.escape(tag)}(?![\w-])|#\[\[{re.escape(tag)}\]\]")


def read_lines(path):
    """Read a Logseq file as lines, remembering its line ending for write_lines."""
    with open(path, encoding="utf-8", newline="") as handle:
        text = handle.read()
    return text.replace("\r\n", "\n").split("\n"), "\r\n" if "\r\n" in text else "\n"


def write_lines(path, lines, eol):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(eol.join(lines))


def indent(line):
    return len(line) - len(line.lstrip())


def uniq(seq):
    return list(dict.fromkeys(seq))


def resolve_page(name, logseq):
    """Locate a linked Markdown page by filename or an explicit title property."""
    pages = Path(logseq) / "pages"
    target = name.strip().casefold()
    candidates = []
    for page in sorted(pages.rglob("*.md")):
        relative = page.relative_to(pages).with_suffix("").as_posix()
        if target in (relative.casefold(), relative.replace("___", "/").casefold()):
            candidates.append(page)
            continue
        try:
            title = re.search(r"^title::\s*(.*?)\s*$", page.read_text(encoding="utf-8"), re.M)
        except (OSError, UnicodeError):
            continue
        if title and title.group(1).casefold() == target:
            candidates.append(page)
    if len(candidates) != 1:
        raise JournalError(f"Context page [[{name}]] must resolve to exactly one Markdown file in {pages}; "
                           f"found {len(candidates)}")
    return str(candidates[0].resolve())


def link_path(target):
    """Local path for a Markdown link target, or None for remote links."""
    url = urlsplit(target)
    if len(url.scheme) == 1:  # Windows drive letter, such as C:\docs\spec.md
        return Path(unquote(target.split("#", 1)[0]))
    if url.scheme == "file":
        if url.netloc not in ("", "localhost"):
            return None
        return Path(url2pathname(url.path))
    if url.scheme:
        return None
    return Path(unquote(url.path))


def context_files(block, journal):
    """Resolve local Markdown file links anywhere in the task block."""
    files = []
    for match in FILE_LINK_RE.finditer(block):
        path = link_path(match.group(1) or match.group(2))
        if path is None or path.suffix.lower() not in (".md", ".markdown"):
            continue
        path = path.expanduser()
        if not path.is_absolute():
            path = Path(journal).parent / path
        files.append(str(path.resolve()))
    return uniq(files)


def build_context(task, logseq):
    """Snapshot linked documents once so the agent does not need access to the graph."""
    files = list(task.get("context_files", []))
    files.extend(resolve_page(name, logseq) for name in task.get("context_pages", []))
    sections = []
    for name in uniq(files):
        path = Path(name)
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise JournalError(f"Cannot read Markdown context file {path}: {error}") from error
        sections.append(f"## Document: {path}\n\n{content}")
    return "\n\n".join(sections)


def attachment_index(tickets, directory):
    """Validate downloaded Jira files and build the offline attachment index."""
    root = Path(directory).resolve()
    lines = []
    for ticket in tickets:
        attachments = ticket.get("attachments", [])
        if not isinstance(attachments, list):
            raise JournalError(f"Invalid attachment metadata for {ticket['key']}")
        for attachment in attachments:
            if not isinstance(attachment, dict):
                raise JournalError(f"Invalid attachment entry for {ticket['key']}")
            name = attachment.get("filename", "unnamed attachment")
            local = attachment.get("local_path")
            if not isinstance(local, str) or not local:
                raise JournalError(f"Attachment not downloaded for {ticket['key']}: {name}")
            path = Path(local).resolve()
            if root not in path.parents or not path.is_file():
                raise JournalError(f"Attachment missing or outside its task directory: {name}")
            size = attachment.get("size")
            if isinstance(size, int) and size > 0 and path.stat().st_size != size:
                raise JournalError(f"Attachment size mismatch for {ticket['key']}: {name}")
            lines.append(f"- {ticket['key']}: {name}; type: {attachment.get('mime_type', 'unknown')}; local file: {path}")
    return "\n".join(lines)


def journal_files(logseq, days):
    today = dt.date.today()
    for n in range(days):
        p = Path(logseq) / "journals" / f"{today - dt.timedelta(days=n):%Y_%m_%d}.md"
        if p.exists():
            yield p


def repo_properties(block):
    implementations, references = [], []
    for match in REPO_RE.finditer(block):
        names = [name.strip() for name in match.group(2).split(",") if name.strip()]
        (references if match.group(1) == "reference-repos" else implementations).extend(names)
    return uniq(implementations), uniq(references)


def base_properties(block):
    base, overrides, error = "", {}, ""
    for match in BASE_RE.finditer(block):
        value = match.group(2).strip()
        if not value:
            error = f"{match.group(1)}:: must not be empty"
        elif match.group(1) == "base":
            if base and base != value:
                error = "Conflicting base:: properties"
            base = value
        else:
            for entry in value.split(","):
                name, separator, branch = entry.strip().partition("=")
                name, branch = name.strip(), branch.strip()
                if not separator or not name or not branch:
                    error = "base-branches:: requires comma-separated repo=branch entries"
                elif name in overrides and overrides[name] != branch:
                    error = f"Conflicting base branches for {name}"
                else:
                    overrides[name] = branch
    return base, overrides, error


def scan(path, tags):
    lines, _ = read_lines(path)
    patterns = [tag_re(tag) for tag in tags]
    tasks = []
    for i, line in enumerate(lines):
        m = TASK_RE.match(line)
        if not m:
            continue
        ind = len(m.group(1))
        end = i + 1
        while end < len(lines) and (not lines[end].strip() or indent(lines[end]) > ind):
            end += 1
        block = "\n".join(lines[i:end]).rstrip()
        if "agent-run::" in block:
            continue
        if not all(pattern.search(line) for pattern in patterns):
            continue

        # Branch names may contain an epic ticket key; these are not task tickets.
        keys = uniq(KEY_RE.findall(BASE_RE.sub("", block)))
        repos, reference_repos = repo_properties(block)
        base_branch, base_branches, base_error = base_properties(block)
        title = line.strip()[2:]
        for pattern in patterns:
            title = pattern.sub("", title)
        tasks.append({
            "file": str(path),
            "line": i,
            "id": keys[0] if keys else "task-" + hashlib.sha1(block.encode()).hexdigest()[:8],
            "title": title.strip(),
            "keys": keys,
            "repo": repos[0] if repos else "",
            "repos": repos,
            "reference_repos": reference_repos,
            "base_branch": base_branch,
            "base_branches": base_branches,
            "base_error": base_error,
            "block": block,
            "context_files": context_files(block, path),
            # First-line page references organize tasks; child references supply context.
            "context_pages": uniq(PAGE_LINK_RE.findall("\n".join(block.split("\n")[1:]))),
        })
    return tasks


def claim(tasks):
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    by_file = {}
    for t in tasks:
        by_file.setdefault(t["file"], []).append(t)
    for f, ts in by_file.items():
        lines, eol = read_lines(f)
        for t in sorted(ts, key=lambda t: -t["line"]):  # bottom-up keeps indexes valid
            i = t["line"]
            if lines[i] != t["block"].split("\n", 1)[0]:
                raise JournalError(f"Task changed while selecting it in {f}; refusing to claim it")
            pad = lines[i][: indent(lines[i])]
            # Reserve the batch, but mark DOING only when execution starts.
            lines.insert(i + 1, f"{pad}  agent-run:: {t['id']} @ {stamp}")
        write_lines(f, lines, eol)


def pick(logseq, days, tags, dry_run=False):
    tasks, seen = [], {}
    for p in journal_files(logseq, days):
        for t in scan(p, tags):
            n = seen.get(t["id"], 0)
            seen[t["id"]] = n + 1
            if n:
                t["id"] = f"{t['id']}-{n + 1}"
            tasks.append(t)
    if not dry_run:
        claim(tasks)
    return tasks


def split_summary(text):
    """First non-blank line is the headline; the rest is the Markdown body."""
    rows = text.strip().split("\n")
    headline = HEADING_RE.sub(r"\1", BULLET_RE.sub(r"\2", rows[0])).strip()
    return headline, "\n".join(rows[1:])


def logseq_safe(text):
    # Summary lines must not become tasks or properties when read back as blocks.
    text = MARKER_RE.sub(lambda m: m.group(1).capitalize(), text.strip())
    return re.sub(r"^([^\s:]+)::", r"\1:", text)


def summary_blocks(text):
    """Turn Markdown headings, paragraphs and nested bullets into (depth, text) blocks."""
    blocks, base = [], 0
    for line in text.split("\n"):
        if not line.strip():
            continue
        heading, bullet = HEADING_RE.match(line), BULLET_RE.match(line)
        if heading:
            blocks.append((0, f"**{logseq_safe(heading.group(1))}**"))
            base = 1
        elif bullet:
            depth = base + len(bullet.group(1).expandtabs(4)) // 2
            limit = blocks[-1][0] + 1 if blocks else 0
            blocks.append((min(depth, limit), logseq_safe(bullet.group(2))))
        elif blocks and indent(line) and not blocks[-1][1].startswith("**"):
            blocks[-1] = (blocks[-1][0], f"{blocks[-1][1]} {logseq_safe(line)}")
        else:
            text = logseq_safe(line)
            blocks.append((min(base, blocks[-1][0] + 1) if blocks else 0, text))
            if text.endswith(":"):
                base = blocks[-1][0] + 1
    return blocks


def insert_summary(lines, task, blocks):
    """Add the summary as a child block tree after the task's properties."""
    pad = lines[task][: indent(lines[task])]
    at = task + 1
    while at < len(lines) and lines[at].strip() and indent(lines[at]) > len(pad) \
            and not lines[at].lstrip().startswith("- "):
        at += 1
    child = next((l for l in lines[at:at + 1] if indent(l) > len(pad) and l.lstrip().startswith("- ")), None)
    if child is not None:
        unit = child[len(pad): indent(child)]
    else:
        unit = "\t" if "\t" in pad or any(re.match(r"^\t+- ", l) for l in lines) else "  "
    rows = [f"{pad}{unit}- **Agent summary**"]
    rows += [f"{pad}{unit * (depth + 2)}- {text}" for depth, text in blocks]
    lines[at:at] = rows


def _find_run(lines, task_id):
    pat = re.compile(rf"^\s*agent-run::\s*{re.escape(task_id)}(?=\s|$)")
    for i, line in enumerate(lines):
        if pat.search(line) and i > 0:
            if not re.search(r"- (TODO|LATER|DOING)\s", lines[i - 1]):
                raise JournalError("Task state changed; refusing to overwrite it")
            return i
    raise JournalError(f"agent-run marker for {task_id} not found")


def _set_marker(lines, i, marker):
    lines[i - 1] = re.sub(r"- (TODO|LATER|DOING)\s", f"- {marker} ", lines[i - 1], count=1)


def start(file, task_id):
    """Mark one reserved task DOING."""
    lines, eol = read_lines(file)
    i = _find_run(lines, task_id)
    _set_marker(lines, i, "DOING")
    write_lines(file, lines, eol)


def report(file, task_id, status, text, branch="", worktree="", validation="", repositories="",
           blocked="", details=""):
    """Record a handoff: WAITING when ok or no-change, otherwise back to TODO."""
    lines, eol = read_lines(file)
    i = _find_run(lines, task_id)
    _set_marker(lines, i, "WAITING" if status in ("ok", "no-change") else "TODO")
    headline, body = split_summary(text)
    props = {"agent-result": headline, "agent-branch": branch,
             "agent-worktree": worktree, "agent-validation": validation,
             "agent-repositories": repositories, "agent-status": status,
             "agent-blocked": blocked}
    lines[i + 1:i + 1] = [f"{lines[i][: indent(lines[i])]}{key}:: {' '.join(value.split())}"
                          for key, value in props.items() if value]
    blocks = summary_blocks(body)
    if details:
        blocks.append((0, f"Details: {details}"))
    if blocks:
        insert_summary(lines, i - 1, blocks)
    write_lines(file, lines, eol)
