from saipet.store import SeenStore


def test_persists_and_reloads(tmp_path):
    path = tmp_path / "seen.json"
    store = SeenStore(path)
    assert not store.has("reddit", "abc")
    store.mark("reddit", "abc")
    assert store.has("reddit", "abc")

    reloaded = SeenStore(path)  # fresh instance, same file
    assert reloaded.has("reddit", "abc")
    assert not reloaded.has("reddit", "other")
