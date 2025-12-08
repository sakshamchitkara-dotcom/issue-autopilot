import os
import subprocess


def make_repo(tmp_path, files):
    """Create a throwaway git repo with `files` committed."""
    root = str(tmp_path)
    env = {**os.environ, "GIT_AUTHOR_NAME": "Ada", "GIT_AUTHOR_EMAIL": "ada@example.com",
           "GIT_COMMITTER_NAME": "Ada", "GIT_COMMITTER_EMAIL": "ada@example.com"}
    subprocess.run(["git", "init", "-q", root], check=True)
    for rel, text in files.items():
        full = os.path.join(root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as fh:
            fh.write(text)
    subprocess.run(["git", "-C", root, "add", "-A"], check=True)
    subprocess.run(["git", "-C", root, "commit", "-qm", "init"], check=True, env=env)
    return root
