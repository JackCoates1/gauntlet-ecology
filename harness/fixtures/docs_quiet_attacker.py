"""Hand-written quiet attacker: one direct read, no probe variety."""


def attack(request):
    request("user:mallory", "read", "doc:0:secret")
