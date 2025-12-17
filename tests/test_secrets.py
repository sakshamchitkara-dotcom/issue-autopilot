from issue_autopilot.sources import Context, secrets

# Fake credentials are assembled at runtime so this file never contains a real-looking token.
AWS = "AKIA" + "Q7" * 8
GHP = "ghp_" + "a1B2" * 9


def scan(tmp_path, files):
    for rel, text in files.items():
        (tmp_path / rel).write_text(text)
    return secrets.scan(Context(path=str(tmp_path), repo=None, gh=None))


def test_detects_and_redacts(tmp_path):
    sigs = scan(tmp_path, {"cfg.py": f'KEY = "{AWS}"\nTOKEN = "{GHP}"\npassword = "hunter2hunter2"\n'})
    assert [s.meta["rule"] for s in sigs] == ["aws-access-key", "github-token", "generic-secret"]
    for s in sigs:
        assert AWS not in s.summary and GHP not in s.summary and "hunter2hunter2" not in s.summary
        assert s.priority == "P1" and s.group == "cfg.py"


def test_ignores_placeholders_and_lockfiles(tmp_path):
    sigs = scan(tmp_path, {
        "a.py": 'api_key = "your-api-key-here"\nsecret = "${SECRET_FROM_ENV}"\n',
        "package-lock.json": f'"x": "{AWS}"',
    })
    assert sigs == []


def test_private_key_header(tmp_path):
    sigs = scan(tmp_path, {"id_rsa": "-----BEGIN " + "RSA PRIVATE KEY-----\nabc\n"})
    assert sigs[0].meta["rule"] == "private-key"
