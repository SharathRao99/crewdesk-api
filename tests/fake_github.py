"""A tiny pretend GitHub for testing the sourcing pipeline without the network."""

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

from sourcer.github import Interrupted


class Crash(BaseException):
    """Simulates the worker process being killed: nothing in the pipeline may catch it."""


def _ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")


def person(login: str, location: str, repos: List[Dict[str, Any]], **profile: Any) -> Dict[str, Any]:
    return {"login": login, "location": location, "repos": repos, "profile": profile}


def php_repos(login: str, n: int = 4) -> List[Dict[str, Any]]:
    return [{"name": f"{login}-laravel-{i}", "description": "Laravel app", "topics": ["laravel"], "language": "PHP", "fork": False,
             "created_at": _ago(1300 - i * 50), "pushed_at": _ago(30 + i)} for i in range(n)]


def js_repos(login: str, n: int = 9) -> List[Dict[str, Any]]:
    return [{"name": f"{login}-web-{i}", "description": "site", "topics": [], "language": "JavaScript", "fork": False,
             "created_at": _ago(900 - i * 30), "pushed_at": _ago(20)} for i in range(n)]


def universe() -> Dict[str, Dict[str, Any]]:
    people = [
        person("amy", "Bengaluru, India", php_repos("amy"), email="amy@x.com", blog="amy.dev"),
        person("ben", "Pune, India", php_repos("ben", 5), email="ben@x.com"),
        person("chitra", "Bengaluru", php_repos("chitra", 3)),
        person("dev", "Chennai, India", php_repos("dev", 6), blog="dev.example.org"),
        person("weak", "Bengaluru, India", js_repos("weak") + php_repos("weak", 1)),  # PHP is 10% of their repos: not a main skill
        person("tex", "Texas, USA", php_repos("tex")),  # right skill, wrong place
        person("karachi", "Karachi, Pakistan", php_repos("karachi")),  # excluded place
        person("flaky", "Bengaluru, India", php_repos("flaky")),  # fetching this profile fails
    ]
    return {p["login"]: p for p in people}


class FakeGitHub:
    """Stands in for GitHubClient. `calls` records every request so tests can check what was (not) repeated."""

    def __init__(self, people: Dict[str, Dict[str, Any]], *, fail: Optional[Set[str]] = None, crash_after: Optional[int] = None,
                 on_wait=None, interrupt=None, wait_at: Optional[int] = None, pause_after: Optional[int] = None, on_pause=None, **_: Any):
        self.people, self.fail, self.crash_after = people, fail or set(), crash_after
        self._on_wait, self._interrupt, self.wait_at = on_wait, interrupt, wait_at
        self.pause_after, self.on_pause = pause_after, on_pause
        self.calls: List[str] = []
        self.profiles = 0

    def close(self) -> None:
        pass

    def get(self, path: str, params: Optional[Dict[str, Any]] = None, search: bool = False) -> Any:
        if self._interrupt and self._interrupt():
            raise Interrupted()
        self.calls.append(path)
        params = params or {}

        if path == "/search/users":
            lang = re.search(r"language:(\S+)", params["q"]).group(1)
            place = re.search(r'location:"([^"]+)"', params["q"]).group(1).lower()
            hits = [p for p in self.people.values() if place in p["location"].lower() and any(r["language"] == lang for r in p["repos"])]
            items = [{"login": p["login"], "type": "User"} for p in hits] if params.get("page", 1) == 1 else []
            return {"items": items}
        if path == "/search/repositories":
            keyword = params["q"].split()[0]
            items = [{"owner": {"login": p["login"], "type": "User"}} for p in self.people.values() if any(keyword in r["name"] for r in p["repos"])]
            return {"items": items} if params.get("page", 1) == 1 else {"items": []}

        match = re.fullmatch(r"/users/([^/]+)", path)
        if match:
            login = match.group(1)
            self.profiles += 1
            if self.wait_at is not None and self.profiles == self.wait_at and self._on_wait:
                self._on_wait("rate limit", 120)
            if self.pause_after is not None and self.profiles == self.pause_after + 1 and self.on_pause:
                self.on_pause()  # someone clicks Pause while this person is being checked
            if self.crash_after is not None and self.profiles > self.crash_after:
                raise Crash()
            if login in self.fail:
                raise RuntimeError(f"boom for {login}")
            p = self.people.get(login)
            if not p:
                return None
            return {"login": login, "type": "User", "name": login.title(), "location": p["location"], "bio": p["profile"].get("bio"),
                    "email": p["profile"].get("email"), "blog": p["profile"].get("blog"), "twitter_username": None, "hireable": None,
                    "followers": 3, "public_repos": len(p["repos"]), "created_at": _ago(2000), "html_url": f"https://github.com/{login}",
                    "company": None}
        match = re.fullmatch(r"/users/([^/]+)/repos", path)
        if match:
            return self.people[match.group(1)]["repos"]
        if re.fullmatch(r"/users/([^/]+)/social_accounts", path):
            return []
        return None  # composer.json lookups and anything else
