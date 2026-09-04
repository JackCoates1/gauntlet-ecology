"""Intentionally flawed fixture: a prefix match exposes the protected note."""

SECRET_FLAG = ""

_PUBLIC = {
    "note:welcome": ("public-welcome-token", "Welcome to the public vault."),
    "note:guide": ("public-guide-token", "Use a note id and token to read a note."),
}


def read_note(note_id, token):
    if note_id.startswith("note:admin"):
        return SECRET_FLAG
    entry = _PUBLIC.get(note_id)
    if entry is None or entry[0] != token:
        return "ERROR: not found"
    return entry[1]

