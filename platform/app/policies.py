"""Policy versioning and deterministic operation guards."""

from __future__ import annotations

from copy import deepcopy
from posixpath import normpath
from typing import Any
from urllib.parse import unquote, urlsplit

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .models import Page, Policy, Site

ALLOWED_ACTIONS = frozenset(
    {"metadata", "publish", "refresh", "store_editorial", "links", "alt_text"}
)

DEFAULT_POLICY: dict[str, Any] = {
    "enabled": False,
    "allowed_actions": ["metadata"],
    "protected_paths": [
        "/",
        "/contact*",
        "/privacy*",
        "/terms*",
        "/checkout*",
        "/cart*",
        "/my-account*",
    ],
    "posts_per_week": 2,
    "refreshes_per_week": 1,
    "monthly_budget_cents": 30000,
    "tracked_keywords": [],
    "competitors": [],
    "tracked_questions": [],
    "publish_days": [1, 4],
    "author_id": None,
}

_MAX_LIST_ITEMS = {
    "tracked_keywords": 25,
    "competitors": 3,
    "tracked_questions": 20,
}

_POLICY_KEYS = frozenset(DEFAULT_POLICY)
_MAX_MONTHLY_BUDGET_CENTS = 30_000
_MAX_POSTS_PER_WEEK = 2
_MAX_REFRESHES_PER_WEEK = 1


