#!/usr/bin/env python3
"""Scan a text file for credential SHAPES and report where, never what.

Used by scripts/build-platform-graph.sh as the publish gate for the merged
platform graph, and callable on its own so the canary battery can exercise one
shape per fixture instead of rebuilding a graph per case.

Exit codes:
    0   nothing matched, and the report says how much was inspected
    1   at least one line matched a credential shape
    2   the scan could not run, or it inspected nothing it was pointed at
        (bad usage, unreadable target, zero lines read from a non-empty file)

Exit 2 exists so a caller can tell "I looked and found nothing" from "I could
not look". Only the first is a pass, and it is only a pass when the report
carries the count: a verdict with no units behind it is indistinguishable from
a scanner that never opened the file. Every exit therefore states how many
lines and characters were read, on stdout when clean and on stderr when not,
and lines are split on "\n" alone so the count can be checked against `rg -c`
by the caller.

Design note, which is the whole point of this file. Anchoring on a vendor
prefix (AKIA, ghp_, sk-ant, ...) only finds credentials issued by vendors that
publish a prefix. An `authpt` query parameter is the
counter-example: an opaque token passed as a URL query parameter, with no
prefix and no keyword a wordlist would have carried. So the rules below come in
three layers:

  1. vendor prefixes           - cheap, exact, zero false positives
  2. shape in context          - `<name> = <value>` and `?<name>=<value>`,
                                 with the quotes around the value OPTIONAL,
                                 plus the legacy auth parameter names that
                                 carry a credential with no prefix
  3. opaque value, no context  - any 32+ character alphanumeric run that looks
                                 random rather than like an identifier,
                                 wherever it sits

Layer 3 is a heuristic and is documented as one. A run qualifies when it mixes
lower case, upper case and digits AND either changes character class on at
least 45 % of its adjacent pairs or carries at least 4.5 bits of Shannon
entropy per character, AND it does not decompose into words. Lower case hex
digests (git shas, md5) never qualify because they carry no upper case.

The word test is not cosmetic: without it the heuristic cannot tell a long
camelCase symbol from a token. The counter-example is measured, not imagined --
`testLoginName2UserNameNoUsersOnLDAP`, a PHPUnit method of the upstream
Nextcloud `user_ldap` app, sits at a class transition rate of 0.471 and 3.976
bits: it clears the transition arm on the strength of its camelCase boundaries
and its `2`, while the entropy arm correctly reports it as not random. A dense
identifier with short words and an acronym is exactly the shape that arm
misreads, and the graph of a PHP corpus is full of them.

Requiring BOTH arms would also silence it, and it was measured before being
rejected: detection of a 32-character secret falls from 98.8 % to 64.7 %. The
word test costs almost nothing instead. A run is treated as an identifier when
it holds at least 4 lower-case word runs of 2+ characters covering at least
70 % of its length. Measured over 20,000 random tokens per length, comparing
the heuristic with and without the word test: 98.83 % -> 98.39 % at 32
characters, 99.86 % -> 99.54 % at 40, 100 % -> 99.98 % at 64, and 98.61 % ->
98.29 % for a 24-byte base64url value. The worst case gives up 0.43 points of
sensitivity; requiring both arms gives up 34.

What this deliberately gives up: a passphrase-shaped secret built from four or
more concatenated lower-case words reaches layer 3 and is not reported by it.
Such a value is still caught by layers 1 and 2 whenever it appears next to a
name (`password=`, `?token=`, a vendor prefix), which is how a passphrase
travels in a code graph. An opaque machine-issued token cannot take that shape.

The run is judged on its own form and on nothing around it. An upper length
bound would mean the longer the secret the safer it travels, and requiring a
non-word character in front of it would mean `sess-<run>` and `tok_<run>` --
the ordinary way a session or bearer value is written -- never reach the
heuristic at all. Maximal munch already splits a run at every non-alphanumeric
character, so the prefix is its own token and the opaque tail is judged alone.
"""

import collections
import math
import os
import re
import sys

