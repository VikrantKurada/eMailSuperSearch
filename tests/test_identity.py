from emailsupersearch.ingest.identity import (
    message_key,
    normalize_addr,
    normalize_subject,
)


def test_normalize_addr():
    assert normalize_addr(" <Jane.Doe@Example.COM> ") == "jane.doe@example.com"


def test_normalize_subject_strips_reply_prefixes():
    assert normalize_subject("RE: Re[2]: FW: Acme  renewal ") == "acme renewal"


def test_message_key_uses_message_id_when_present(parsed_factory):
    assert message_key(parsed_factory(message_id="<abc@x>")) == "abc@x"


def test_message_key_metadata_hash_is_stable_and_body_sensitive(parsed_factory):
    a = parsed_factory(message_id=None, body_text="hello")
    b = parsed_factory(message_id=None, body_text="hello")
    c = parsed_factory(message_id=None, body_text="other")
    assert message_key(a) == message_key(b) != message_key(c) and len(message_key(a)) == 64


def test_normalize_addr_keeps_x500_whole():
    assert normalize_addr(" /O=ACME/OU=EXCH/CN=Recipients/CN=JDoe ") == "/o=acme/ou=exch/cn=recipients/cn=jdoe"


def test_normalize_subject_strips_every_prefix_family():
    assert normalize_subject("  aw: WG: Fwd: fw[3]:  Q1   plan") == "q1 plan"


def test_message_key_hash_matches_documented_formula(parsed_factory):
    import hashlib

    m = parsed_factory(message_id=None, sender_addr="<Jane@Example.com>", subject="RE: Hello", sent_ts=None, body_text="x" * 3000)
    expected = hashlib.sha256(f"jane@example.com|None|hello|{'x' * 2048}".encode()).hexdigest()
    assert message_key(m) == expected


def test_message_key_blank_message_id_falls_back_to_hash(parsed_factory):
    assert len(message_key(parsed_factory(message_id="<>"))) == 64
