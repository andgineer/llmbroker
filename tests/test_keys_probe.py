"""What one merge site can learn about the keys behind a lineup."""

from llmbroker.broker.keys import KeyProbe
from llmbroker.standalone.secrets import DictSecrets, Secrets


async def test_a_scope_prefixed_ref_resolves_as_present():
    probe = KeyProbe(DictSecrets({"alice/K": "own"}), scope="alice")
    assert (await probe.evidence(["K"])).present == {"K"}


async def test_an_empty_value_reads_as_absent(monkeypatch):
    monkeypatch.setenv("BLANK", "")
    monkeypatch.setenv("FILLED", "v")
    evidence = await KeyProbe(Secrets()).evidence(["BLANK", "FILLED"])
    assert evidence.present == {"FILLED"}


async def test_have_keys_true_declares_everything():
    probe = KeyProbe(DictSecrets({}), have_keys=True)
    assert (await probe.evidence(["A", "B"])).present == {"A", "B"}


async def test_an_empty_ref_is_not_a_ref():
    probe = KeyProbe(DictSecrets({"A": "v"}), have_keys=True)
    assert (await probe.evidence(["", "A"])).present == {"A"}


async def test_have_keys_as_a_bare_string_declares_that_one_ref():
    """A str is a Sequence[str]; taken apart into characters it declares nothing."""
    probe = KeyProbe(DictSecrets({}), have_keys="GEMINI_API_KEY")
    evidence = await probe.evidence(["GEMINI_API_KEY", "GROQ"])
    assert evidence.present == {"GEMINI_API_KEY"}


async def test_have_keys_declares_a_ref_the_backend_cannot_resolve():
    probe = KeyProbe(DictSecrets({}), have_keys=["GEMINI"])
    assert (await probe.evidence(["GEMINI", "GROQ"])).present == {"GEMINI"}


# ── How much a missing key proves ────────────────────────────────────────────


async def test_a_probe_that_resolved_nothing_at_all_proves_nothing():
    """Registry in llms.toml, secrets in Vault: absence here says the keys live
    somewhere this merge site cannot reach."""
    evidence = await KeyProbe(DictSecrets({})).evidence(["GEMINI"])
    assert (evidence.present, evidence.visible) == (frozenset(), False)


async def test_a_scoped_installation_needs_a_declaration_before_absence_counts():
    secrets = DictSecrets({"alice/GEMINI": "sk"})
    scoped = await KeyProbe(secrets, scope="alice").evidence(["GEMINI"])
    assert (scoped.visible, scoped.scoped) == (False, True)

    declared = await KeyProbe(secrets, scope="alice", have_keys=["GEMINI"]).evidence(["GEMINI"])
    assert (declared.visible, declared.scoped) == (True, True)

    shared = await KeyProbe(DictSecrets({"GEMINI": "sk"})).evidence(["GEMINI"])
    assert (shared.visible, shared.scoped) == (True, False)
