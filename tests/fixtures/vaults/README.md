# Registry fixture vaults

Two small vaults, `alpha` and `beta`, served together through a `vaults.yaml`
by `tests/test_vault_registry.py` and `tests/test_gitsync.py`. They are not the
golden vault (`tests/fixtures/vault/`): the golden master never reads them.

Each vault has its own `vault.yaml`, and the titles and bodies of the two use
disjoint words from the `bag-of-words` embeddings vocabulary, so a query can
tell the vaults apart. Tests copy them before writing anything.

`links` is a third vault, read only by `tests/test_wikilinks.py`: five notes
whose wikilinks exercise the resolver (paths, path case, aliases escaped in a
table, headings, a self-link, a duplicate note name, a missing note and a
`.canvas` target). Its edges are written out by hand in that test.
