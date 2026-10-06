"""Fill the role's email template for a candidate."""

from urllib.parse import quote
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_OUTREACH = {
    "role_title": "developer",
    "subject": "Opportunity",
    "body": "Hi {first_name},\n\nI came across your work on {repo} and wanted to reach out about a role.\n\nBest regards,\n{sender}",
}


def best_email(row: Dict[str, Any]) -> Optional[str]:
    emails: List[str] = [e for e in [row.get("email"), *(row.get("portfolio_emails") or [])] if e]
    return emails[0] if emails else None


def render_draft(row: Dict[str, Any], outreach: Dict[str, str], sender: str = "") -> Tuple[str, str]:
    """Return (subject, body) with placeholders filled in."""
    values = {
        "first_name": (row.get("name") or row["login"]).split()[0],
        "repo": (row.get("matching_repos") or ["your projects"])[0],
        "sender": sender,
        "role_title": outreach.get("role_title", ""),
    }

    def fill(text: str) -> str:
        for key, value in values.items():
            text = text.replace("{" + key + "}", value)
        return text

    return fill(outreach["subject"]), fill(outreach["body"]).strip()


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