def _copy_settings(settings: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(settings)


def _string_list(settings: dict[str, Any], key: str, maximum: int) -> list[str]:
    value = settings.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    if len(value) > maximum:
        raise ValueError(f"{key} may contain at most {maximum} items")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{key} must contain non-empty strings")
        result.append(item.strip())
    return result


def _validated_settings(settings_dict: dict[str, Any] | None) -> dict[str, Any]:
    if settings_dict is None:
        settings_dict = {}
    if not isinstance(settings_dict, dict):
        raise ValueError("policy settings must be an object")

    unknown = sorted(set(settings_dict).difference(_POLICY_KEYS))
    if unknown:
        raise ValueError(f"unsupported policy settings: {', '.join(unknown)}")

    settings = _copy_settings(DEFAULT_POLICY)
    settings.update(_copy_settings(settings_dict))

    if not isinstance(settings["enabled"], bool):
        raise ValueError("enabled must be a boolean")

    allowed_actions = settings.get("allowed_actions")
    if not isinstance(allowed_actions, list):
        raise ValueError("allowed_actions must be a list")
    normalized_actions: list[str] = []
    for action in allowed_actions:
        if not isinstance(action, str) or action not in ALLOWED_ACTIONS:
            raise ValueError(f"unsupported policy action: {action!r}")
        if action not in normalized_actions:
            normalized_actions.append(action)
    settings["allowed_actions"] = normalized_actions

    protected_paths = settings.get("protected_paths")
    if not isinstance(protected_paths, list):
        raise ValueError("protected_paths must be a list")
    normalized_paths: list[str] = []
    for path in protected_paths:
        if not isinstance(path, str) or not path.strip().startswith("/"):
            raise ValueError("protected_paths must contain URL paths")
        normalized_paths.append(path.strip())
    settings["protected_paths"] = normalized_paths

    for key, maximum in _MAX_LIST_ITEMS.items():
        settings[key] = _string_list(settings, key, maximum)

    for key in ("posts_per_week", "refreshes_per_week", "monthly_budget_cents"):
        value = settings.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{key} must be a non-negative integer")
    if settings["posts_per_week"] > _MAX_POSTS_PER_WEEK:
        raise ValueError(f"posts_per_week may not exceed {_MAX_POSTS_PER_WEEK}")
    if settings["refreshes_per_week"] > _MAX_REFRESHES_PER_WEEK:
        raise ValueError(f"refreshes_per_week may not exceed {_MAX_REFRESHES_PER_WEEK}")
    if settings["monthly_budget_cents"] > _MAX_MONTHLY_BUDGET_CENTS:
        raise ValueError("monthly_budget_cents may not exceed 30000")

    publish_days = settings.get("publish_days")
    if (
        not isinstance(publish_days, list)
        or any(isinstance(day, bool) or not isinstance(day, int) or day < 0 or day > 6 for day in publish_days)
    ):
        raise ValueError("publish_days must contain weekday integers from 0 through 6")
    settings["publish_days"] = list(dict.fromkeys(publish_days))

    author_id = settings.get("author_id")
    if author_id is not None and (not isinstance(author_id, str) or not author_id.strip()):
        raise ValueError("author_id must be a non-empty string or null")
    if isinstance(author_id, str):
        settings["author_id"] = author_id.strip()

    return settings


def create_policy(
    db: Session,
    site: Site,
    user_id: str | None,
    settings_dict: dict[str, Any] | None,
) -> Policy:
    """Append a new validated policy version for a site."""

    settings = _validated_settings(settings_dict)
    if not isinstance(site, Site) or not site.id:
        raise ValueError("a persisted site is required")

    # Version allocation is serialized by the owning site row. PostgreSQL
    # honors FOR UPDATE directly; SQLite has no row locks, so a fresh
    # transaction takes a database write lock before the same read/insert
    # sequence. The SELECT ... FOR UPDATE remains intentional for production.
    if db.get_bind().dialect.name == "sqlite":
        if not db.in_transaction():
            db.execute(text("BEGIN IMMEDIATE"))
        else:
            # API requests commonly read the site before creating a policy,
            # which opens a deferred SQLite transaction. A no-op write claims
            # the database writer lock before version allocation so two
            # concurrent saves cannot both observe the same latest version.
            db.execute(text("UPDATE sites SET id = id WHERE id = :site_id"), {"site_id": site.id})
    locked_site = db.scalar(
        select(Site)
        .where(Site.id == site.id)
        .with_for_update()
    )
    if locked_site is None:
        raise ValueError("site not found")
    latest = db.scalar(
        select(Policy)
        .where(Policy.site_id == locked_site.id)
        .order_by(Policy.version.desc())
        .limit(1)
    )
    version = (latest.version if latest is not None else 0) + 1
    policy = Policy(
        site_id=locked_site.id,
        version=version,
        settings=settings,
        created_by=user_id,
    )
    db.add(policy)
    db.flush()
    return policy


def current_policy(db: Session, site_id: str) -> Policy | None:
    """Return the newest policy version without mutating older versions."""

    return db.scalar(
        select(Policy)
        .where(Policy.site_id == site_id)
        .order_by(Policy.version.desc())
        .limit(1)
    )


def _page_value(page: Page | dict[str, Any] | None, key: str, default: Any = None) -> Any:
    if page is None:
        return default
    if isinstance(page, dict):
        return page.get(key, default)
    return getattr(page, key, default)


def _protected_path(path: str, pattern: str) -> bool:
    if pattern == "/":
        return path == "/"
    if pattern.endswith("*"):
        return path.startswith(pattern[:-1])
    return path == pattern


def _page_path(page: Page | dict[str, Any]) -> str:
    raw_url = str(_page_value(page, "url", "") or "")
    parsed = urlsplit(raw_url)
    path = unquote(parsed.path or raw_url or "/")
    # Apply the same effective-path normalization that a URL consumer uses
    # before matching protected resources. Without this, a URL such as
    # ``/marketing/../contact`` (or its encoded equivalent) could bypass a
    # ``/contact*`` protection rule while still resolving to /contact.
    path = path.replace("\\", "/")
    while "//" in path:
        path = path.replace("//", "/")
    if not path.startswith("/"):
        path = "/" + path
    normalized = normpath(path)
    if normalized == ".":
        return "/"
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return normalized


def _has_business_facts(site: Site) -> bool:
    facts = site.facts if isinstance(site.facts, dict) else {}
    if not str(facts.get("business_name", "")).strip():
        return False
    substantive_keys = ("audience", "locations", "services", "products")
    return any(facts.get(key) for key in substantive_keys)


def evaluate_policy(
    site: Site,
    policy: Policy | None,
    action: str,
    page: Page | dict[str, Any] | None = None,
    global_pause: bool = False,
) -> list[str]:
    """Return all deterministic reasons an operation must be blocked.

    The function is pure with respect to the database. Callers can show the
    returned reasons to an operator and must refuse the operation when any are
    present.
    """

    blockers: list[str] = []
    if global_pause:
        blockers.append("global_pause")
    if site.paused:
        blockers.append("site_paused")
    if policy is None:
        blockers.append("policy_missing")
        return blockers

    if policy.site_id != site.id:
        blockers.append("policy_site_mismatch")
        return blockers
    try:
        settings = _validated_settings(policy.settings)
    except ValueError:
        blockers.append("policy_invalid")
        return blockers
    if not isinstance(policy.version, int) or policy.version < 1:
        blockers.append("policy_invalid")
        return blockers
    if not settings.get("enabled", False):
        blockers.append("policy_disabled")

    if action not in settings.get("allowed_actions", []):
        blockers.append("action_not_allowed")

    if page is not None:
        path = _page_path(page)
        if any(_protected_path(path, pattern) for pattern in settings.get("protected_paths", [])):
            blockers.append("protected_path")
        if action in {"publish", "refresh", "store_editorial", "links", "alt_text"} and not _page_value(
            page, "enrolled", False
        ):
            blockers.append("page_not_enrolled")

    if action == "publish":
        if not _has_business_facts(site):
            blockers.append("business_facts_required")
        author_id = settings.get("author_id")
        if not author_id:
            facts = site.facts if isinstance(site.facts, dict) else {}
            authors = facts.get("authors")
            if not (isinstance(authors, list) and authors):
                blockers.append("author_required")

    return list(dict.fromkeys(blockers))


__all__ = [
    "ALLOWED_ACTIONS",
    "DEFAULT_POLICY",
    "create_policy",
    "current_policy",
    "evaluate_policy",
]
