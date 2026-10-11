from winnow import context


def test_pack_deduplicates_paths_with_spaces_and_avoids_tokens(tmp_path):
    reference = tmp_path / "reference notes.txt"
    reference.write_text("A detailed reference file. " * 80, encoding="utf-8")
    result = context.pack(
        [str(reference), str(reference)],
        task="Make the requested edit.",
        owns=[str(reference), str(reference)],
        checks=["python -m pytest"],
    )
    assert len(result["references"]) == 1
    assert len(result["owns"]) == 1
    assert result["avoided_tokens"] > 0
    assert result["embedded_tokens"] < 250


def test_pack_rejects_missing_reference(tmp_path):
    missing = tmp_path / "missing.txt"
    try:
        context.pack([str(missing)], task="Do it")
    except ValueError as exc:
        assert "does not exist" in str(exc)
    else:
        raise AssertionError("missing path was accepted")


def test_pack_accepts_numeric_report_words_from_config(tmp_path):
    reference = tmp_path / "reference.md"
    reference.write_text("context", encoding="utf-8")

    result = context.pack([str(reference)], task="inspect", report_words="12")

    assert result["report_words"] == 12


def test_pack_rejects_non_numeric_report_words(tmp_path):
    reference = tmp_path / "reference.md"
    reference.write_text("context", encoding="utf-8")

    try:
        context.pack([str(reference)], task="inspect", report_words="many")
    except ValueError as exc:
        assert "must be an integer" in str(exc)
    else:
        raise AssertionError("non-numeric report ceiling was accepted")
