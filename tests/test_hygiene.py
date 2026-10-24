from emailsupersearch.ingest.hygiene import new_text, strip_quoted, strip_signature


def test_strip_quoted_outlook_header_block():
    body = "Agreed, 4% is fine.\n\nFrom: Priya Nair\nSent: Monday\nTo: Daniel\nSubject: RE: Acme\n\nEarlier text"
    assert strip_quoted(body).strip() == "Agreed, 4% is fine."


def test_strip_quoted_on_wrote_and_gt_lines():
    assert strip_quoted("Yes.\n\nOn Tue, Mar 3 Priya wrote:\n> old\n> text").strip() == "Yes."


def test_strip_signature_regards_block():
    body = "Done.\n\nKind regards,\nMei Lin\nFinance | +44 20 7946 0000"
    assert strip_signature(body).strip() == "Done."


def test_new_text_keeps_quotes_without_parent():
    assert "old" in new_text("Yes.\n> old", has_parent=False)
    assert "old" not in new_text("Yes.\n> old", has_parent=True)


def test_new_text_never_empty():
    assert new_text("> only a quote", has_parent=True).strip()


def test_strip_quoted_original_message_marker():
    body = "Please proceed.\n\n-----Original Message-----\nFrom: A\nSent: B\n\nold"
    assert strip_quoted(body).strip() == "Please proceed."


def test_strip_quoted_outlook_underscore_separator():
    body = "Looks good.\n\n________________________________\nFrom: A\nold"
    assert strip_quoted(body).strip() == "Looks good."


def test_strip_quoted_cuts_at_earliest_marker():
    body = "Fine.\n> quoted early\nOn Tue, Mar 3 Priya wrote:\nold"
    assert strip_quoted(body).strip() == "Fine."


def test_strip_quoted_from_line_without_sent_or_to_is_kept():
    body = "Note below.\nFrom: the finance team we heard back\nNo header block here."
    assert strip_quoted(body) == body


def test_strip_quoted_on_wrote_is_case_insensitive():
    assert strip_quoted("Yes.\non Tue Priya WROTE:\nold").strip() == "Yes."


def test_strip_quoted_leaves_unquoted_text_alone():
    body = "A > B is the claim.\nNothing quoted here."
    assert strip_quoted(body) == body


def test_strip_signature_dash_dash_delimiter():
    assert strip_signature("Done.\n-- \nMei Lin\nFinance").strip() == "Done."


def test_strip_signature_thanks_and_best_and_warm():
    assert strip_signature("Done.\nThanks,\nMei").strip() == "Done."
    assert strip_signature("Done.\nThanks\nMei").strip() == "Done."
    assert strip_signature("Done.\nBest regards,\nMei").strip() == "Done."
    assert strip_signature("Done.\nWarm regards\nMei").strip() == "Done."


def test_strip_signature_sent_from_my_device():
    assert strip_signature("On it.\nSent from my iPhone").strip() == "On it."


def test_strip_signature_trailing_phone_block_without_marker():
    body = "Done.\n\nMei Lin\nSenior Analyst\nAcme Ltd\nT: +44 20 7946 0000"
    assert strip_signature(body).strip() == "Done."


def test_strip_signature_keeps_short_lines_without_phone_number():
    body = "Done.\n\nMei Lin\nSenior Analyst\nAcme Ltd"
    assert strip_signature(body) == body


def test_strip_signature_does_not_eat_a_long_trailing_paragraph():
    body = "Call me on +44 20 7946 0000 about the 4% uplift we discussed with the whole team."
    assert strip_signature(body) == body


def test_strip_signature_trailing_block_stops_at_a_blank_line():
    body = "Call +44 20 7946 0000 today.\n\nThanks for the update on the contract terms."
    assert strip_signature(body) == body


def test_new_text_collapses_whitespace():
    assert new_text("A  \t b\n\n\n\n\nc  ", has_parent=False) == "A b\n\nc"


def test_new_text_falls_back_to_original_when_everything_stripped():
    assert new_text("Kind regards,\nMei", has_parent=False) == "Kind regards,\nMei"


def test_new_text_of_blank_body_is_empty():
    assert new_text("  \n\n ", has_parent=True) == ""


def test_strip_signature_trailing_run_longer_than_six_lines_is_kept():
    body = "Hi.\n\na\nb\nc\nd\ne\nf\nT: +44 20 7946 0000"
    assert strip_signature(body) == body


def test_strip_signature_date_is_not_a_phone_number():
    body = "Deadline: 2026-10-24\nPlease confirm."
    assert strip_signature(body) == body
    body = "Done.\n\nDeadline: 2026-10-24\nPlease confirm."
    assert strip_signature(body) == body


def test_strip_signature_uk_phone_formats_are_phone_numbers():
    assert strip_signature("Done.\n\nMei Lin\n020 7946 0000").strip() == "Done."
    assert strip_signature("Done.\n\nMei Lin\n+44 20 7946 0000").strip() == "Done."


def test_strip_signature_trailing_block_is_anchored_at_a_blank_line():
    no_blank = "Please confirm.\nMei Lin\n+44 20 7946 0000"
    assert strip_signature(no_blank) == no_blank
    with_blank = "Please confirm.\n\nMei Lin\n+44 20 7946 0000"
    assert strip_signature(with_blank).strip() == "Please confirm."


def test_strip_signature_trailing_block_with_a_long_line_is_kept():
    body = "Done.\n\nCall me on +44 20 7946 0000 about the 4% uplift we discussed with the team.\nMei"
    assert strip_signature(body) == body


def test_strip_signature_trailing_block_ignores_trailing_blank_lines():
    assert strip_signature("Done.\n\nMei Lin\n+44 20 7946 0000\n\n  \n").strip() == "Done."
