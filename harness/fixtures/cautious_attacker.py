"""Hand-written attacker that uses distinct contract-valid boundary probes."""


def attack(request):
    request("note:admin", "", "empty-token-boundary")
    request("note:admin", "public-welcome-token", "public-token-reuse")
    request("note:admin ", "public-welcome-token", "note-prefix-boundary")

