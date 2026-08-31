"""Direct unit tests for class_names() — pure function, no HA needed.

class_names() unions two sources on purpose: the /api/classes roster
(which only includes an empty-count class when ClassDash's own
showEmptyClasses setting is on, and never includes an announcement-only
class at all) and a scan of due-soon/ahead/overdue/announcements (which
misses empty classes entirely). Neither alone is the complete picture.
"""

from __future__ import annotations

from custom_components.classdash.coordinator import ClassDashData, class_names


def _data(due_soon=(), ahead=(), overdue=(), announcements=(), classes=()) -> ClassDashData:
    return ClassDashData(
        status={},
        due_soon=list(due_soon),
        ahead=list(ahead),
        overdue=list(overdue),
        announcements=list(announcements),
        classes=list(classes),
    )


def test_includes_classes_from_the_roster_alone() -> None:
    """A class with nothing due (showEmptyClasses on) has no presence in
    any of the item lists — only the roster knows about it."""
    data = _data(classes=[{"name": "Art History", "dueSoon": 0, "ahead": 0, "overdue": 0}])
    assert class_names(data) == {"Art History"}


def test_includes_classes_from_item_lists_alone() -> None:
    """The reverse: an announcement-only class (or, with showEmptyClasses
    off, any class with only a due-soon item) never appears in the
    roster at all."""
    data = _data(announcements=[{"class": "Chemistry"}])
    assert class_names(data) == {"Chemistry"}


def test_unions_both_sources_without_duplicates() -> None:
    data = _data(
        due_soon=[{"class": "Physics"}],
        classes=[
            {"name": "Physics", "dueSoon": 1, "ahead": 0, "overdue": 0},
            {"name": "Art History", "dueSoon": 0, "ahead": 0, "overdue": 0},
        ],
    )
    assert class_names(data) == {"Physics", "Art History"}


def test_empty_everything_is_empty() -> None:
    assert class_names(_data()) == set()
