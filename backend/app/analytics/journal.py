"""Trade journal: notes and tags on an order or a closed trade, stored in account.journal."""
from typing import Optional
from ..core.errors import NotFound

TARGETS = ("order", "trade")
MAX_NOTE, MAX_TAGS, MAX_TAG = 4000, 12, 32


def clean_tags(tags) -> list:
    if tags is None:
        return []
    if isinstance(tags, str):
        tags = tags.split(",")
    if not isinstance(tags, list):
        raise ValueError("tags must be a list of words or a comma-separated string")
    out, seen = [], set()
    for t in tags:
        t = " ".join(str(t).split())
        if not t:
            continue
        if len(t) > MAX_TAG:
            raise ValueError(f"a tag can be at most {MAX_TAG} characters")
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    if len(out) > MAX_TAGS:
        raise ValueError(f"an entry can have at most {MAX_TAGS} tags")
    return out


def clean_note(note) -> str:
    note = "" if note is None else str(note).strip()
    if len(note) > MAX_NOTE:
        raise ValueError(f"a note can be at most {MAX_NOTE} characters")
    return note


def _next_id(account) -> str:
    n = max((int(j["id"][1:]) for j in account.journal if j["id"][1:].isdigit()), default=0)
    return f"J{n + 1}"


def add(account, target_type: str, target_id: str, note, tags, now: float) -> dict:
    if target_type not in TARGETS:
        raise ValueError("an entry must be attached to an 'order' or a 'trade'")
    note, tags = clean_note(note), clean_tags(tags)
    if not note and not tags:
        raise ValueError("write a note or add at least one tag")
    entry = {"id": _next_id(account), "target_type": target_type, "target_id": str(target_id),
             "note": note, "tags": tags, "created_at": now, "updated_at": now}
    account.journal.append(entry)
    return entry


def get(account, entry_id: str) -> dict:
    for j in account.journal:
        if j["id"] == entry_id:
            return j
    raise NotFound(f"no journal entry {entry_id}")


def update(account, entry_id: str, now: float, note=None, tags=None) -> dict:
    j = get(account, entry_id)
    new_note = clean_note(note) if note is not None else j["note"]
    new_tags = clean_tags(tags) if tags is not None else j["tags"]
    if not new_note and not new_tags:
        raise ValueError("an entry needs a note or at least one tag; delete it instead")
    j.update(note=new_note, tags=new_tags, updated_at=now)
    return j


def delete(account, entry_id: str) -> None:
    j = get(account, entry_id)
    account.journal.remove(j)


def entries(account, target_type: Optional[str] = None, target_id: Optional[str] = None, tag: Optional[str] = None) -> list:
    out = account.journal
    if target_type:
        out = [j for j in out if j["target_type"] == target_type]
    if target_id:
        out = [j for j in out if j["target_id"] == target_id]
    if tag:
        out = [j for j in out if tag.lower() in (t.lower() for t in j["tags"])]
    return sorted(out, key=lambda j: (j["created_at"], j["id"]), reverse=True)
