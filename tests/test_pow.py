import hashlib

from dgsearch.spiders.daangn import solve_pow


def test_solve_pow():
    challenge = "test-challenge"
    nonce = solve_pow(challenge, 3)
    digest = hashlib.sha256(f"{challenge}:{nonce}".encode()).hexdigest()
    assert digest.startswith("000")