# Layer 1 and 2. Order matters only for the reported name: the first rule that
# matches a line is the one printed, and one report per line is enough.
PATTERNS = [
    ("aws_access_key_id", r"\bAKIA[0-9A-Z]{16}\b"),
    ("github_token", r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    ("slack_token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    ("google_api_key", r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    ("atlassian_api_token", r"\bATATT[A-Za-z0-9_\-=]{20,}\b"),
    ("anthropic_api_key", r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b"),
    ("openai_api_key", r"\bsk-[A-Za-z0-9]{32,}\b"),
    ("private_key_block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("json_web_token",
     r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
    ("basic_auth_in_url", r"://[^/\s:@]+:[^/\s@]{6,}@"),
    # Legacy auth parameter names. These carry no prefix and no keyword, so
    # nothing else would see them.
    ("legacy_auth_param",
     r"(?i)\bauth(pt|pw|tok(en)?)\b[ \t]{0,4}[:=][ \t]{0,4}"
     r"[\"'`]?(?![$%{<])[A-Za-z0-9/+._=\-]{8,}"),
    # Generic assignment. The quotes around the value are optional on purpose:
    # requiring them is what let `password=<value>` through.
    ("assigned_secret_literal",
     r"(?i)\b(pass(word|wd)?|pwd|secret|api[_-]?key|token|credential)"
     r"[A-Za-z0-9_\-]{0,24}[ \t]{0,4}[:=][ \t]{0,4}"
     r"[\"'`]?(?![$%{<])[A-Za-z0-9/+._=\-]{12,}"),
    # Any URL query parameter carrying a long opaque value, whatever its name.
    # This is the generic form of the `authpt` case.
    ("url_query_opaque_value",
     r"[?&][A-Za-z_][A-Za-z0-9_.\-]{1,32}=(?![$%{<])[A-Za-z0-9%._~+/\-]{16,}"),
]
COMPILED = [(name, re.compile(rx)) for name, rx in PATTERNS]

# Layer 3. LONG_RUN is a cheap prefilter so the entropy maths only runs on the
# handful of lines that could possibly qualify.
LONG_RUN = re.compile(r"[A-Za-z0-9]{32}")
OPAQUE_RUN = re.compile(r"[A-Za-z0-9]{32,}")
MIN_CLASS_TRANSITION_RATE = 0.45
MIN_SHANNON_BITS = 4.5

#: One word of a camelCase or snake-free identifier: a lower-case run of two or
#: more characters, with the capital that opens it when there is one. `LDAP` and
#: a bare digit are not words, which is why coverage is a fraction and not a
#: requirement that the whole run be consumed.
WORD_RUN = re.compile(r"[A-Z]?[a-z]{2,}")
MIN_IDENTIFIER_WORDS = 4
MIN_IDENTIFIER_COVERAGE = 0.70


def shannon_bits(text):
    """Shannon entropy per character."""
    counts = collections.Counter(text)
    total = float(len(text))
    return -sum((c / total) * math.log(c / total, 2) for c in counts.values())


def class_transition_rate(text):
    """Fraction of adjacent pairs that change character class."""
    def klass(ch):
        if ch.isdigit():
            return "d"
        return "l" if ch.islower() else "u"
    changes = sum(1 for a, b in zip(text, text[1:]) if klass(a) != klass(b))
    return changes / float(len(text) - 1)


def looks_like_identifier(token):
    """True when the run reads as concatenated words rather than as a value.

    A symbol name is words glued together; a machine-issued token is not, and
    cannot be made to be. The check is a fraction rather than a full parse so an
    acronym (`LDAP`) or a version digit (`V2`) inside an otherwise word-shaped
    name does not disqualify it.
    """
    words = WORD_RUN.findall(token)
    if len(words) < MIN_IDENTIFIER_WORDS:
        return False
    covered = sum(len(word) for word in words)
    return covered / float(len(token)) >= MIN_IDENTIFIER_COVERAGE


def looks_opaque(token):
    has_lower = any(c.islower() for c in token)
    has_upper = any(c.isupper() for c in token)
    has_digit = any(c.isdigit() for c in token)
    if not (has_lower and has_upper and has_digit):
        return False
    if looks_like_identifier(token):
        return False
    return (class_transition_rate(token) >= MIN_CLASS_TRANSITION_RATE
            or shannon_bits(token) >= MIN_SHANNON_BITS)


def classify(line):
    """Return the name of the first shape this line matches, or None."""
    for name, rx in COMPILED:
        if rx.search(line):
            return name
    if LONG_RUN.search(line):
        for match in OPAQUE_RUN.finditer(line):
            if looks_opaque(match.group(0)):
                return "opaque_high_entropy_value"
    return None


def main(argv):
    if len(argv) != 2:
        print("usage: scan-credential-shapes.py <file>", file=sys.stderr)
        return 2
    path = argv[1]
    hits = []
    lines = 0
    chars = 0
    try:
        size = os.path.getsize(path)
        # newline="\n" keeps the line count identical to `rg -c ''`, so the
        # caller can check this scan covered the whole artifact instead of
        # taking the exit code on faith.
        with open(path, encoding="utf-8", errors="replace", newline="\n") as fh:
            for lineno, line in enumerate(fh, 1):
                lines = lineno
                chars += len(line)
                name = classify(line)
                if name:
                    hits.append((name, lineno))
    except OSError as exc:
        # Not a pass: the caller must be able to tell this from a clean scan.
        print(f"could not read {path}: {exc.strerror}", file=sys.stderr)
        return 2

    # Inspecting zero units when there were units to inspect is not a clean
    # scan; it is a scan that did not happen, and it gets the "could not look"
    # code rather than the "found nothing" one.
    if size > 0 and lines == 0:
        print(f"read 0 lines from {path}, which holds {size} bytes; nothing was inspected",
              file=sys.stderr)
        return 2

    if hits:
        print(f"{len(hits)} suspected credential(s) in {lines} line(s) inspected; "
              "the value is never printed:", file=sys.stderr)
        for name, lineno in hits[:20]:
            print(f"  {name} at line {lineno}", file=sys.stderr)
        if len(hits) > 20:
            print(f"  ... and {len(hits) - 20} more", file=sys.stderr)
        return 1

    # The marker is the caller's contract: a fixed prefix plus the counts. A
    # scanner that exits 0 without printing it has not reported an inspection,
    # and the caller is expected to refuse that.
    print(f"credential-scan: clean rules={len(COMPILED)} heuristics=1 lines={lines} chars={chars}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
