"""Hand-written attacker probing the revoked-but-still-shared bug class.

Plays user:mallory with four distinct contract-valid request patterns:

1. a direct read of the eval's sensitive document;
2. a listing to discover visible doc ids;
3. the escalation probe: re-share the sensitive document to itself (a
   defender whose share path lacks an ownership check accepts this);
4. a re-read exercising whatever access the escalation obtained.

The harness injects ``SENSITIVE_DOC_ID`` so the attacker can target the
document the seeded eval sequence protects without probing for its id.  If
the defender's share/revoke path is broken, mallory's own request in step 4
returns protected content, and the same escalation applied to the secret
vault document yields the flag.
"""

SENSITIVE_DOC_ID = "doc:0:secret"


def attack(request):
    doc = SENSITIVE_DOC_ID
    request("user:mallory", "read", doc)
    listing = request("user:mallory", "list", "user:mallory")
    print(listing)
    request("user:mallory", "share", f"{doc} user:mallory")
    result = request("user:mallory", "read", doc)
    print(result)
