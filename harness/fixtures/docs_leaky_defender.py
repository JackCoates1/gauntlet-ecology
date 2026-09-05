"""Intentionally flawed shared-doc defender: revocable access is circumventable.

Bug: `share` performs no ownership check, so any principal may grant any
existing document to anyone.  A revoked user (or any outsider like mallory)
can simply re-share the admin's vault document to itself and read the secret.
This is the revoked-but-still-shared bug class: revocation is honored at
revoke time but immediately circumvented through the broken share path.
"""

SECRET_FLAG = ""
SECRET_DOC_ID = "doc:admin-secret"

_docs = {}
_shares = {}


def doc_access(user, action, arg):
    if action == "create":
        content = SECRET_FLAG if arg == SECRET_DOC_ID else f"content of {arg}"
        _docs[arg] = (user, content)
        return "OK created " + arg
    if action == "read":
        entry = _docs.get(arg)
        if entry is None:
            return "ERROR: not found"
        owner, content = entry
        if user == owner or user in _shares.get(arg, set()):
            return content
        return "ERROR: denied"
    if action == "share":
        target_doc, _, grantee = arg.rpartition(" ")
        entry = _docs.get(target_doc)
        if entry is None:
            return "ERROR: not found"
        # BUG: no ownership check at all.  Anyone may share anything.
        _shares.setdefault(target_doc, set()).add(grantee)
        return "OK shared"
    if action == "revoke":
        target_doc, _, grantee = arg.rpartition(" ")
        entry = _docs.get(target_doc)
        if entry is None:
            return "ERROR: not found"
        if user != entry[0]:
            return "ERROR: denied"
        _shares.get(target_doc, set()).discard(grantee)
        return "OK revoked"
    if action == "list":
        visible = sorted(
            doc_id
            for doc_id, (owner, _) in _docs.items()
            if owner == user or user in _shares.get(doc_id, set())
        )
        return "\n".join(visible) if visible else "ERROR: no docs"
    return "ERROR: unknown action"
