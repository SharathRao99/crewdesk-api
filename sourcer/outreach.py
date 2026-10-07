"""Email templates: placeholders, rendering, and mail links."""

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

DEFAULT_OUTREACH = {
    "role_title": "developer",
    "subject": "Opportunity",
    "body": "Hi {first_name},\n\nI came across your work on {repo} and wanted to reach out about a role.\n\nBest regards,\n{sender}",
}

# Every placeholder a template may use. Anything else is rejected when a template is saved.
PLACEHOLDERS: Dict[str, str] = {
    "first_name": "Candidate's first name",
    "full_name": "Candidate's full name",
    "repo": "One of their repos that matches the role",
    "location": "Their location",
    "company_name": "Your company name",
    "role_title": "Title of the role you are hiring for",
    "sender": "The name of the person sending the email",
    "form_link": "A private link where the candidate shares notice period and salary expectations",
}
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def unknown_placeholders(*texts: str) -> List[str]:
    """Placeholders used in the texts that are not supported, in order of first use."""
    found: List[str] = []
    for text in texts:
        for name in _PLACEHOLDER_RE.findall(text or ""):
            if name not in PLACEHOLDERS and name not in found:
                found.append(name)
    return found


def best_email(row: Dict[str, Any]) -> Optional[str]:
    emails: List[str] = [e for e in [row.get("email"), *(row.get("portfolio_emails") or [])] if e]
    return emails[0] if emails else None


def candidate_values(row: Dict[str, Any], company_name: str = "", role_title: str = "", sender: str = "", form_link: str = "") -> Dict[str, str]:
    name = (row.get("name") or row.get("login") or "").strip()
    return {
        "first_name": name.split()[0] if name else "",
        "full_name": name,
        "repo": (row.get("matching_repos") or ["your projects"])[0],
        "location": row.get("location") or "",
        "company_name": company_name,
        "role_title": role_title,
        "sender": sender,
        "form_link": form_link,
    }


def fill(text: str, values: Dict[str, str]) -> str:
    """Replace known placeholders in one pass, so a value containing braces is never re-expanded."""
    return _PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), m.group(0)), text)


def render(subject: str, body: str, values: Dict[str, str]) -> Tuple[str, str]:
    return fill(subject, values).strip(), fill(body, values).strip()


def uses_form_link(*texts: str) -> bool:
    return any("{form_link}" in (t or "") for t in texts)


def render_draft(row: Dict[str, Any], outreach: Dict[str, str], sender: str = "", company_name: str = "", form_link: str = "") -> Tuple[str, str]:
    """Return (subject, body) for one candidate from a {role_title, subject, body} dict."""
    values = candidate_values(row, company_name, outreach.get("role_title", ""), sender, form_link)
    return render(outreach["subject"], outreach["body"], values)


def mail_links(to: str, subject: str, body: str) -> Dict[str, str]:
    """Links that open a ready-to-send draft. Gmail/Outlook open in the browser and need
    no default mail app; mailto: uses whatever mail app the computer has set up."""
    q = lambda text: quote(text, safe="")
    # Mail apps only turn a web address into a link once something follows it, so a URL that ends
    # the message needs trailing line breaks.
    body = body.rstrip() + "\n\n"
    crlf = body.replace("\r\n", "\n").replace("\n", "\r\n")  # mailto bodies expect CRLF line breaks
    return {
        "mailto_link": f"mailto:{to}?subject={q(subject)}&body={q(crlf)}",
        "gmail_link": f"https://mail.google.com/mail/?view=cm&fs=1&to={q(to)}&su={q(subject)}&body={q(body)}",
        "outlook_link": f"https://outlook.office.com/mail/deeplink/compose?to={q(to)}&subject={q(subject)}&body={q(body)}",
    }
