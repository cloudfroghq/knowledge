"""Regression tests for ``confluence_id`` validation in ``confluence_sync``.

A ``confluence_id`` is a Confluence page id: a plain ASCII digit string.
Anything else has to be rejected locally, before any request leaves the
process, so a malformed value can never be mistaken for a transport failure.

Every case here builds its own vault under ``tmp_path`` and points
``VAULT_PATH`` at it. None of them reads vault content shipped with the
repository, and none of them reaches the network: ``urllib.request.urlopen``
is patched to fail the test if it is ever called.
"""

from pathlib import Path

import pytest


class TestConfluenceIdRegression:
    """Local validation of the ``confluence_id`` frontmatter field."""

    def test_sync_confluence_with_invalid_confluence_id_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        sync_confluence must reject a file whose confluence_id
        is not a valid Confluence page id (a numeric string), and it must do so
        by validating the value locally -- not by letting the Confluence API
        reject it and catching that as a generic error.

        Those two are not the same claim. A generic `except Exception` around
        the fetch would also catch a dropped connection, an expired token, or
        Confluence being down -- none of which say anything about confluence_id
        being well-formed. To tell them apart, urllib.request.urlopen is
        monkeypatched to fail the test if it is ever called: if sync() still
        reports the file as an invalid-format error, the rejection happened
        before any network attempt, which is the only way to prove the field is
        actually validated.
        """
        import urllib.request

        def _fail_if_network_used(*_args: object, **_kwargs: object) -> None:
            pytest.fail(
                "sync() reached urllib.request.urlopen for a malformed "
                "confluence_id -- validation must happen locally, before any "
                "request to Confluence, not rely on the API rejecting it."
            )

        monkeypatch.setattr(urllib.request, "urlopen", _fail_if_network_used)

        # Create a temporary vault with an INVALID confluence_id
        tmp_vault = tmp_path / "vault"
        conventions_dir = tmp_vault / "Conventions"
        conventions_dir.mkdir(parents=True, exist_ok=True)

        invalid_doc = conventions_dir / "InvalidId.md"
        invalid_doc.write_text(
            "---\nconfluence_id: INVALID_ID_NOT_A_NUMBER\nlast_updated: 2026-01-01\n---\n# Invalid\n"
        )

        # Point config at this temp vault. Confluence credentials are set to
        # unresolvable placeholders -- they only need to look "configured" so
        # sync() doesn't short-circuit on missing credentials; the urlopen
        # patch above is what actually guarantees no real request happens.
        monkeypatch.setenv("VAULT_PATH", str(tmp_vault))
        monkeypatch.setenv("CONFLUENCE_URL", "https://invalid.example.test")
        monkeypatch.setenv("CONFLUENCE_EMAIL", "test@example.test")
        monkeypatch.setenv("CONFLUENCE_TOKEN", "test-token-not-a-credential")

        import importlib
        import knowledge.config
        importlib.reload(knowledge.config)

        # Run sync_confluence - it should reject the invalid confluence_id
        # before ever contacting Confluence.
        from knowledge.confluence_sync import sync
        result = sync(dry_run=True, scope_prefix="Conventions/")

        processed_ids = [f.get("confluence_id") for f in result.get("needs_update", [])]
        assert "INVALID_ID_NOT_A_NUMBER" not in processed_ids, (
            "sync_confluence must reject files with invalid confluence_id format."
        )

        error_ids = [e.get("confluence_id") for e in result.get("errors", [])]
        assert "INVALID_ID_NOT_A_NUMBER" in error_ids, (
            "sync_confluence must report the malformed confluence_id as an "
            f"explicit validation error, got errors={result.get('errors')}"
        )
        invalid_error = next(
            e for e in result["errors"] if e.get("confluence_id") == "INVALID_ID_NOT_A_NUMBER"
        )
        assert "invalid" in invalid_error["error"].lower(), (
            f"Expected an invalid-format error message, got: {invalid_error['error']!r}"
        )

    # -------------------------------------------------------------------
    # str.isdigit() Unicode-digit bypass.
    #
    # str(cid).isdigit() returns True for any Unicode codepoint with a
    # "digit" numeric type -- not only ASCII "0"-"9". Validating with it
    # lets confluence_id values written in other numbering systems (or
    # superscript/circled digit glyphs) pass local validation and reach a
    # real network call to Confluence. These cases pin the ASCII-only rule
    # that closes the bypass.
    # -------------------------------------------------------------------

    UNICODE_DIGIT_BYPASS_CASES = [
        pytest.param("১২৩", id="bengali_digits"),
        pytest.param("০", id="bengali_zero"),
        pytest.param("²³", id="superscript_digits"),
        pytest.param("①②③", id="circled_digits"),
    ]

    @pytest.mark.parametrize("cid", UNICODE_DIGIT_BYPASS_CASES)
    def test_is_valid_confluence_id_rejects_unicode_digits(self, cid: str) -> None:
        """
        Unit-level guard: _is_valid_confluence_id() must reject any
        confluence_id that is not a plain ASCII digit string, even when
        str.isdigit() considers it a digit.
        """
        from knowledge.confluence_sync import _is_valid_confluence_id

        assert cid.isdigit(), (
            f"Sanity check failed: {cid!r} was expected to be a str.isdigit() "
            "true positive that ASCII validation must still reject."
        )
        assert _is_valid_confluence_id(cid) is False, (
            f"_is_valid_confluence_id({cid!r}) must be False: it is a "
            "Unicode digit, not an ASCII Confluence page id."
        )

    @pytest.mark.parametrize("cid", ["0", "1", "6421610526", "000123"])
    def test_is_valid_confluence_id_accepts_ascii_digits(self, cid: str) -> None:
        """Sanity check: the ASCII-digit fix must not reject real ids."""
        from knowledge.confluence_sync import _is_valid_confluence_id

        assert _is_valid_confluence_id(cid) is True

    @pytest.mark.parametrize("cid", UNICODE_DIGIT_BYPASS_CASES)
    def test_sync_confluence_with_unicode_digit_id_never_touches_network(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cid: str
    ) -> None:
        """
        End-to-end guard against the bypass: a confluence_id written in
        Bengali digits, superscript digits, or circled digits is rejected
        by sync() before urllib.request.urlopen is ever called. Validation
        that leans on str.isdigit() sends every one of these to Confluence.
        """
        import urllib.request

        def _fail_if_network_used(*_args: object, **_kwargs: object) -> None:
            pytest.fail(
                f"sync() reached urllib.request.urlopen for confluence_id={cid!r} "
                "-- a Unicode digit string must be rejected locally, exactly "
                "like a non-numeric id, never sent to Confluence."
            )

        monkeypatch.setattr(urllib.request, "urlopen", _fail_if_network_used)

        tmp_vault = tmp_path / "vault"
        conventions_dir = tmp_vault / "Conventions"
        conventions_dir.mkdir(parents=True, exist_ok=True)

        doc = conventions_dir / "UnicodeDigitId.md"
        doc.write_text(
            f"---\nconfluence_id: {cid}\nlast_updated: 2026-01-01\n---\n# Unicode digit id\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("VAULT_PATH", str(tmp_vault))
        monkeypatch.setenv("CONFLUENCE_URL", "https://invalid.example.test")
        monkeypatch.setenv("CONFLUENCE_EMAIL", "test@example.test")
        monkeypatch.setenv("CONFLUENCE_TOKEN", "test-token-not-a-credential")

        import importlib
        import knowledge.config
        importlib.reload(knowledge.config)

        from knowledge.confluence_sync import sync
        result = sync(dry_run=True, scope_prefix="Conventions/")

        error_ids = [e.get("confluence_id") for e in result.get("errors", [])]
        assert cid in error_ids, (
            f"sync_confluence must report {cid!r} as an invalid-format error, "
            f"got errors={result.get('errors')}"
        )
        rejected = next(e for e in result["errors"] if e.get("confluence_id") == cid)
        assert "invalid" in rejected["error"].lower(), (
            f"Expected an invalid-format error message for {cid!r}, "
            f"got: {rejected['error']!r}"
        )
